from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.db import get_conn
from app import actions

router = APIRouter(prefix="/actions")
templates = Jinja2Templates(directory="app/templates")


def _get_project(name: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"No such project: {name}")
        return dict(row)


@router.post("/preview")
def preview_create(request: Request, project: str = Form(...), service: str = Form(...), user=Depends(require_admin)):
    try:
        proj = _get_project(project)
        token = actions.create_preview(actor=user["username"], project=proj, service=service)
    except (ValueError, actions.ActionError) as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.get("/{token}", response_class=HTMLResponse)
def preview_show(request: Request, token: str, user=Depends(require_admin)):
    row = actions.get_pending(token)
    if row is None:
        return HTMLResponse("<p class='error'>No such pending action.</p><p><a href='/'>Back</a></p>", status_code=404)
    return templates.TemplateResponse("action_preview.html", {"request": request, "user": user, "action": row})


@router.post("/{token}/confirm", response_class=HTMLResponse)
async def preview_confirm(request: Request, token: str, user=Depends(require_admin)):
    try:
        row = await actions.confirm_and_run(token, actor=user["username"])
    except actions.ActionError as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)
    try:
        proj = _get_project(row["project_name"])
    except ValueError:
        proj = None
    return templates.TemplateResponse("action_result.html", {"request": request, "user": user, "action": row, "project": proj})


@router.post("/{token}/cancel")
def preview_cancel(request: Request, token: str, user=Depends(require_admin)):
    row = actions.get_pending(token)
    try:
        actions.cancel(token, actor=user["username"])
    except actions.ActionError as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)
    # Return to the project the action belonged to, not the homepage — the
    # operator is working on a specific project and expects to land back there.
    if row:
        try:
            proj = _get_project(row["project_name"])
            return RedirectResponse(f"/projects/{proj['id']}", status_code=303)
        except ValueError:
            pass
    return RedirectResponse("/", status_code=303)
