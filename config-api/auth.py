"""
Authentication helpers for the Voice Agent admin.

Two independent layers:
  - Admin UI  (/admin/*):  session cookie (random token → Redis lookup)
  - REST API  (/api/*):    X-Api-Key header (compared against API_KEY env var)

ADMIN_PASSWORD is kept as an emergency backdoor — it still produces a valid
session cookie without touching the DB/Redis user system.

If the env vars are empty, auth is skipped and a warning is logged at startup.
/internal/* is never checked — Docker-network isolation is its gate.
"""

import hashlib
import hmac
import json
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

log = logging.getLogger(__name__)

ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
API_KEY: str = os.getenv("API_KEY", "")
COOKIE_NAME = "va_session"
SESSION_TTL = 86400 * 30  # 30 days

# Warn once at import time so it shows in container logs on startup
if not ADMIN_PASSWORD:
    log.warning("ADMIN_PASSWORD not set — password backdoor disabled")
if not API_KEY:
    log.warning("API_KEY not set — REST API is unprotected")


# ── Admin backdoor (HMAC, stateless) ─────────────────────────────────────────

def _admin_token() -> str:
    """Deterministic HMAC token for the password backdoor."""
    return hmac.new(
        ADMIN_PASSWORD.encode(),
        b"va-admin-session-v1",
        hashlib.sha256,
    ).hexdigest()


def make_admin_session_cookie() -> str:
    return _admin_token()


# ── OAuth sessions (Redis-backed) ─────────────────────────────────────────────

async def get_session_user(request: Request) -> dict | None:
    """
    Return the user dict for the current session, or None if not authenticated.

    Checks in order:
    1. ADMIN_PASSWORD backdoor — HMAC cookie, returns synthetic admin user
    2. OAuth session — random token looked up in Redis
    3. No ADMIN_PASSWORD + no OAuth configured — allow all (dev mode)
    """
    import redis_client  # late import to avoid circular dep at module load

    token = request.cookies.get(COOKIE_NAME, "")

    # Dev mode: no auth configured at all → let everyone through
    if not ADMIN_PASSWORD and not os.getenv("GITHUB_CLIENT_ID"):
        return {"user_id": "dev", "name": "Dev", "email": "", "avatar_url": ""}

    # ADMIN_PASSWORD backdoor
    if ADMIN_PASSWORD and token and hmac.compare_digest(token, _admin_token()):
        return {"user_id": "admin", "name": "Admin", "email": "", "avatar_url": ""}

    # OAuth session via Redis
    if token:
        try:
            raw = await redis_client.get_redis().get(f"session:{token}")
            if raw:
                return json.loads(raw)
        except Exception as exc:
            log.warning("Redis session lookup failed: %s", exc)

    return None


async def has_valid_session(request: Request) -> bool:
    return (await get_session_user(request)) is not None


# ── API key ───────────────────────────────────────────────────────────────────

def get_owner_id(request: Request) -> str | None:
    """
    Return the UUID string to use in owner_id WHERE clauses, or None if the
    caller is the admin backdoor / dev mode (which sees all rows).
    """
    user = getattr(request.state, "user", None)
    if not user:
        return None
    uid = user.get("user_id", "")
    # admin backdoor and dev mode are superusers — no filter
    return None if uid in ("admin", "dev") else uid


def has_valid_api_key(request: Request) -> bool:
    """Return True if the request carries a valid X-Api-Key header."""
    if not API_KEY:
        return True
    key = request.headers.get("x-api-key", "")
    if not key:
        return False
    return hmac.compare_digest(key, API_KEY)


# ── Starlette middleware ──────────────────────────────────────────────────────

def _next_url(request: Request) -> str:
    path = request.url.path
    if path and path not in ("/admin/login", "/admin/logout") and not path.startswith("/admin/auth"):
        return f"/admin/login?next={path}"
    return "/admin/login"


async def admin_auth_middleware(request: Request, call_next):
    """Protect /admin/* — redirect to login if not authenticated."""
    path = request.url.path
    # Always pass: login page, logout, OAuth callbacks, static assets
    if (not path.startswith("/admin/")
            or path.startswith("/admin/login")
            or path.startswith("/admin/logout")
            or path.startswith("/admin/auth/")):
        return await call_next(request)

    user = await get_session_user(request)
    if not user:
        return RedirectResponse(_next_url(request), status_code=303)

    # Attach user to request state so templates can read it
    request.state.user = user
    return await call_next(request)


async def api_auth_middleware(request: Request, call_next):
    """Protect /api/* — require valid session cookie or X-Api-Key."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    user = await get_session_user(request)
    if user:
        request.state.user = user
        return await call_next(request)
    if has_valid_api_key(request):
        # API key callers get admin-level access (no owner filter)
        request.state.user = {"user_id": "admin", "name": "API", "email": "", "avatar_url": ""}
        return await call_next(request)
    return JSONResponse({"detail": "Forbidden — provide X-Api-Key header"}, status_code=403)
