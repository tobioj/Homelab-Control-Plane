"""Optional, per-project observability opt-in — GlitchTip (Sentry-compatible
error tracking), Loki (log shipping), and ntfy (push alerts), all part of
the existing monitoring stack on VM 103 itself (~/dev-infra/, see
INFRA.md — read, never modified by this module). NOT part of the mandatory
onboarding wizard — this is a standalone action on a project's hub page,
enabled any time, for any subset of projects the operator chooses.

Deliberately narrow in what it automates, matching this app's "ask, don't
guess" discipline:
  - GlitchTip: creates one PROJECT (and reads back its DSN) under an
    already-existing organization/team the operator set up themselves
    (GLITCHTIP_ORG_SLUG/GLITCHTIP_TEAM_SLUG in .env) — never creates or
    modifies an org or team, never touches a project it didn't create.
  - Loki: only ever fills in the ALREADY-SHARED push credentials
    (LOKI_PUSH_URL/USER/PASSWORD, same for every project — confirmed live
    against ~/dev-infra/loki-proxy: a single nginx basic-auth pair, Loki's
    own `auth_enabled: false` means there is no real per-tenant isolation
    to configure). Real per-project SEPARATION in Grafana/Loki depends on
    the deployed app's own logging code tagging its stream (confirmed live
    by reading primary-store's own settings.py: `loki_tags={"job": "...",
    ...}` is hardcoded in ITS repo, not read from an env var) — that's the
    target repo's own responsibility, not something this tool can set from
    outside. Documented as a repo-readiness expectation, not enforced.
  - ntfy: creates one TOPIC-level ACL grant (anonymous read-write, matching
    the existing `grafana-alerts`/`glitchtip-issues` convention confirmed
    live via `ntfy access`) — never touches ntfy's users, roles, or global
    auth settings.

`primary-store` already has its own real, hand-configured GlitchTip project
under this same organization — every function here calls
safety.assert_project_not_protected() FIRST, same as every other mutating
action in this app, so this integration can never create a duplicate
project for it or touch its existing one.
"""

import asyncio
import json
import shlex
import urllib.error
import urllib.request

from app.db import get_conn
from app.audit import log_action
from app.config import settings
from app import actions, safety, onboarding, ssh_client

DEV_INFRA_DIR = "/home/deploy/dev-infra"

# Every project_env_vars key this integration owns and manages itself. These
# are written by confirm_enable() and must NOT be surfaced as hand-editable
# rows in the env-vars wizard step, nor deleted by its save-reconciliation
# (onboarding.load_editable_env_vars / reconcile_env_vars both consult this) —
# otherwise an ordinary env-vars re-save would silently wipe a live project's
# observability config. Single source of truth for that set.
MANAGED_ENV_KEYS = frozenset({
    "SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_TRACES_SAMPLE_RATE",
    "LOKI_PUSH_URL", "LOKI_PUSH_USER", "LOKI_PUSH_PASSWORD", "NTFY_TOPIC",
})


