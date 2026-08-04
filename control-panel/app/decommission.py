"""Decommission flow (§3.2 of the infra brief). Reuses the exact same
pending_actions preview/confirm/cancel/expiry machinery as app/actions.py —
create_preview_row() + actions.confirm_and_run() — for the actual remote
command execution. What's specific to decommission lives here:

  - building a compound stop+disable+unlink+reload command from whichever
    services a project actually has enabled (not a fixed single command)
  - the control-panel-side bookkeeping that must happen AFTER the remote
    command succeeds: freeing the project's Redis ledger row(s) and
    marking the project 'decommissioned'
  - a SEPARATE, independently confirmed second stage for deleting the
    project directory — only offered once stage 1 has actually completed,
    per the brief's explicit call for this to be its own confirmation step
"""

import shlex
from datetime import datetime

from app.db import get_conn
from app.audit import log_action
from app import actions, safety, ssh_client


def _enabled_services(project: dict):
    services = ["gunicorn"]
    if project["uses_celery"]:
        services.append("celery")
    if project["uses_celerybeat"]:
        services.append("celerybeat")
    return services


def _build_stop_command(project: dict) -> str:
    name = project["name"]
    parts = []
    for svc in _enabled_services(project):
        unit = f"{svc}@{name}"
        # Belt-and-suspenders: even though `name` is already checked against
        # the protected-project denylist by the caller, also verify the
        # computed unit/site filenames themselves never collide with a
        # protected shared file — catches the case where some future caller
        # builds this from an unchecked/derived name.
        safety.assert_filename_not_protected(f"{unit}.service")
        parts.append(f"sudo -n /usr/bin/systemctl stop {shlex.quote(unit)}")
        parts.append(f"sudo -n /usr/bin/systemctl disable {shlex.quote(unit)}")
    safety.assert_filename_not_protected(name)
    parts.append(f"sudo -n /usr/bin/rm -f {shlex.quote('/etc/nginx/sites-enabled/' + name)}")
    parts.append("sudo -n /usr/bin/systemctl reload nginx")
    return " && ".join(parts)


def create_stop_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    if project["status"] == "decommissioned":
        raise actions.ActionError(f"{project['name']} is already decommissioned")
    command = _build_stop_command(project)
    return actions.create_preview_row(actor, project, command, f"ALL@{project['name']}", "decommission_stop")


def finalize_stop(token: str, actor: str, project_id: int):
    """Call after actions.confirm_and_run() reports status == 'executed' for
    a decommission_stop token. Frees this project's ledger rows and marks
    the project decommissioned. Never runs if the remote command failed —
    a failed stop should not silently free indices that might still be in
    use by a half-stopped project."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE redis_ledger
               SET status = 'free', project_id = NULL,
                   purpose = 'freed from ' || (SELECT name FROM projects WHERE id = ?) || ' on ' || ?
               WHERE project_id = ?""",
            (project_id, datetime.utcnow().isoformat(timespec="seconds"), project_id),
        )
        conn.execute("UPDATE projects SET status = 'decommissioned' WHERE id = ?", (project_id,))
    log_action(actor=actor, action="ledger_freed_and_project_decommissioned", target=str(project_id))


async def create_delete_files_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "decommissioned":
        raise actions.ActionError(
            f"{project['name']} must be decommissioned (services stopped) before its files can be deleted"
        )
    # The DB's 'decommissioned' status can drift from live reality — e.g. if
    # someone manually restarts a service after stage 1 (observed during
    # testing). Never trust that field alone before an irreversible rm -rf;
    # re-check actual systemd state on the VM right before building the
    # command.
    for svc in _enabled_services(project):
        unit = f"{svc}@{project['name']}"
        live = await ssh_client.unit_status(unit, host=project["vm_ip"])
        if live == "active":
            raise actions.ActionError(
                f"{unit} is currently active on {project['vm_ip']} — refusing to delete "
                f"files out from under a running process. Stop it first."
            )
    # No sudo needed — deploy owns /srv/apps/<project> and its own SSH keys
    # directly. Deploy-key cleanup rides along with this step rather than
    # delete-forever specifically — a real gap noted in CLAUDE.md §5: the
    # orphaned ~/.ssh/deploy_<name>[.pub] keypair was never removed by any
    # existing action, accumulating indefinitely. delete_files is the right
    # home for it (not delete_cert, not delete_forever) since a deploy key
    # is repo-access, not cert/DNS-related, and this step is already the
    # one that irreversibly removes the project's on-disk footprint.
    parts = [f"rm -rf {shlex.quote('/srv/apps/' + project['name'])}"]
    if project["deploy_key_path"]:
        parts.append(f"rm -f {shlex.quote(project['deploy_key_path'])} {shlex.quote(project['deploy_key_path'] + '.pub')}")
    command = " && ".join(parts)
    return actions.create_preview_row(actor, project, command, project["name"], "decommission_delete_files")


def create_delete_cert_preview(actor: str, project: dict) -> str:
    safety.assert_project_not_protected(project["name"])
    if project["status"] != "decommissioned":
        raise actions.ActionError(
            f"{project['name']} must be decommissioned before its certificate can be deleted"
        )
    # cert_expiry_date, not duckdns_subdomain, is the "a real cert exists"
    # signal — duckdns_subdomain is set as soon as a subdomain INTENT is
    # recorded during Phase 2 onboarding, long before certbot ever runs.
    # A project that recorded a subdomain but never reached Phase 3 has no
    # real cert to delete; the old check let it reach here and fire a real
    # `certbot delete` for a cert-name certbot has never heard of.
    if not project["cert_expiry_date"]:
        raise actions.ActionError(
            f"{project['name']} never had a certificate issued (Phase 3 wasn't run) — nothing to delete. "
            f"Clear its recorded subdomain via 'delete forever' directly."
        )
    cert_name = f"{project['duckdns_subdomain']}.duckdns.org"
    command = f"sudo -n /usr/bin/certbot delete --cert-name {shlex.quote(cert_name)} --non-interactive"
    return actions.create_preview_row(actor, project, command, cert_name, "decommission_delete_cert")


def finalize_delete_cert(project_id: int, actor: str):
    with get_conn() as conn:
        conn.execute("UPDATE projects SET duckdns_subdomain = NULL, cert_expiry_date = NULL WHERE id = ?", (project_id,))
    log_action(actor=actor, action="cert_record_cleared", target=str(project_id))
