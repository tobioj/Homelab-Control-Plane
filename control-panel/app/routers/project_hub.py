"""Per-project 'hub' page — a consolidated detail view reached by clicking
a project's card on the dashboard. Visual/information-architecture layer
only: every piece of data and every action button here already exists
elsewhere (dashboard's live service status, onboarding.build_deployment_summary,
the existing decommission/lifecycle preview routes) — this page just brings
them together in one place instead of scattering them across the dashboard
row, the wizard summary page, and the recommission page, mirroring how
Render.com's per-service page is the single hub for a service's status,
logs, and settings rather than a flat list."""

import html
import shlex

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import json
import urllib.parse

from app.auth import require_login, require_admin
from app.db import get_conn
from app.config import settings
from app import ssh_client, onboarding, actions, github_actions, observability, metrics, deploy_plan, safety

router = APIRouter(prefix="/projects")
templates = Jinja2Templates(directory="app/templates")

# Same set build_deployment_summary is only meaningful for — see
# wizard.py's summary_page route, mirrored here rather than imported since
# it's a one-line literal, not shared logic worth factoring out.
_SUMMARY_READY_STEPS = ("onboarding_complete", "dns_checked", "cert_issued", "nginx_installed", "https_live")

LOG_TAIL_LINES = 200


def _error(e) -> HTMLResponse:
    # Matches the inline-HTMLResponse convention every other router in
    # this codebase uses (wizard.py, lifecycle.py) rather than a Jinja
    # template — there's no error.html anywhere in this app.
    return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)


@router.get("/{project_id}", response_class=HTMLResponse)
async def project_hub(request: Request, project_id: int, user=Depends(require_login)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return _error(f"No project with id {project_id}")
    project = dict(row)

    # One SSH round trip for status + memory + restarts + uptime across every
    # unit (replaces the old per-unit unit_status loop). Never raises.
    service_labels = metrics.service_labels(project)
    try:
        project["metrics"] = await metrics.get_project_metrics(project)
    except Exception as e:  # noqa: BLE001 - hub must render even if VM 100 is unreachable
        project["metrics"] = {label: {"status": f"unreachable", "sub": "", "restarts": "—", "mem": "n/a", "uptime": "—"} for label in service_labels}

    with get_conn() as conn:
        redis_rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM redis_ledger WHERE project_id = ? ORDER BY idx", (project_id,)
            ).fetchall()
        ]
        # Activity timeline — built from pending_actions (clean project_name +
        # real outcomes; audit_log has no project_id and an inconsistent
        # target, so it's not the spine). Newest first.
        activity = [
            dict(r) for r in conn.execute(
                "SELECT action_type, status, actor, created_at, executed_at, exit_code, target_unit "
                "FROM pending_actions WHERE project_name = ? ORDER BY datetime(created_at) DESC LIMIT 40",
                (project["name"],),
            ).fetchall()
        ]
        # Merge in autonomous cert renewals (they bypass pending_actions by
        # design — see dns_cert.renew_cert_if_due — and only log to audit_log).
        for r in conn.execute(
            "SELECT ts, action, details, result FROM audit_log "
            "WHERE actor = 'system-renewal' AND (target = ? OR target LIKE ?) ORDER BY ts DESC LIMIT 10",
            (project["name"], f"%@{project['name']}"),
        ).fetchall():
            activity.append({
                "action_type": r["action"], "status": (r["result"] or "").split(":")[0] or "system",
                "actor": "system-renewal", "created_at": r["ts"], "executed_at": r["ts"],
                "exit_code": None, "target_unit": r["details"],
            })
    activity.sort(key=lambda a: a["created_at"] or "", reverse=True)

    summary = None
    if project["onboarding_step"] in _SUMMARY_READY_STEPS:
        try:
            summary = await onboarding.build_deployment_summary(project)
        except actions.ActionError:
            summary = None

    obs_status = observability.check_observability_status(project_id)

    # "Search logs in Grafana" deep-link — only meaningful once this project
    # actually ships logs to Loki (obs_status.loki_enabled). The exact stream
    # label is owned by the repo's own logging config, so seed a best-effort
    # query the operator can refine in Explore.
    grafana_url = None
    if obs_status.get("loki_enabled"):
        left = json.dumps({
            "datasource": "loki",
            "queries": [{"refId": "A", "expr": '{job="%s"}' % project["name"]}],
            "range": {"from": "now-6h", "to": "now"},
        })
        grafana_url = f"{settings.GRAFANA_BASE_URL.rstrip('/')}/explore?left={urllib.parse.quote(left)}"

    return templates.TemplateResponse(
        "project_hub.html",
        {
            "request": request, "user": user, "project": project,
            "service_labels": service_labels, "redis_rows": redis_rows,
            "summary": summary, "obs_status": obs_status, "activity": activity,
            "grafana_url": grafana_url,
        },
    )


