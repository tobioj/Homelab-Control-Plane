"""Defense-in-depth denylist, independent of what sudoers technically
permits. Sudoers wildcards (e.g. `systemctl stop gunicorn@*`) necessarily
also match primary-store — sudoers has no concept of "which project" beyond
the unit-name pattern. This module is the actual safety boundary for
destructive actions: checked in Python before any command that deletes
files, unlinks+disables a project, or revokes a certificate is ever built,
regardless of what the orchestrator key's sudoers grants would technically
allow it to run.

Deliberately NOT applied to plain service restarts — restarting
primary-store's own services through this tool is an expected, ordinary
action (it's what the existing CI pipeline already does routinely). This
denylist exists specifically for irreversible or infra-disrupting actions:
decommission, file deletion, cert revocation.
"""

from app.actions import ActionError
from app.db import get_conn

PROTECTED_PROJECT_NAMES = {"primary-store"}

# Filenames that must never be a cp/rm target, independent of project name —
# the shared systemd templates every project's gunicorn/celery/celerybeat
# instance falls back to if no concrete per-project unit file exists, and
# the nginx catchall that drops unrecognized Host headers.
PROTECTED_FILENAMES = {
    "gunicorn@.service",
    "celery@.service",
    "celerybeat@.service",
    "00-catchall",
}


class ProtectedTargetError(ActionError):
    pass


def assert_project_not_protected(project_name: str):
    if project_name in PROTECTED_PROJECT_NAMES:
        raise ProtectedTargetError(
            f"'{project_name}' is a protected project — destructive actions "
            f"(decommission, file deletion, cert revocation) are refused at "
            f"the application layer, independent of sudoers permissions."
        )


def assert_filename_not_protected(filename: str):
    if filename in PROTECTED_FILENAMES:
        raise ProtectedTargetError(
            f"'{filename}' is a protected shared file and cannot be targeted "
            f"by a destructive action."
        )


def assert_subdomain_available(subdomain: str, project_id: int):
    """The project-name check above stops a project literally named
    'primary-store' from being touched, but says nothing about a
    DIFFERENTLY-named project being configured with primary-store's real
    DuckDNS subdomain — which would sail straight past that check and let
    Phase 3 make a real DuckDNS API call / request a real cert for the live
    domain. Found live during Phase 3 testing (a disposable test project
    was accidentally given primary-store's real subdomain value, caught
    only by an external permission check before any request left the box —
    this function is the actual fix). Checked both where a subdomain is
    first recorded (onboarding.set_subdomain_intent) and again immediately
    before any live DuckDNS/certbot call, independent of whether the
    conflicting project happens to also be in PROTECTED_PROJECT_NAMES —
    two projects sharing one subdomain is a bug regardless of which one, if
    either, is "protected"."""
    if not subdomain:
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM projects WHERE duckdns_subdomain = ? AND id != ?",
            (subdomain, project_id),
        ).fetchone()
    if row is not None:
        raise ProtectedTargetError(
            f"Subdomain '{subdomain}' is already recorded against project "
            f"'{row['name']}' — refusing to let a different project "
            f"(id {project_id}) claim or act on the same DuckDNS subdomain."
        )
