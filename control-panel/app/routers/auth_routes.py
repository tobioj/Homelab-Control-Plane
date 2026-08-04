from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import get_conn
from app.auth import verify_password, get_current_user, require_login, hash_password
from app.audit import log_action

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if not row or not verify_password(password, row["password_hash"]):
        log_action(actor=username, action="login_failed", result="denied")
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid username or password"}, status_code=401
        )

    request.session["user_id"] = row["id"]
    log_action(actor=username, action="login", result="ok")
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    user = get_current_user(request)
    if user:
        log_action(actor=user["username"], action="logout", result="ok")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account/password", response_class=HTMLResponse)
def change_password_form(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(
        "change_password.html", {"request": request, "user": user, "error": None, "success": False}
    )


@router.post("/account/password", response_class=HTMLResponse)
def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user=Depends(require_login),
):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    def _rerender(error: str):
        return templates.TemplateResponse(
            "change_password.html",
            {"request": request, "user": user, "error": error, "success": False},
            status_code=400,
        )

    if not verify_password(current_password, row["password_hash"]):
        log_action(actor=user["username"], action="password_change_failed", result="denied", details="wrong current password")
        return _rerender("Current password is incorrect.")
    if len(new_password) < 8:
        return _rerender("New password must be at least 8 characters.")
    if new_password != confirm_password:
        return _rerender("New password and confirmation don't match.")

    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user["id"]))
    log_action(actor=user["username"], action="password_changed", result="ok")
    return templates.TemplateResponse(
        "change_password.html", {"request": request, "user": user, "error": None, "success": True}
    )
