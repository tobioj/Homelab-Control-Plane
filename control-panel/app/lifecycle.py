"""Post-onboarding lifecycle management: pause/resume a running project,
recommission a decommissioned one, and permanently delete a project's
control-panel record. Distinct from decommission.py, which is specifically
the shutdown sequence (stop+disable+unlink+free-ledger) plus its two
separately-confirmed teardown stages (delete files, delete cert). This
module is everything AFTER that: bringing a project back, or actually
removing its last trace once it's fully torn down.

Same conventions as every other mutating module in this app: every entry
point re-checks safety.assert_project_not_protected() even where it seems
redundant, every plain-shell-command action goes through
actions.create_preview_row()/confirm_and_run(), and the two actions that
aren't a single shell command (recommission-with-reallocation, delete
forever) use a bespoke confirm function following the same shape
onboarding.confirm_write_env() and dns_cert.confirm_certbot_start()
already established — SFTP/local-DB work first, an optional shell command
second, then actions.record_result() ties it back into the same
pending_actions/audit trail as everything else.
"""

import shlex

from app.db import get_conn, next_free_redis_index
from app.audit import log_action
from app import actions, safety, ssh_client, onboarding


def _enabled_services(project: dict):
    services = ["gunicorn"]
    if project["uses_celery"]:
        services.append("celery")
    if project["uses_celerybeat"]:
        services.append("celerybeat")
    return services


# ---------------------------------------------------------------------------
# Pause / resume — temporary, reversible, touches only the services
# ---------------------------------------------------------------------------

def create_pause_preview(actor: str, project: dict) -> str:
    """Stop AND disable — not just stop. A VM reboot must not silently
    un-pause a project (systemd would otherwise bring an enabled unit back
    on its own). Nginx site, Redis ledger, and cert are all left exactly as
    they are — this is reversible with `resume`, unlike decommission."""
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "running":
        raise actions.ActionError(f"{project['name']} is '{project['status']}', not 'running' — nothing to pause")
    parts = []
    for svc in _enabled_services(project):
        unit = f"{svc}@{project['name']}"
        parts.append(f"sudo -n /usr/bin/systemctl stop {shlex.quote(unit)}")
        parts.append(f"sudo -n /usr/bin/systemctl disable {shlex.quote(unit)}")
    command = " && ".join(parts)
    return actions.create_preview_row(actor, project, command, f"ALL@{project['name']}", "lifecycle_pause")


def finalize_pause(project_id: int, actor: str):
    with get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'paused' WHERE id = ?", (project_id,))
    log_action(actor=actor, action="lifecycle_paused", target=str(project_id))


def create_resume_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "paused":
        raise actions.ActionError(f"{project['name']} is '{project['status']}', not 'paused' — nothing to resume")
    parts = []
    for svc in _enabled_services(project):
        unit = f"{svc}@{project['name']}"
        parts.append(f"sudo -n /usr/bin/systemctl enable {shlex.quote(unit)}")
        parts.append(f"sudo -n /usr/bin/systemctl start {shlex.quote(unit)}")
    command = " && ".join(parts)
    return actions.create_preview_row(actor, project, command, f"ALL@{project['name']}", "lifecycle_resume")


def finalize_resume(project_id: int, actor: str):
    with get_conn() as conn:
        conn.execute("UPDATE projects SET status = 'running' WHERE id = ?", (project_id,))
    log_action(actor=actor, action="lifecycle_resumed", target=str(project_id))


# ---------------------------------------------------------------------------
# Recommission — bring a decommissioned project back to life
# ---------------------------------------------------------------------------