async def _run_in_thread(fn, *args):
    """asyncio.to_thread() needs Python 3.9+ — this venv runs 3.8 (real
    bug hit live: AttributeError on the first actual test of this module).
    loop.run_in_executor(None, ...) is the 3.8-compatible equivalent."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


def _glitchtip_request(method: str, path: str, body: dict = None) -> dict:
    """Synchronous — wrapped in _run_in_thread() by callers. Plain
    stdlib urllib rather than adding a new dependency (requests/httpx
    aren't installed anywhere in this venv) — this app already leans on
    the standard library wherever reasonable (sqlite3, asyncssh aside)."""
    url = f"{settings.GLITCHTIP_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {settings.GLITCHTIP_API_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise actions.ActionError(f"GlitchTip API {method} {path} failed: {e.code} {detail[:300]}")
    except urllib.error.URLError as e:
        raise actions.ActionError(f"Could not reach GlitchTip at {settings.GLITCHTIP_BASE_URL}: {e.reason}")


async def create_glitchtip_project(project_name: str) -> dict:
    """Creates ONE GlitchTip project under the operator's already-existing
    org/team, returns {"slug": ..., "dsn": ...}. Never touches any
    existing project — GlitchTip's own API only ever creates new ones here,
    this function is never called with a project that already has a
    recorded SENTRY_DSN (see check_observability_status/create_enable_preview's
    guard). Exact request/response shape follows GlitchTip's documented
    Sentry-API compatibility — NOT yet exercised against a real token as of
    when this was written; the first real "Enable observability" click is
    also the first live test of this exact call shape."""
    if not settings.GLITCHTIP_API_TOKEN or not settings.GLITCHTIP_ORG_SLUG or not settings.GLITCHTIP_TEAM_SLUG:
        raise actions.ActionError(
            "GLITCHTIP_API_TOKEN, GLITCHTIP_ORG_SLUG, and GLITCHTIP_TEAM_SLUG must all be set in .env "
            "before observability can be enabled for any project."
        )

    def _do():
        created = _glitchtip_request(
            "POST",
            f"/api/0/teams/{settings.GLITCHTIP_ORG_SLUG}/{settings.GLITCHTIP_TEAM_SLUG}/projects/",
            {"name": project_name},
        )
        slug = created["slug"]
        keys = _glitchtip_request("GET", f"/api/0/projects/{settings.GLITCHTIP_ORG_SLUG}/{slug}/keys/")
        if not keys:
            raise actions.ActionError(f"GlitchTip project '{slug}' was created but has no DSN key yet.")
        dsn = keys[0]["dsn"]["public"]
        return {"slug": slug, "dsn": dsn}

    return await _run_in_thread(_do)


async def create_ntfy_topic(project_name: str) -> str:
    """Grants anonymous read-write on one dedicated topic, matching the
    existing grafana-alerts/glitchtip-issues convention (confirmed live via
    `docker compose exec ntfy ntfy access` — see INFRA.md). Runs locally on
    VM 103 (this machine IS dev-infra, no SSH needed) — same
    'shell out locally, never over SSH, for anything VM-103-local' pattern
    dashboard.py's _local_docker_status() already established. NOT yet
    exercised live as of when this was written."""
    topic = f"project-{project_name}"
    proc = await asyncio.create_subprocess_shell(
        f"cd {shlex.quote(DEV_INFRA_DIR)} && docker compose exec -T ntfy ntfy access everyone {shlex.quote(topic)} rw",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise actions.ActionError(f"Failed to grant ntfy topic access: {stderr.decode(errors='replace')[:300]}")
    return topic


def _read_shared_loki_credentials() -> dict:
    """Reads LOKI_PUSH_USER/LOKI_PUSH_PASSWORD straight out of dev-infra's
    own .env — legitimate here specifically because this app and dev-infra
    run as the same user (deploy) on the SAME machine (VM 103 IS dev-infra,
    per BRIEF.md §2.4); this isn't reaching into another system's secrets,
    it's reading this machine's own already-accessible shared infra config.
    Reuses onboarding.parse_uploaded_env's existing line-parser (skip
    blank/comment lines, key=value) rather than adding a new one."""
    try:
        with open(f"{DEV_INFRA_DIR}/.env") as f:
            content = f.read()
    except OSError as e:
        raise actions.ActionError(f"Could not read dev-infra's .env for shared Loki credentials: {e}")
    values = onboarding.parse_uploaded_env(content)
    user, password = values.get("LOKI_PUSH_USER"), values.get("LOKI_PUSH_PASSWORD")
    if not user or not password:
        raise actions.ActionError("LOKI_PUSH_USER/LOKI_PUSH_PASSWORD not found in dev-infra's .env.")
    return {"user": user, "password": password}


async def check_config_prereqs() -> dict:
    """Control-panel-wide (not per-project) — whether GLITCHTIP_API_TOKEN/
    ORG_SLUG/TEAM_SLUG are set in THIS app's own .env. One-time setup, same
    shape as DUCKDNS_TOKEN's prerequisite for Phase 3."""
    return {
        "token_set": bool(settings.GLITCHTIP_API_TOKEN),
        "org_set": bool(settings.GLITCHTIP_ORG_SLUG),
        "team_set": bool(settings.GLITCHTIP_TEAM_SLUG),
    }


