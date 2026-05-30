"""
Authentication helpers for the Voice Agent admin.

Two independent layers:
  - Admin UI  (/admin/*):  session cookie (random token → Redis lookup)
  - REST API  (/api/*):    X-Api-Key header — checked in order:
      1. Global API_KEY env var → admin-level (no owner filter)
      2. Per-user key in api_keys table → scoped to that user's resources

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
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

log = logging.getLogger(__name__)

ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
API_KEY: str = os.getenv("API_KEY", "")
COOKIE_NAME = "va_session"
SESSION_TTL = 86400 * 30  # 30 days

# Key format: sk-va-<64 hex chars>
KEY_PREFIX = "sk-va-"

# Warn once at import time so it shows in container logs on startup
if not ADMIN_PASSWORD:
    log.warning("ADMIN_PASSWORD not set — password backdoor disabled")
if not API_KEY:
    log.warning("API_KEY not set — global API key disabled (per-user keys still work)")


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


# ── Key helpers ───────────────────────────────────────────────────────────────

def generate_raw_key() -> str:
    """Generate a new raw API key. Shown to the user once — never stored."""
    return KEY_PREFIX + secrets.token_hex(32)


def hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of the raw key — this is what we store."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


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


# ── Per-user API key lookup ───────────────────────────────────────────────────

async def get_user_for_api_key(raw_key: str) -> dict | None:
    """
    Look up a per-user API key. Returns the user dict on hit, None on miss.
    Also updates last_used_at in the background (best-effort).
    """
    import db  # late import to avoid circular dep
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT k.id AS key_id, u.id, u.email, u.name, u.avatar_url
               FROM api_keys k
               JOIN users u ON u.id = k.user_id
               WHERE k.key_hash = $1""",
            hash_key(raw_key),
        )
        if not row:
            return None
        # Touch last_used_at (fire-and-forget, don't delay the request)
        await conn.execute(
            "UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", row["key_id"]
        )
    return {
        "user_id": str(row["id"]),
        "name": row["name"] or "",
        "email": row["email"] or "",
        "avatar_url": row["avatar_url"] or "",
    }


# ── Owner ID helper ───────────────────────────────────────────────────────────

def get_owner_id(request: Request) -> str | None:
    """
    Return the UUID string to use in owner_id WHERE clauses, or None if the
    caller is the admin backdoor / dev mode / global API key (sees all rows).
    """
    user = getattr(request.state, "user", None)
    if not user:
        return None
    uid = user.get("user_id", "")
    # admin backdoor, dev mode, and global API key are superusers — no filter
    return None if uid in ("admin", "dev") else uid


# ── Global API key check ──────────────────────────────────────────────────────

def _is_global_api_key(raw_key: str) -> bool:
    """Check the raw key against the global API_KEY env var."""
    if not API_KEY:
        return False
    return hmac.compare_digest(raw_key, API_KEY)


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
    """
    Protect /api/* — accept in this order:
    1. Valid session cookie (browser / OAuth) — scoped to that user
    2. Global API_KEY env var — admin-level (no owner filter)
    3. Per-user key from api_keys table — scoped to that user's resources
    """
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)

    # 1. Session cookie
    user = await get_session_user(request)
    if user:
        request.state.user = user
        return await call_next(request)

    raw_key = request.headers.get("x-api-key", "")
    if raw_key:
        # 2. Global key — admin level, no owner filter
        if _is_global_api_key(raw_key):
            request.state.user = {"user_id": "admin", "name": "API", "email": "", "avatar_url": ""}
            return await call_next(request)

        # 3. Per-user key — scoped to that user
        try:
            key_user = await get_user_for_api_key(raw_key)
        except Exception as exc:
            log.warning("Per-user API key lookup failed: %s", exc)
            key_user = None

        if key_user:
            request.state.user = key_user
            return await call_next(request)

    # No auth configured at all → open (dev mode already handled via session, but
    # cover the case where someone hits /api directly without a cookie)
    if not ADMIN_PASSWORD and not os.getenv("GITHUB_CLIENT_ID") and not API_KEY:
        request.state.user = {"user_id": "dev", "name": "Dev", "email": "", "avatar_url": ""}
        return await call_next(request)

    return JSONResponse({"detail": "Forbidden — provide X-Api-Key header"}, status_code=403)