async def check_recommission_readiness(project: dict) -> dict:
    """Read-only. Decides which of the two recommission paths (plain
    reclaim vs. explicit reallocation) applies, without mutating anything —
    the actual create_*_preview functions re-verify all of this themselves
    right before building a command, same 'never trust a result computed
    moments earlier in a different request' discipline as
    decommission.create_delete_files_preview's live re-check."""
    name = project["name"]
    dir_check = await ssh_client.run(f"test -d {shlex.quote('/srv/apps/' + name)} && echo yes || echo no", host=project["vm_ip"], timeout=15)
    nginx_check = await ssh_client.run(f"test -f {shlex.quote('/etc/nginx/sites-available/' + name)} && echo yes || echo no", host=project["vm_ip"], timeout=15)
    files_ok = dir_check["stdout"].strip() == "yes"
    nginx_conf_ok = nginx_check["stdout"].strip() == "yes"

    # The nginx site file references a cert path directly — if the cert is
    # gone, nginx -t fails partway through the compound recommission
    # command. Can't check this with a direct filesystem read: /etc/
    # letsencrypt/ is root-only (0700), unreadable by the deploy user this
    # app SSHes in as, and there's no granted sudoers command for a plain
    # existence test there (caught live: an earlier version of this check
    # used a bare `test -f` into that directory and always silently
    # returned "gone" regardless of reality, since it couldn't even see
    # into it). Best available signal instead: decommission.
    # finalize_delete_cert() clears cert_expiry_date to NULL only after
    # certbot itself confirms deletion succeeded, and dns_cert.
    # confirm_certbot_start() only ever SETS it after a real successful
    # issuance — so cert_expiry_date IS NOT NULL is a reliable proxy for
    # "a real cert exists." (duckdns_subdomain alone is NOT this signal —
    # it's set during Phase 2 subdomain-intent recording, long before any
    # cert is actually issued; a project that never reached Phase 3 has a
    # subdomain but no cert, and confusing the two caused a real stuck
    # project live — see CLAUDE.md bug list.) Not airtight against a cert
    # removed outside the app entirely (manual cleanup, expiry), which is
    # exactly what _recommission_start_command's self-healing nginx step
    # (see its docstring) exists to catch as the real backstop.
    cert_ok = project["cert_expiry_date"] is not None

    conflicts = []
    with get_conn() as conn:
        for idx in {project["redis_broker_index"], project["redis_cache_index"]}:
            if idx is None:
                continue
            row = conn.execute(
                "SELECT status, project_id FROM redis_ledger WHERE idx = ?", (idx,)
            ).fetchone()
            if row is not None and row["status"] == "used" and row["project_id"] != project["id"]:
                conflicting = conn.execute("SELECT name FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
                conflicts.append({"idx": idx, "project_name": conflicting["name"] if conflicting else f"id {row['project_id']}"})

    return {"files_ok": files_ok, "nginx_conf_ok": nginx_conf_ok, "cert_ok": cert_ok, "conflicts": conflicts}


def _recommission_start_command(project: dict) -> str:
    """The nginx step self-cleans on failure: `ln -sf` has to run before
    `nginx -t` can test the result, but if the test then fails, a plain
    `&&` chain leaves the broken symlink sitting in sites-enabled/ —
    caught live during testing, where a failed recommission (missing cert)
    left a stray symlink that blocked `nginx -t` for every OTHER project's
    reloads too, until manually cleaned up. `(nginx -t || (rm -f <link> &&
    exit 1))` restores the prior state on failure instead of leaving debris
    behind. cert_ok (readiness check) should normally prevent ever reaching
    this in a broken state, but this is the actual backstop if it doesn't."""
    name = project["name"]
    available = shlex.quote("/etc/nginx/sites-available/" + name)
    enabled = shlex.quote("/etc/nginx/sites-enabled/" + name)
    parts = [
        f"sudo -n /usr/bin/ln -sf {available} {enabled} && "
        f"(sudo -n /usr/sbin/nginx -t || (sudo -n /usr/bin/rm -f {enabled} && exit 1))",
        "sudo -n /usr/bin/systemctl reload nginx",
    ]
    for svc in _enabled_services(project):
        unit = f"{svc}@{name}"
        parts.append(f"sudo -n /usr/bin/systemctl enable {shlex.quote(unit)}")
        parts.append(f"sudo -n /usr/bin/systemctl start {shlex.quote(unit)}")
    return " && ".join(parts)


async def create_recommission_preview(actor: str, project: dict) -> str:
    """No-conflict path only — refuses if a conflict exists rather than
    silently picking a side, so the operator is always routed through the
    explicit reallocate confirmation for that case instead."""
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "decommissioned":
        raise actions.ActionError(f"{project['name']} is '{project['status']}', not 'decommissioned' — nothing to recommission")

    readiness = await check_recommission_readiness(project)
    if not readiness["files_ok"]:
        raise actions.ActionError(f"{project['name']}'s files are gone from VM 100 — recommission isn't possible, only 'delete forever' is available.")
    if not readiness["nginx_conf_ok"]:
        raise actions.ActionError(f"{project['name']}'s nginx site file is gone from VM 100 — recommission isn't possible in its current state.")
    if not readiness["cert_ok"]:
        raise actions.ActionError(f"{project['name']}'s certificate is gone from VM 100 — its nginx site references a cert that no longer exists. Re-issue a cert via the Phase 3 wizard before recommissioning.")
    if readiness["conflicts"]:
        names = ", ".join(f"idx {c['idx']} (now {c['project_name']})" for c in readiness["conflicts"])
        raise actions.ActionError(f"{project['name']}'s Redis indices are no longer free: {names}. Use the reallocation step instead.")

    command = _recommission_start_command(project)
    return actions.create_preview_row(actor, project, command, f"ALL@{project['name']}", "lifecycle_recommission")


def finalize_recommission(project_id: int, actor: str):
    with get_conn() as conn:
        project = conn.execute("SELECT redis_broker_index, redis_cache_index FROM projects WHERE id = ?", (project_id,)).fetchone()
        for idx in {project["redis_broker_index"], project["redis_cache_index"]}:
            if idx is None:
                continue
            # AND status='free' is a last-instant race guard, not just a
            # preview-time check — if something claimed this index in the
            # gap between preview and confirm, don't silently steal it back.
            conn.execute(
                "UPDATE redis_ledger SET status = 'used', project_id = ? WHERE idx = ? AND status = 'free'",
                (project_id, idx),
            )
        conn.execute("UPDATE projects SET status = 'running' WHERE id = ?", (project_id,))
    log_action(actor=actor, action="lifecycle_recommissioned", target=str(project_id))


async def create_recommission_reallocate_preview(actor: str, project: dict) -> str:
    """Conflict path — only reached after the operator has explicitly seen
    the conflict (which index, which project) and chosen to proceed anyway.
    Computes what NEW indices would be allocated so the preview shows the
    real numbers before anything is written. `new_cache` is requested with
    `new_broker` passed as `exclude` rather than computed as `new_broker +
    1` — real bug found live in the onboarding.py equivalent of this same
    pattern: `+1` only worked under the old "always increment" allocator;
    now that next_free_redis_index() can return a reclaimed, non-
    consecutive index, `+1` risked colliding with a different project's
    real index (brief §4 bug #2's collision class)."""
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "decommissioned":
        raise actions.ActionError(f"{project['name']} is '{project['status']}', not 'decommissioned'")
    readiness = await check_recommission_readiness(project)
    if not readiness["files_ok"] or not readiness["nginx_conf_ok"]:
        raise actions.ActionError(f"{project['name']}'s files or nginx site are gone from VM 100 — recommission isn't possible.")
    if not readiness["cert_ok"]:
        raise actions.ActionError(f"{project['name']}'s certificate is gone from VM 100 — re-issue a cert via the Phase 3 wizard before recommissioning.")
    if not readiness["conflicts"]:
        raise actions.ActionError("No Redis index conflict detected — use the plain recommission action instead.")

    separate_cache = project["redis_broker_index"] != project["redis_cache_index"]
    new_broker = next_free_redis_index()
    new_cache = next_free_redis_index(exclude=frozenset({new_broker})) if separate_cache else new_broker

    command = _recommission_start_command(project)
    preview_note = (
        f"Reallocate Redis indices: broker /{new_broker}" +
        (f", cache /{new_cache}" if separate_cache else " (also used as cache)") +
        f" — then: {command}"
    )
    return actions.create_preview_row(actor, project, preview_note, f"ALL@{project['name']}", "lifecycle_recommission_reallocate")


async def confirm_recommission_reallocate(token: str, actor: str) -> dict:
    """Bespoke confirm, same shape as dns_cert.confirm_certbot_start:
    local DB + SFTP work first, then a shell command, in one call. Re-checks
    the new indices are still free at confirm time (retry once — if still
    conflicted, fail cleanly rather than getting clever about it)."""
    row = actions.load_pending_for_confirm(token)
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE name = ?", (row["project_name"],)).fetchone())

    log_action(actor=actor, action="confirm_execute", target=row["target_unit"], details="(reallocating Redis indices)")

    separate_cache = project["redis_broker_index"] != project["redis_cache_index"]

    def _try_allocate():
        broker = next_free_redis_index()
        cache = next_free_redis_index(exclude=frozenset({broker})) if separate_cache else broker
        return broker, cache

    new_broker, new_cache = _try_allocate()
    with get_conn() as conn:
        conflict = conn.execute(
            "SELECT 1 FROM redis_ledger WHERE idx IN (?, ?) AND status != 'free'",
            (new_broker, new_cache),
        ).fetchone()
    if conflict is not None:
        # Someone else grabbed the same "next free" index in the gap
        # between preview and confirm — retry exactly once with a fresh
        # number rather than looping indefinitely.
        new_broker, new_cache = _try_allocate()
        with get_conn() as conn:
            conflict = conn.execute(
                "SELECT 1 FROM redis_ledger WHERE idx IN (?, ?) AND status != 'free'",
                (new_broker, new_cache),
            ).fetchone()
        if conflict is not None:
            result = {"exit_status": 1, "stdout": "", "stderr": "Redis index allocation raced twice — retry the reallocation."}
            return actions.record_result(token, row, result)

    with get_conn() as conn:
        broker_purpose = "Celery broker + result backend (reclaimed via reallocation)" if project["uses_celery"] else "cache (reallocated)"
        conn.execute(
            """INSERT INTO redis_ledger (idx, project_id, purpose, status) VALUES (?, ?, ?, 'used')
               ON CONFLICT(idx) DO UPDATE SET project_id = excluded.project_id, purpose = excluded.purpose, status = excluded.status""",
            (new_broker, project["id"], broker_purpose),
        )
        if separate_cache:
            conn.execute(
                """INSERT INTO redis_ledger (idx, project_id, purpose, status) VALUES (?, ?, ?, 'used')
                   ON CONFLICT(idx) DO UPDATE SET project_id = excluded.project_id, purpose = excluded.purpose, status = excluded.status""",
                (new_cache, project["id"], "cache (separate, reallocated)"),
            )
        conn.execute(
            "UPDATE projects SET redis_broker_index = ?, redis_cache_index = ? WHERE id = ?",
            (new_broker, new_cache, project["id"]),
        )
        project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone())

    env_content = onboarding.build_env_file_content(project)
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    write_result = await ssh_client.write_file(f"{app_dir}/.env", env_content, host=project["vm_ip"], mode=0o600)
    if write_result["exit_status"] != 0:
        return actions.record_result(token, row, write_result)

    command = _recommission_start_command(project)
    result = await ssh_client.run(command, host=project["vm_ip"])
    row = actions.record_result(token, row, result)
    if row["status"] == "executed":
        with get_conn() as conn:
            conn.execute("UPDATE projects SET status = 'running' WHERE id = ?", (project["id"],))
    return row


