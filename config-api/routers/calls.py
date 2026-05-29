"""
Call logs API — receive call data from agents, expose for the admin UI.
"""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db

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
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Look up the DID for this agent slug from active routes
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
                 ended_at = EXCLUDED.ended_at,
                 duration_seconds = EXCLUDED.duration_seconds,
                 turn_count = EXCLUDED.turn_count,
                 transcript = EXCLUDED.transcript,
                 end_reason = EXCLUDED.end_reason""",
            body.call_uuid, body.agent_slug, did,
            _parse_dt(body.started_at), _parse_dt(body.ended_at), body.duration_seconds,
            body.turn_count,
            json.dumps(body.transcript) if body.transcript is not None else None,
            body.stt_provider, body.llm_provider, body.tts_provider,
            body.end_reason,
        )
    return {"status": "ok", "call_uuid": body.call_uuid}


@router.get("")
async def list_calls(limit: int = 50, offset: int = 0):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT call_uuid, agent_slug, did, started_at, ended_at,
                      duration_seconds, turn_count, stt_provider, llm_provider,
                      tts_provider, end_reason
               FROM call_logs
               ORDER BY started_at DESC NULLS LAST
               LIMIT $1 OFFSET $2""",
            limit, offset,
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM call_logs")
    return {"total": total, "calls": [dict(r) for r in rows]}


@router.get("/{call_uuid}")
async def get_call(call_uuid: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM call_logs WHERE call_uuid=$1", call_uuid
        )
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    d = dict(row)
    if isinstance(d.get("transcript"), str):
        d["transcript"] = json.loads(d["transcript"])
    return d
