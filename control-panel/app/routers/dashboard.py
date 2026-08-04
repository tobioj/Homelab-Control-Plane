import subprocess

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_login
from app.db import get_conn
from app import ssh_client

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _local_docker_status():
    """VM 103 IS this machine — no SSH needed, just shell out locally.
    Read-only, for situational awareness per §3.2 — this tool never offers
    'manage' actions on these containers."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        lines = [l.split("\t") for l in out.stdout.strip().splitlines() if l]
        return [{"name": n, "status": s} for n, s in lines]
    except Exception as e:
        return [{"name": "error", "status": str(e)}]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(require_login)):
    with get_conn() as conn:
        projects = [dict(r) for r in conn.execute("SELECT * FROM projects").fetchall()]
        # External-uptime state from the prober (app/uptime.py). Cheap single
        # query; attached per project as a health dot. Empty until the prober
        # has run at least once.
        health = {r["project_id"]: dict(r) for r in conn.execute("SELECT * FROM project_health").fetchall()}

    for p in projects:
        p["health"] = health.get(p["id"])
        services = {"gunicorn": f"gunicorn@{p['name']}"}
        if p["uses_celery"]:
            services["celery"] = f"celery@{p['name']}"
        if p["uses_celerybeat"]:
            services["celerybeat"] = f"celerybeat@{p['name']}"

        live_status = {}
        for label, unit in services.items():
            try:
                # Short-TTL cache — the dashboard re-checks every project's
                # every service on every page load; project_hub.py and the
                # wizard's own health checks use ssh_client.unit_status()
                # directly (always live) since those pages are visited
                # specifically expecting fresh truth right after an action.
                live_status[label] = await ssh_client.cached_unit_status(unit, host=p["vm_ip"])
            except Exception as e:
                live_status[label] = f"unreachable ({e})"
        p["live_status"] = live_status

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "projects": projects,
        },
    )


@router.get("/infra", response_class=HTMLResponse)
async def infra(request: Request, user=Depends(require_login)):
    """VM 103's own Docker containers — moved off the main dashboard onto
    its own page (linked from the sidebar) so the project grid isn't
    sharing space with unrelated host-level infrastructure info."""
    return templates.TemplateResponse(
        "infra.html", {"request": request, "user": user, "docker_containers": _local_docker_status()}
    )