# ---------------------------------------------------------------------------
# Delete forever — permanent, requires files/cert already gone
# ---------------------------------------------------------------------------

async def create_delete_forever_preview(actor: str, project: dict) -> str:
    """Deliberately does NOT delete files or cert itself — requires them
    already gone via the existing separate delete-files/delete-cert
    actions, matching this project's established 'separate confirmation
    per irreversible step' convention (brief §5). Only ever removes this
    tool's own record of the project plus any leftover ledger claim."""
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "decommissioned":
        raise actions.ActionError(f"{project['name']} must be decommissioned before it can be deleted forever")

    name = project["name"]
    dir_check = await ssh_client.run(f"test -d {shlex.quote('/srv/apps/' + name)} && echo yes || echo no", host=project["vm_ip"], timeout=15)
    if dir_check["stdout"].strip() == "yes":
        raise actions.ActionError(f"{name}'s files still exist on VM 100 — delete files first.")
    # NOT a live filesystem check — /etc/letsencrypt/ is root-only (0700),
    # unreadable by the deploy user this app SSHes in as, and there's no
    # granted sudoers command for a plain existence test there (caught live
    # during testing: an earlier version of this check always silently
    # returned "gone" regardless of reality, since it couldn't even see the
    # directory). decommission.finalize_delete_cert() clears
    # cert_expiry_date to NULL only after certbot itself confirms deletion
    # succeeded — that DB state IS the trustworthy signal here.
    # cert_expiry_date, not duckdns_subdomain: a project can have a
    # recorded subdomain INTENT (Phase 2) with no cert ever issued (Phase 3
    # never run) — that project has nothing to "delete the cert" for and
    # shouldn't be blocked waiting on it.
    if project["cert_expiry_date"] is not None:
        raise actions.ActionError(f"{name} still has a recorded certificate — delete the cert first.")

    # Not a real shell command — matches onboarding.create_write_env_preview's
    # redacted-placeholder precedent for a preview that isn't actually a
    # remote exec.
    placeholder = f"DELETE FROM projects WHERE id={project['id']}; DELETE FROM project_env_vars WHERE project_id={project['id']}; free any remaining Redis ledger rows"
    return actions.create_preview_row(actor, project, placeholder, project["name"], "lifecycle_delete_forever")


