"""Login / logout routes for the admin UI."""

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/admin/agents", error: str = ""):
    # Already logged in → skip
    if auth.has_valid_session(request):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next, "error": error, "auth_enabled": bool(auth.ADMIN_PASSWORD)},
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def do_login(
    request: Request,
    password: str = Form(...),
    next: str = Form("/admin/agents"),
):
    if not auth.ADMIN_PASSWORD or hmac.compare_digest(password, auth.ADMIN_PASSWORD):
        response = RedirectResponse(next, status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.make_session_cookie_value(),
            httponly=True,
            samesite="lax",
            max_age=86400 * 30,  # 30 days
        )
        return response
    # Wrong password — back to login with error
    return RedirectResponse(
        f"/admin/login?next={next}&error=Incorrect+password", status_code=303
    )


@router.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response
