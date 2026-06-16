"""Admin panel router assembly."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin.auth import check_credentials, create_session, destroy_session, get_current_admin
from app.admin.routes import analytics, dashboard, moderation, settings

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/admin/templates")


# --- Auth routes ---

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    # If already logged in, redirect to dashboard
    token = request.cookies.get("tbc_admin_session")
    if token:
        return RedirectResponse("/admin/")  # type: ignore[return-value]
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    if check_credentials(username, password):
        response = RedirectResponse("/admin/", status_code=303)
        create_session(response, username)
        return response
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid credentials"},
        status_code=401,
    )


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    destroy_session(response)
    return response


# --- Section routers ---

router.include_router(dashboard.router, prefix="")
router.include_router(settings.router, prefix="/settings", tags=["settings"])
router.include_router(moderation.router, prefix="/moderation", tags=["moderation"])
router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