def _enabled_services(project: dict):
    services = ["gunicorn"]
    if project["uses_celery"]:
        services.append("celery")
    if project["uses_celerybeat"]:
        services.append("celerybeat")
    return services


def _get_project_or_error(project_id: int):
    """Not async — used by both the page and the polled-fragment route,
    neither of which needs anything beyond the plain row."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"No project with id {project_id}")
    return dict(row)


@router.get("/{project_id}/logs/{service}", response_class=HTMLResponse)
async def logs_page(request: Request, project_id: int, service: str, user=Depends(require_login)):
    try:
        project = _get_project_or_error(project_id)
        # Only a service this project actually has — never let the path
        # parameter reach journalctl unvalidated (same discipline as
        # actions._validate_service_for_project, just against this
        # project's own real service list rather than a fixed allowlist).
        if service not in _enabled_services(project):
            raise ValueError(f"{project['name']} does not have a '{service}' service")
    except ValueError as e:
        return _error(e)
    return templates.TemplateResponse(
        "project_logs.html", {"request": request, "user": user, "project": project, "service": service}
    )


@router.get("/{project_id}/logs/{service}/tail", response_class=HTMLResponse)
async def logs_tail(project_id: int, service: str, user=Depends(require_login)):
    """The htmx-polled fragment — plain journalctl snapshot, not a real
    streaming/follow connection (this app is a synchronous request/response
    server-rendered tool, no websocket infra exists anywhere else in it).
    Polled every few seconds by project_logs.html via hx-trigger, each
    request an independent bounded `journalctl -n N --no-pager` snapshot —
    same 'never a detached/backgrounded remote process' discipline as
    everything else in this app (see dns_cert.py's certbot-run docstring).
    Confirmed live: journalctl works for the deploy user on VM 100 without
    sudo (systemd-journal group membership), no new sudoers grant needed."""
    try:
        project = _get_project_or_error(project_id)
        if service not in _enabled_services(project):
            return HTMLResponse("<pre class='error'>Not a valid service for this project.</pre>", status_code=400)
    except ValueError as e:
        return HTMLResponse(f"<pre class='error'>{e}</pre>", status_code=400)

    unit = f"{service}@{project['name']}"
    result = await ssh_client.run(
        f"journalctl -u {shlex.quote(unit)} -n {LOG_TAIL_LINES} --no-pager 2>&1", host=project["vm_ip"], timeout=15
    )
    body = html.escape(result["stdout"] or "(no log output)")
    return HTMLResponse(f"<pre id='log-content'>{body}</pre>")


def _inline_banner(ok: bool, msg: str) -> str:
    cls = "ok" if ok else "error"
    icon = "✓" if ok else "✗"
    return (
        f"<div class='inline-banner inline-banner-{cls}'>"
        f"<span>{icon} {html.escape(msg)}</span>"
        f"<button type='button' class='banner-x' title='Dismiss' "
        f"onclick=\"this.closest('.inline-banner').remove()\">✕</button></div>"
    )


@router.post("/{project_id}/restart/{service}", response_class=HTMLResponse)
async def restart_inline(project_id: int, service: str, user=Depends(require_admin)):
    """Inline restart (htmx) — stays on the hub page and returns a dismissible
    result banner instead of navigating to a preview then a result page.
    Still goes through the full audit pipeline (create_preview builds the exact
    command + a pending_actions row, confirm_and_run executes + records the
    result) — the only thing skipped for this lowest-stakes, idempotent action
    is the separate preview *page*, per the operator's UX request."""
    try:
        project = _get_project_or_error(project_id)
        token = actions.create_preview(actor=user["username"], project=project, service=service)
        row = await actions.confirm_and_run(token, actor=user["username"])
    except (ValueError, actions.ActionError) as e:
        return HTMLResponse(_inline_banner(False, f"{service}: {e}"))
    ok = row["status"] == "executed"
    if ok:
        msg = f"{service} restarted"
    else:
        msg = f"{service} restart failed: {(row['stderr'] or '').strip()[:200] or 'see logs'}"
    return HTMLResponse(_inline_banner(ok, msg))


@router.get("/{project_id}/redeploy", response_class=HTMLResponse)
async def redeploy_page(request: Request, project_id: int, user=Depends(require_admin)):
    """Push-to-deploy preview: shows the incoming commits (read-only fetch)
    and the exact command sequence, before the operator prepares the
    confirmable action. primary-store is refused."""
    try:
        project = _get_project_or_error(project_id)
        safety.assert_project_not_protected(project["name"])
        if project["status"] != "running":
            raise actions.ActionError(f"{project['name']} is '{project['status']}' — only a running project can be redeployed.")
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    incoming = await deploy_plan.incoming_commits(project)
    command = deploy_plan.redeploy_command(project)
    return templates.TemplateResponse(
        "redeploy.html",
        {"request": request, "user": user, "project": project, "incoming": incoming, "command": command},
    )


@router.post("/{project_id}/redeploy")
async def redeploy_prepare(project_id: int, user=Depends(require_admin)):
    """Creates the confirmable pending_actions row, then hands off to the
    standard /actions/{token} preview→confirm (the generic confirm runs the
    command and records the result — no bespoke confirm/finalize needed)."""
    try:
        project = _get_project_or_error(project_id)
        token = deploy_plan.create_redeploy_preview(actor=user["username"], project=project)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.get("/{project_id}/github-actions", response_class=HTMLResponse)
async def github_actions_page(
    request: Request, project_id: int, external_host: str = "", external_port: int = 22, user=Depends(require_admin)
):
    try:
        project = _get_project_or_error(project_id)
    except ValueError as e:
        return _error(e)
    workflow_yaml = None
    if external_host:
        workflow_yaml = github_actions.render_deploy_workflow(project, external_host, external_port)
    return templates.TemplateResponse(
        "github_actions.html",
        {
            "request": request, "user": user, "project": project,
            "external_host": external_host, "external_port": external_port,
            "workflow_yaml": workflow_yaml, "sudoers_snippet": github_actions.sudoers_snippet(project),
            "ci_key_result": None,
        },
    )


@router.post("/{project_id}/github-actions/ci-key", response_class=HTMLResponse)
async def github_actions_generate_ci_key(
    request: Request, project_id: int,
    external_host: str = Form(""), external_port: int = Form(22),
    user=Depends(require_admin),
):
    try:
        project = _get_project_or_error(project_id)
        ci_key_result = await github_actions.generate_ci_key(project, actor=user["username"])
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    workflow_yaml = github_actions.render_deploy_workflow(project, external_host, external_port) if external_host else None
    return templates.TemplateResponse(
        "github_actions.html",
        {
            "request": request, "user": user, "project": project,
            "external_host": external_host, "external_port": external_port,
            "workflow_yaml": workflow_yaml, "sudoers_snippet": github_actions.sudoers_snippet(project),
            "ci_key_result": ci_key_result,
        },
    )


@router.get("/{project_id}/observability", response_class=HTMLResponse)
async def observability_wizard(request: Request, project_id: int, user=Depends(require_login)):
    """Dedicated, standalone guided setup for observability — deliberately
    SEPARATE from the Phase 1/2/3 onboarding wizard (project.onboarding_step
    never changes here), since not every project wants this and it can be
    set up any time, not just during initial onboarding. Every step's
    status is computed LIVE (config prereqs, repo readiness, saved-vs-
    deployed sync) rather than tracked in a persisted 'step' column —
    there's no ordering ambiguity to track since each check is independently
    re-verifiable at any time."""
    try:
        project = _get_project_or_error(project_id)
    except ValueError as e:
        return _error(e)
    prereqs = await observability.check_config_prereqs()
    repo_readiness = await observability.check_repo_readiness(project)
    obs_status = observability.check_observability_status(project_id)
    sync = await observability.check_deployment_sync(project) if any(obs_status.values()) else None
    return templates.TemplateResponse(
        "observability_wizard.html",
        {
            "request": request, "user": user, "project": project,
            "prereqs": prereqs, "repo_readiness": repo_readiness,
            "obs_status": obs_status, "sync": sync,
        },
    )


@router.post("/{project_id}/observability/enable")
def observability_enable_preview(project_id: int, user=Depends(require_admin)):
    """Preview-only — creates a pending_actions row describing what will
    happen (GlitchTip project creation, shared Loki creds, ntfy topic
    grant) but does nothing until the operator explicitly confirms on the
    next page, same as every other mutating action in this app."""
    try:
        project = _get_project_or_error(project_id)
        token = observability.create_enable_preview(actor=user["username"], project=project)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{token}/confirm", response_class=HTMLResponse)
async def observability_confirm(request: Request, token: str, user=Depends(require_admin)):
    try:
        row = await observability.confirm_enable(token, actor=user["username"])
    except actions.ActionError as e:
        return _error(e)
    with get_conn() as conn:
        proj_row = conn.execute("SELECT * FROM projects WHERE name = ?", (row["project_name"],)).fetchone()
    proj = dict(proj_row) if proj_row else None
    return templates.TemplateResponse(
        "action_result.html", {"request": request, "user": user, "action": row, "project": proj}
    )
