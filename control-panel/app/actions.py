"""Generic preview-then-confirm pipeline for state-changing actions against
a managed VM. This is the ONLY module allowed to build a command string for
a mutating action — every command is assembled from validated enum/DB
inputs, never from raw request text, so there is no injection surface even
though the values ultimately flow into a shell command over SSH.

Flow: create_preview() -> row in pending_actions, status='pending', returns
a token -> operator reviews the exact command in the UI -> confirm_and_run()
re-validates the row is still pending and not expired, executes over SSH,
records the result, and marks it 'executed' or 'failed'. cancel() marks it
'cancelled' without ever touching VM 100.
"""

import secrets
import shlex
from datetime import datetime, timedelta

from app.db import get_conn
from app.audit import log_action
from app import ssh_client

PREVIEW_TTL_MINUTES = 10

ALLOWED_SERVICES = {"gunicorn", "celery", "celerybeat"}

SYSTEMCTL_PATH = "/usr/bin/systemctl"


class ActionError(ValueError):
    pass


def _validate_service_for_project(project: dict, service: str):
    if service not in ALLOWED_SERVICES:
        raise ActionError(f"Unknown service kind: {service!r}")
    if service == "celery" and not project["uses_celery"]:
        raise ActionError(f"{project['name']} does not have a celery worker enabled")
    if service == "celerybeat" and not project["uses_celerybeat"]:
        raise ActionError(f"{project['name']} does not have celerybeat enabled")


def create_preview_row(actor: str, project: dict, command: str, target_unit: str, action_type: str) -> str:
    """Shared by every action kind (single-command restarts, compound
    decommission sequences, whatever comes next) — the one place a
    pending_actions row is ever inserted. Callers are responsible for
    validating their own inputs and building `command` from trusted,
    server-side data before calling this."""
    token = secrets.token_urlsafe(24)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO pending_actions
               (token, actor, action_type, target_host, target_unit, command, project_name, is_live, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (token, actor, action_type, project["vm_ip"], target_unit, command, project["name"], project["is_live"]),
        )
    log_action(actor=actor, action=f"{action_type}_preview_created", target=target_unit, details=command)
    return token


def create_preview(actor: str, project: dict, service: str) -> str:
    """Validates inputs against the project's actual DB record (never
    trusts a raw unit name from the request), builds the exact command that
    would run, and stores it as a pending row. Returns the preview token."""
    _validate_service_for_project(project, service)

    unit = f"{service}@{project['name']}"
    # Absolute path + exact single-argument form, matching the sudoers
    # entries on VM 100 verbatim (sudoers matches literally, no wildcards).
    command = f"sudo -n {SYSTEMCTL_PATH} restart {shlex.quote(unit)}"
    return create_preview_row(actor, project, command, unit, "service_restart")


def get_pending(token: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_actions WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None


def _is_expired(row: dict) -> bool:
    created = datetime.fromisoformat(row["created_at"])
    return datetime.utcnow() - created > timedelta(minutes=PREVIEW_TTL_MINUTES)


def load_pending_for_confirm(token: str) -> dict:
    """Shared validation for every confirm path (plain shell commands here,
    or the SFTP-based .env write in onboarding.py) — not pending, already
    resolved, or expired all raise the same way regardless of how the
    action actually gets executed once validated.

    Atomically claims the row via `claimed_at` rather than a plain SELECT-
    then-check — real bug found live: some confirm paths (certbot's DNS-01
    challenge in particular) take 40-60+ seconds with no visible progress
    indicator in the UI, which invites a double-submit (impatient second
    click, or a browser retry). Under the old SELECT-then-check, two
    concurrent requests for the SAME token could both observe status=
    'pending' and both proceed to run the underlying action concurrently —
    for certbot specifically, that's two real certonly runs against the
    same domain racing each other, with whichever's record_result() UPDATE
    lands last silently overwriting the other's recorded outcome (record_
    result has no status guard of its own).

    Deliberately does NOT add a new `status` enum value for this (e.g.
    'confirming') — pending_actions.status has a CHECK(status IN (...))
    constraint (db.py), and SQLite can't ALTER a CHECK constraint without a
    full table rebuild, which this codebase has no existing pattern for.
    `claimed_at` is a separate, unconstrained nullable column instead —
    same atomic-claim shape already used elsewhere in this codebase for a
    race guard (lifecycle.finalize_recommission's `AND status = 'free'`),
    just via a dedicated column rather than overloading `status`."""
    row = get_pending(token)
    if row is None:
        raise ActionError("No such pending action")
    if row["status"] != "pending":
        raise ActionError(f"This action is already '{row['status']}' — cannot confirm again")
    if _is_expired(row):
        with get_conn() as conn:
            conn.execute("UPDATE pending_actions SET status = 'expired' WHERE token = ?", (token,))
        raise ActionError("This preview expired — go back and generate a new one")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE pending_actions SET claimed_at = datetime('now') WHERE token = ? AND status = 'pending' AND claimed_at IS NULL",
            (token,),
        )
        if cur.rowcount == 0:
            raise ActionError("This action is already being confirmed (or was just resolved) — check its result before retrying.")
    return row


def record_result(token: str, row: dict, result: dict) -> dict:
    """Shared result-recording for every confirm path. `result` is an
    ssh_client-shaped dict (exit_status/stdout/stderr) regardless of whether
    it came from run() or write_file()."""
    status = "executed" if result["exit_status"] == 0 else "failed"
    with get_conn() as conn:
        conn.execute(
            """UPDATE pending_actions
               SET status = ?, executed_at = datetime('now'), exit_code = ?, stdout = ?, stderr = ?
               WHERE token = ?""",
            (status, result["exit_status"], result["stdout"], result["stderr"], token),
        )
    log_action(
        actor=row["actor"], action="action_result", target=row["target_unit"],
        details=f"exit={result['exit_status']}", result=status,
    )
    row.update(status=status, exit_code=result["exit_status"], stdout=result["stdout"], stderr=result["stderr"])
    return row


async def confirm_and_run(token: str, actor: str) -> dict:
    row = load_pending_for_confirm(token)
    log_action(actor=actor, action="confirm_execute", target=row["target_unit"], details=row["command"])
    result = await ssh_client.run(row["command"], host=row["target_host"])
    return record_result(token, row, result)


def cancel(token: str, actor: str):
    row = get_pending(token)
    if row is None:
        raise ActionError("No such pending action")
    if row["status"] != "pending":
        raise ActionError(f"This action is already '{row['status']}' — nothing to cancel")
    with get_conn() as conn:
        conn.execute("UPDATE pending_actions SET status = 'cancelled' WHERE token = ?", (token,))
    log_action(actor=actor, action="cancelled", target=row["target_unit"])
