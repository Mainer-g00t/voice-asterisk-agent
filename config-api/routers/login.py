"""Login / logout routes for the admin UI."""

import hmac
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth
import redis_client

router = APIRouter()
templates = Jinja2Templates(directory="templates")

GITHUB_CONFIGURED = bool(os.getenv("GITHUB_CLIENT_ID"))


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/admin/agents", error: str = ""):
    if await auth.has_valid_session(request):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {
            "next": next,
            "error": error,
            "password_enabled": bool(auth.ADMIN_PASSWORD),
            "github_enabled":   GITHUB_CONFIGURED,
        },
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
            auth.make_admin_session_cookie(),
            httponly=True,
            samesite="lax",
            max_age=auth.SESSION_TTL,
        )
        return response
    return RedirectResponse(
        f"/admin/login?next={next}&error=Incorrect+password", status_code=303
    )


@router.get("/logout", include_in_schema=False)
async def logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME, "")
    # Delete OAuth session from Redis if it exists
    if token:
        try:
            await redis_client.get_redis().delete(f"session:{token}")
        except Exception:
            pass
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response