async def check_repo_readiness(project: dict) -> dict:
    """Best-effort, grep-based, same 'inspect the real repo, never guess'
    discipline as onboarding.detect_required_env_vars — does this
    project's OWN code already reference SENTRY_DSN / LOKI_PUSH_URL at
    all? A repo that's never heard of these vars won't do anything useful
    with them even after this wizard saves and deploys them — that's a
    real repo-code requirement (see primary-store's own settings.py for
    the reference pattern: DSN-guarded Sentry init, python-logging-loki
    wired to LOKI_PUSH_URL), not something this tool can add for the
    operator. Not required to proceed — informational only."""
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    result = await ssh_client.run(
        f"grep -rlE 'SENTRY_DSN|LOKI_PUSH_URL' --include={shlex.quote('*.py')} {shlex.quote(app_dir)} 2>/dev/null || true",
        host=project["vm_ip"], timeout=30,
    )
    found_in = [p.strip() for p in result["stdout"].splitlines() if p.strip()]
    return {"referenced": bool(found_in), "files": found_in}


def _strip_shell_quotes(value: str) -> str:
    """Values written by this app's own build_env_file_content() are
    shlex.quote()-wrapped (single-quoted) on disk; values edited directly
    on VM 100 by hand (confirmed live: this happened to `app-beta` mid-session,
    outside this app entirely) are NOT. Strip a single matching pair of
    quotes either way before comparing, so the sync check isn't fooled by
    quoting style alone."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


async def check_deployment_sync(project: dict) -> dict:
    """Live comparison: does VM 100's ACTUALLY DEPLOYED .env match what
    this app has saved for SENTRY_DSN/LOKI_PUSH_URL/NTFY_TOPIC right now?
    'Enable observability' only saves to this app's own DB — the operator
    still has to re-run 'Write .env' + restart services afterward for it
    to take effect, and it's easy to forget that's a separate step. This
    is a live, verifiable answer instead of just trusting a step 'ran'.

    Three real states, not two — real gap found live: `app-beta`'s deployed
    .env was found to have a THIRD value (copied from `app-alpha`'s real DSN,
    edited directly on VM 100 outside this app entirely) that matched
    neither what this app has saved for `app-beta` NOR nothing at all. A
    plain in-sync/out-of-sync boolean would have called that 'not deployed
    yet', which is misleading — it IS deployed, just to something this
    app doesn't know about and didn't put there."""
    dest = f"/srv/apps/{project['name']}"
    app_dir = f"{dest}/{project['app_subdir']}" if project["app_subdir"] else dest
    result = await ssh_client.run(
        f"grep -E '^(SENTRY_DSN|LOKI_PUSH_URL|NTFY_TOPIC)=' {shlex.quote(app_dir + '/.env')} 2>/dev/null || true",
        host=project["vm_ip"], timeout=15,
    )
    deployed_raw = onboarding.parse_uploaded_env(result["stdout"])
    deployed = {k: _strip_shell_quotes(v) for k, v in deployed_raw.items()}

    with get_conn() as conn:
        saved_rows = conn.execute(
            "SELECT key, value FROM project_env_vars WHERE project_id = ? AND key IN "
            "('SENTRY_DSN', 'LOKI_PUSH_URL', 'NTFY_TOPIC')", (project["id"],)
        ).fetchall()
    from app import crypto
    saved = {r["key"]: crypto.decrypt(r["value"]) for r in saved_rows}

    keys = ["SENTRY_DSN", "LOKI_PUSH_URL", "NTFY_TOPIC"]
    out = {}
    for k in keys:
        if k not in saved and k not in deployed:
            state = "not_saved"
        elif k in deployed and deployed.get(k) == saved.get(k):
            state = "in_sync"
        elif k in deployed:
            state = "deployed_but_different"  # real value on disk, doesn't match what this app has saved
        else:
            state = "saved_not_deployed"
        out[k] = {"saved": k in saved, "deployed": state == "in_sync", "state": state}
    return out


def check_observability_status(project_id: int) -> dict:
    """Read-only. Whether this project already has observability vars
    saved — used to show current state on the hub page and to refuse a
    duplicate 'Enable' (which would otherwise create a second, orphaned
    GlitchTip project under the same name)."""
    with get_conn() as conn:
        rows = {
            r["key"]: True for r in conn.execute(
                "SELECT key FROM project_env_vars WHERE project_id = ? AND key IN "
                "('SENTRY_DSN', 'LOKI_PUSH_URL', 'NTFY_TOPIC')", (project_id,)
            ).fetchall()
        }
    return {
        "sentry_enabled": "SENTRY_DSN" in rows,
        "loki_enabled": "LOKI_PUSH_URL" in rows,
        "ntfy_enabled": "NTFY_TOPIC" in rows,
    }


def create_enable_preview(actor: str, project: dict) -> str:
    """Preview only — no real command runs until confirm. Not a shell
    command (same shape as onboarding.create_write_env_preview): the
    'command' shown is a plain description of what will happen, since the
    real work is an HTTPS API call plus a local docker exec, not SSH."""
    safety.assert_project_not_protected(project["name"])
    status = check_observability_status(project["id"])
    if status["sentry_enabled"] or status["loki_enabled"] or status["ntfy_enabled"]:
        raise actions.ActionError(
            f"{project['name']} already has observability vars saved — re-enabling would create a "
            f"second, orphaned GlitchTip project. Nothing to do."
        )
    placeholder = (
        f"[enable observability for {project['name']}] "
        f"1) create a GlitchTip project under {settings.GLITCHTIP_ORG_SLUG or '<GLITCHTIP_ORG_SLUG not set>'}"
        f"/{settings.GLITCHTIP_TEAM_SLUG or '<GLITCHTIP_TEAM_SLUG not set>'}, save its DSN as SENTRY_DSN "
        f"2) save the shared Loki push credentials as LOKI_PUSH_URL/USER/PASSWORD "
        f"3) grant an ntfy topic 'project-{project['name']}' anonymous read-write, save it as NTFY_TOPIC"
    )
    return actions.create_preview_row(actor, project, placeholder, project["name"], "observability_enable")


async def confirm_enable(token: str, actor: str) -> dict:
    """Bespoke confirm, same shape as onboarding.confirm_write_env — local/
    API work first, then a synthetic result dict for actions.record_result()
    so this rides the same pending_actions/audit trail as every other
    action without inventing new bookkeeping. Re-checks not-already-enabled
    and not-protected at confirm time too, not just preview time."""
    row = actions.load_pending_for_confirm(token)
    with get_conn() as conn:
        project = dict(conn.execute("SELECT * FROM projects WHERE name = ?", (row["project_name"],)).fetchone())

    try:
        safety.assert_project_not_protected(project["name"])
        status = check_observability_status(project["id"])
        if status["sentry_enabled"] or status["loki_enabled"] or status["ntfy_enabled"]:
            raise actions.ActionError(f"{project['name']} already has observability vars saved since the preview.")

        log_action(actor=actor, action="confirm_execute", target=row["target_unit"], details="(enabling observability)")

        glitchtip = await create_glitchtip_project(project["name"])
        ntfy_topic = await create_ntfy_topic(project["name"])
        loki_creds = await _run_in_thread(_read_shared_loki_credentials)

        onboarding.save_env_vars(
            project["id"], actor=actor,
            values={
                "SENTRY_DSN": glitchtip["dsn"],
                "SENTRY_ENVIRONMENT": "production",
                "SENTRY_TRACES_SAMPLE_RATE": "0.1",
                "LOKI_PUSH_URL": settings.LOKI_PUSH_URL,
                "LOKI_PUSH_USER": loki_creds["user"],
                "LOKI_PUSH_PASSWORD": loki_creds["password"],
                "NTFY_TOPIC": ntfy_topic,
            },
        )
        log_action(
            actor=actor, action="observability_enabled", target=project["name"],
            details=f"glitchtip_project={glitchtip['slug']} ntfy_topic={ntfy_topic}",
        )
    except actions.ActionError as e:
        result = {"exit_status": 1, "stdout": "", "stderr": str(e)}
        return actions.record_result(token, row, result)

    result = {
        "exit_status": 0,
        "stdout": (
            f"GlitchTip project '{glitchtip['slug']}' created — SENTRY_DSN saved.\n"
            f"Loki push credentials saved (shared, read from dev-infra's own .env).\n"
            f"ntfy topic '{ntfy_topic}' granted anonymous read-write — subscribe at "
            f"{settings.NTFY_BASE_URL}/{ntfy_topic}\n\n"
            f"These are saved to this project's config but NOT written to VM 100 yet — "
            f"re-run the 'Write .env' step (via the wizard breadcrumb's redo button, or the "
            f"project hub) to actually push them to the deployed .env file, then restart "
            f"gunicorn/celery for the app to pick them up."
        ),
        "stderr": "",
    }
    return actions.record_result(token, row, result)
