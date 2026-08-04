from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.db import get_conn
from app import actions, decommission

router = APIRouter(prefix="/decommission")
templates = Jinja2Templates(directory="app/templates")


def _get_project(name: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"No such project: {name}")
        return dict(row)


@router.post("/{project_name}/preview-stop")
def preview_stop(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = decommission.create_stop_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_name}/preview-delete-files")
async def preview_delete_files(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = await decommission.create_delete_files_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_name}/preview-delete-cert")
def preview_delete_cert(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = decommission.create_delete_cert_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{token}/confirm", response_class=HTMLResponse)
async def confirm(request: Request, token: str, user=Depends(require_admin)):
    try:
        row = await actions.confirm_and_run(token, actor=user["username"])
    except actions.ActionError as e:
        return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)

    try:
        proj = _get_project(row["project_name"])
    except ValueError:
        proj = None

    if row["status"] == "executed" and proj:
        if row["action_type"] == "decommission_stop":
            decommission.finalize_stop(token, actor=user["username"], project_id=proj["id"])
        elif row["action_type"] == "decommission_delete_cert":
            decommission.finalize_delete_cert(project_id=proj["id"], actor=user["username"])

    return templates.TemplateResponse("action_result.html", {"request": request, "user": user, "action": row, "project": proj})
