"""
Call logs API — receive call data from agents, expose for the admin UI.
"""

import asyncio
import json
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

import auth
import db
import redis_client

router = APIRouter()


class CallLogIn(BaseModel):
    call_uuid: str
    agent_slug: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: int | None = None
    turn_count: int = 0
    transcript: list[dict] | None = None
    stt_provider: str | None = None
    llm_provider: str | None = None
    tts_provider: str | None = None
    end_reason: str | None = None


@router.post("", status_code=201)
async def receive_call_log(body: CallLogIn):
    """Called by the agent container — no user context, no ownership filter."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        did_row = await conn.fetchrow(
            "SELECT did FROM phone_routes WHERE agent_slug=$1 AND is_active=true "
            "ORDER BY did LIMIT 1",
            body.agent_slug,
        )
        did = did_row["did"] if did_row else None

        def _parse_dt(s: str | None) -> datetime | None:
            return datetime.fromisoformat(s) if s else None

        await conn.execute(
            """INSERT INTO call_logs
               (call_uuid, agent_slug, did, started_at, ended_at, duration_seconds,
                turn_count, transcript, stt_provider, llm_provider, tts_provider, end_reason)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (call_uuid) DO UPDATE SET
                 did              = COALESCE(EXCLUDED.did, call_logs.did),
                 started_at       = COALESCE(EXCLUDED.started_at, call_logs.started_at),
                 ended_at         = EXCLUDED.ended_at,
                 duration_seconds = EXCLUDED.duration_seconds,
                 turn_count       = EXCLUDED.turn_count,
                 transcript       = EXCLUDED.transcript,
                 stt_provider     = EXCLUDED.stt_provider,
                 llm_provider     = EXCLUDED.llm_provider,
                 tts_provider     = EXCLUDED.tts_provider,
                 end_reason       = EXCLUDED.end_reason""",
            body.call_uuid, body.agent_slug, did,
            _parse_dt(body.started_at), _parse_dt(body.ended_at), body.duration_seconds,
            body.turn_count,
            json.dumps(body.transcript) if body.transcript is not None else None,
            body.stt_provider, body.llm_provider, body.tts_provider,
            body.end_reason,
        )

    meta = await redis_client.get_call_meta(body.call_uuid)
    callback_url = meta.get("callback_url")
    if callback_url:
        cdr_payload = {
            **body.model_dump(),
            "did": did,
            "metadata": meta.get("metadata", {}),
        }
        asyncio.create_task(_fire_callback(callback_url, body.call_uuid, cdr_payload))

    return {"status": "ok", "call_uuid": body.call_uuid}


async def _fire_callback(url: str, call_uuid: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            resp.raise_for_status()
        logger.info(f"CDR callback fired for {call_uuid} → {url} ({resp.status_code})")
    except Exception as exc:
        logger.warning(f"CDR callback failed for {call_uuid} → {url}: {exc}")


@router.get("")
async def list_calls(request: Request, limit: int = 50, offset: int = 0):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT cl.call_uuid, cl.agent_slug, cl.did, cl.started_at, cl.ended_at,
                      cl.duration_seconds, cl.turn_count, cl.stt_provider, cl.llm_provider,
                      cl.tts_provider, cl.end_reason
               FROM call_logs cl
               LEFT JOIN agents a ON cl.agent_slug = a.slug
               WHERE ($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)
               ORDER BY cl.started_at DESC NULLS LAST
               LIMIT $2 OFFSET $3""",
            owner_id, limit, offset,
        )
        total = await conn.fetchval(
            """SELECT COUNT(*) FROM call_logs cl
               LEFT JOIN agents a ON cl.agent_slug = a.slug
               WHERE ($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)""",
            owner_id,
        )
    return {"total": total, "calls": [dict(r) for r in rows]}


@router.get("/{call_uuid}")
async def get_call(request: Request, call_uuid: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT cl.* FROM call_logs cl
               LEFT JOIN agents a ON cl.agent_slug = a.slug
               WHERE cl.call_uuid=$1
               AND ($2::uuid IS NULL OR a.owner_id = $2::uuid OR a.owner_id IS NULL)""",
            call_uuid, owner_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    d = dict(row)
    if isinstance(d.get("transcript"), str):
        d["transcript"] = json.loads(d["transcript"])
    return d
