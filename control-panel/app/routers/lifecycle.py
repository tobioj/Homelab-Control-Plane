from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.db import get_conn
from app import actions, lifecycle, safety

router = APIRouter(prefix="/lifecycle")
templates = Jinja2Templates(directory="app/templates")


def _get_project(name: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"No such project: {name}")
        return dict(row)


def _error(e) -> HTMLResponse:
    return HTMLResponse(f"<p class='error'>{e}</p><p><a href='/'>Back</a></p>", status_code=400)


@router.post("/{project_name}/preview-pause")
def preview_pause(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = lifecycle.create_pause_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_name}/preview-resume")
def preview_resume(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = lifecycle.create_resume_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.get("/{project_name}/recommission", response_class=HTMLResponse)
async def recommission_page(request: Request, project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        # Caught live during testing: this read-only info page had no
        # protection or status check at all, so it happily rendered
        # "ready to recommission" for primary-store (status 'running', not
        # even decommissioned) purely because check_recommission_readiness
        # itself is unconditional. No actual mutation could ever have
        # succeeded — create_recommission_preview/reallocate_preview both
        # already refuse protected/non-decommissioned projects — but this
        # page shouldn't show misleading readiness info in the first place.
        safety.assert_project_not_protected(proj["name"])
        if proj["status"] != "decommissioned":
            raise actions.ActionError(f"{proj['name']} is '{proj['status']}', not 'decommissioned' — nothing to recommission")
        readiness = await lifecycle.check_recommission_readiness(proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return templates.TemplateResponse(
        "lifecycle_recommission.html",
        {"request": request, "user": user, "project": proj, "readiness": readiness},
    )


@router.post("/{project_name}/recommission-confirm")
async def recommission_confirm_step(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = await lifecycle.create_recommission_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_name}/recommission-reallocate")
async def recommission_reallocate_step(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = await lifecycle.create_recommission_reallocate_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{project_name}/preview-delete-forever")
async def preview_delete_forever(project_name: str, user=Depends(require_admin)):
    try:
        proj = _get_project(project_name)
        token = await lifecycle.create_delete_forever_preview(actor=user["username"], project=proj)
    except (ValueError, actions.ActionError) as e:
        return _error(e)
    return RedirectResponse(f"/actions/{token}", status_code=303)


@router.post("/{token}/confirm", response_class=HTMLResponse)
async def confirm(request: Request, token: str, user=Depends(require_admin)):
    try:
        pending = actions.get_pending(token)
        if pending is None:
            raise actions.ActionError("No such pending action")
        if pending["action_type"] == "lifecycle_recommission_reallocate":
            row = await lifecycle.confirm_recommission_reallocate(token, actor=user["username"])
        elif pending["action_type"] == "lifecycle_delete_forever":
            row = await lifecycle.confirm_delete_forever(token, actor=user["username"])
            # The project row is gone by now — never look it up again.
            return templates.TemplateResponse("action_result.html", {"request": request, "user": user, "action": row})
        else:
            row = await actions.confirm_and_run(token, actor=user["username"])
    except actions.ActionError as e:
        return _error(e)

    try:
        proj = _get_project(row["project_name"])
    except ValueError:
        proj = None

    if row["status"] == "executed" and proj:
        finalize_map = {
            "lifecycle_pause": lifecycle.finalize_pause,
            "lifecycle_resume": lifecycle.finalize_resume,
            "lifecycle_recommission": lifecycle.finalize_recommission,
        }
        finalize_fn = finalize_map.get(row["action_type"])
        if finalize_fn:
            finalize_fn(proj["id"], actor=user["username"])

    return templates.TemplateResponse("action_result.html", {"request": request, "user": user, "action": row, "project": proj})
