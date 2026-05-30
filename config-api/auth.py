"""
Authentication helpers for the Voice Agent admin.

Two independent layers:
  - Admin UI  (/admin/*):  session cookie (HMAC-signed, keyed by ADMIN_PASSWORD)
  - REST API  (/api/*):    X-Api-Key header (compared against API_KEY env var)

If the env vars are empty, auth is skipped and a warning is logged at startup.
/internal/* is never checked — Docker-network isolation is its gate.
"""

import hashlib
import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

log = logging.getLogger(__name__)

ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
API_KEY: str = os.getenv("API_KEY", "")
COOKIE_NAME = "va_session"

# Warn once at import time so it shows in container logs on startup
if not ADMIN_PASSWORD:
    log.warning("ADMIN_PASSWORD not set — admin UI is unprotected")
if not API_KEY:
    log.warning("API_KEY not set — REST API is unprotected")


# ── Session (admin UI) ────────────────────────────────────────────────────────

def _session_token() -> str:
    """Deterministic HMAC of a fixed string keyed by ADMIN_PASSWORD."""
    return hmac.new(
        ADMIN_PASSWORD.encode(),
        b"va-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()


def has_valid_session(request: Request) -> bool:
    """Return True if the request carries a valid session cookie."""
    if not ADMIN_PASSWORD:
        return True
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return False
    return hmac.compare_digest(token, _session_token())


def make_session_cookie_value() -> str:
    return _session_token()


# ── API key ───────────────────────────────────────────────────────────────────

def has_valid_api_key(request: Request) -> bool:
    """Return True if the request carries a valid X-Api-Key header."""
    if not API_KEY:
        return True
    key = request.headers.get("x-api-key", "")
    if not key:
        return False
    return hmac.compare_digest(key, API_KEY)


# ── Starlette middleware helpers ──────────────────────────────────────────────

def _next_url(request: Request) -> str:
    """Build ?next= param to return the user to the right page after login."""
    path = request.url.path
    if path and path != "/admin/login":
        return f"/admin/login?next={path}"
    return "/admin/login"


async def admin_auth_middleware(request: Request, call_next):
    """Protect /admin/* — redirect to login if session cookie is missing/invalid."""
    path = request.url.path
    # Always allow login / logout pages and static assets
    if not path.startswith("/admin/") or path.startswith("/admin/login") or path.startswith("/admin/logout"):
        return await call_next(request)
    if not has_valid_session(request):
        return RedirectResponse(_next_url(request), status_code=303)
    return await call_next(request)


async def api_auth_middleware(request: Request, call_next):
    """Protect /api/* — require either a valid session cookie or X-Api-Key header."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if has_valid_session(request) or has_valid_api_key(request):
        return await call_next(request)
    return JSONResponse({"detail": "Forbidden — provide X-Api-Key header"}, status_code=403)