async def confirm_delete_forever(token: str, actor: str) -> dict:
    """Bespoke confirm with no SSH at all — the 'remote command' is
    actually local DB statements. Re-verifies everything one more time
    (protected/status/file-absence) rather than trusting the preview is
    still valid, then does the deletion in one transaction. Builds a
    synthetic result dict for actions.record_result(), same trick
    onboarding.confirm_write_env already uses for a non-shell action, to
    keep this in the same pending_actions/audit trail as every other
    action without inventing a new bookkeeping mechanism."""
    row = actions.load_pending_for_confirm(token)
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE name = ?", (row["project_name"],)).fetchone())

    try:
        safety.assert_project_not_protected(project["name"])
        if project["status"] != "decommissioned":
            raise actions.ActionError(f"{project['name']} is no longer 'decommissioned'")
        name = project["name"]
        dir_check = await ssh_client.run(f"test -d {shlex.quote('/srv/apps/' + name)} && echo yes || echo no", host=project["vm_ip"], timeout=15)
        if dir_check["stdout"].strip() == "yes":
            raise actions.ActionError(f"{name}'s files reappeared on VM 100 since the preview — refusing to delete the record.")
        # See create_delete_forever_preview — cert_expiry_date is the
        # trustworthy signal, not a direct filesystem read.
        if project["cert_expiry_date"] is not None:
            raise actions.ActionError(f"{name} has a recorded certificate again since the preview — refusing to delete the record.")
    except actions.ActionError as e:
        result = {"exit_status": 1, "stdout": "", "stderr": str(e)}
        return actions.record_result(token, row, result)

    with get_conn() as conn:
        conn.execute("UPDATE redis_ledger SET status = 'free', project_id = NULL WHERE project_id = ?", (project["id"],))
        conn.execute("DELETE FROM project_env_vars WHERE project_id = ?", (project["id"],))
        conn.execute("DELETE FROM projects WHERE id = ?", (project["id"],))

    log_action(actor=actor, action="lifecycle_deleted_forever", target=project["name"], details=f"project id {project['id']}")
    result = {"exit_status": 0, "stdout": f"Deleted control-panel record for {project['name']} (id {project['id']}).", "stderr": ""}
    return actions.record_result(token, row, result)
