from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import init_db
from app.routers import auth_routes, dashboard, ledger, actions, decommission, wizard, lifecycle, project_hub

app = FastAPI(title="Home Server Control Panel")
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(ledger.router)
app.include_router(actions.router)
app.include_router(decommission.router)
app.include_router(wizard.router)
app.include_router(lifecycle.router)
app.include_router(project_hub.router)


@app.on_event("startup")
def on_startup():
    init_db()
