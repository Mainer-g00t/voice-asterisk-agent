"""
OAuth routes — currently GitHub only.
Extend by adding a new provider block following the same pattern.

Mount under /admin/auth in main.py.
"""

import json
import os
import secrets

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

import db
import redis_client
from auth import COOKIE_NAME, SESSION_TTL

router = APIRouter()

GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
BASE_URL             = os.getenv("BASE_URL", "http://localhost:8080")

GITHUB_AUTHORIZE_URL   = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL       = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL        = "https://api.github.com/user"
GITHUB_EMAILS_URL      = "https://api.github.com/user/emails"

STATE_TTL = 300  # 5 minutes — time to complete the OAuth consent screen


# ── GitHub ────────────────────────────────────────────────────────────────────

@router.get("/github", include_in_schema=False)
async def github_login(request: Request):
    """Step 1 — redirect the browser to GitHub's OAuth consent page."""
    if not GITHUB_CLIENT_ID:
        return RedirectResponse("/admin/login?error=GitHub+OAuth+not+configured", status_code=303)

    state = secrets.token_hex(16)
    await redis_client.get_redis().setex(f"oauth:state:{state}", STATE_TTL, "1")

    callback = f"{BASE_URL}/admin/auth/github/callback"
    url = (
        f"{GITHUB_AUTHORIZE_URL}"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&redirect_uri={callback}"
        f"&scope=user:email"
        f"&state={state}"
    )
    return RedirectResponse(url, status_code=303)


@router.get("/github/callback", include_in_schema=False)
async def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Step 2 — GitHub redirects here after the user approves (or denies) access."""

    if error:
        return RedirectResponse(f"/admin/login?error=GitHub+denied:+{error}", status_code=303)

    # Verify CSRF state
    state_key = f"oauth:state:{state}"
    valid = await redis_client.get_redis().getdel(state_key)
    if not valid:
        return RedirectResponse("/admin/login?error=Invalid+OAuth+state", status_code=303)

    callback = f"{BASE_URL}/admin/auth/github/callback"

    async with httpx.AsyncClient(timeout=10) as client:
        # Exchange code for access token
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id":     GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code":          code,
                "redirect_uri":  callback,
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            err = token_data.get("error_description", "token exchange failed")
            return RedirectResponse(f"/admin/login?error={err}", status_code=303)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }

        # Fetch profile
        user_resp  = await client.get(GITHUB_USER_URL,   headers=headers)
        profile    = user_resp.json()
        github_id  = str(profile.get("id", ""))
        name       = profile.get("name") or profile.get("login", "")
        avatar_url = profile.get("avatar_url", "")
        email      = profile.get("email", "")

        # GitHub may hide email — fetch from /user/emails if needed
        if not email:
            emails_resp = await client.get(GITHUB_EMAILS_URL, headers=headers)
            for e in emails_resp.json():
                if e.get("primary") and e.get("verified"):
                    email = e["email"]
                    break
            if not email:
                # Take any verified email as fallback
                for e in emails_resp.json():
                    if e.get("verified"):
                        email = e["email"]
                        break

    if not email:
        return RedirectResponse("/admin/login?error=Could+not+retrieve+GitHub+email", status_code=303)

    # Upsert user in Postgres
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, name, avatar_url, github_id, last_login)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (github_id) DO UPDATE
              SET name=$2, avatar_url=$3, email=$1, last_login=NOW()
            RETURNING id, email, name, avatar_url
            """,
            email, name, avatar_url, github_id,
        )

    user = dict(row)

    # Create session in Redis
    session_token = secrets.token_hex(32)
    session_data = {
        "user_id":    str(user["id"]),
        "email":      user["email"],
        "name":       user["name"] or "",
        "avatar_url": user["avatar_url"] or "",
    }
    await redis_client.get_redis().setex(
        f"session:{session_token}",
        SESSION_TTL,
        json.dumps(session_data),
    )

    next_url = request.query_params.get("next", "/admin/agents")
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        session_token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL,
    )
    return response
