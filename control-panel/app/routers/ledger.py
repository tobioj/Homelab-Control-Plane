from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_login
from app.db import get_conn

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/ledger", response_class=HTMLResponse)
def ledger_view(request: Request, user=Depends(require_login)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.idx, l.purpose, l.status, l.notes, p.name AS project_name
               FROM redis_ledger l LEFT JOIN projects p ON p.id = l.project_id
               ORDER BY l.idx"""
        ).fetchall()
    return templates.TemplateResponse(
        "ledger.html", {"request": request, "user": user, "entries": [dict(r) for r in rows]}
    )
