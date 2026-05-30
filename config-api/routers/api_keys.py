"""
Per-user API key management.

Keys are shown in full exactly once (at creation). Only the SHA-256 hash
is stored. Users can create multiple named keys and revoke any of them.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import db

router = APIRouter()


class KeyCreate(BaseModel):
    label: str = "My key"


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("")
async def list_keys(request: Request):
    """List all API keys for the current user (never returns the raw key)."""
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") in ("admin", "dev"):
        raise HTTPException(status_code=403, detail="API keys require a user account (sign in with GitHub)")
    user_id = user["user_id"]
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label, created_at, last_used_at FROM api_keys WHERE user_id=$1 ORDER BY created_at DESC",
            user_id,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_key(request: Request, body: KeyCreate):
    """
    Create a new API key. Returns the raw key — shown ONCE, never stored.
    Subsequent calls to GET /api/keys will show label + metadata only.
    """
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") in ("admin", "dev"):
        raise HTTPException(status_code=403, detail="API keys require a user account (sign in with GitHub)")
    user_id = user["user_id"]

    raw_key = auth.generate_raw_key()
    key_hash = auth.hash_key(raw_key)

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO api_keys (user_id, key_hash, label) VALUES ($1, $2, $3) RETURNING id, label, created_at",
            user_id, key_hash, body.label.strip() or "My key",
        )
    return {
        "id": str(row["id"]),
        "label": row["label"],
        "created_at": row["created_at"],
        "key": raw_key,   # ← shown only here, once
        "warning": "Save this key now — it will not be shown again.",
    }


@router.delete("/{key_id}", status_code=204)
async def revoke_key(request: Request, key_id: str):
    """Revoke (delete) an API key. Irreversible."""
    user = getattr(request.state, "user", None)
    if not user or user.get("user_id") in ("admin", "dev"):
        raise HTTPException(status_code=403, detail="API keys require a user account")
    user_id = user["user_id"]
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM api_keys WHERE id=$1 AND user_id=$2",
            key_id, user_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Key not found")
