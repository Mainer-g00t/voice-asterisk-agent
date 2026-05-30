"""
Internal endpoints — reachable only within the voiceai Docker network.
Used by agent/pipeline.py as a fallback when the Redis key is missing,
and by the Asterisk dialplan (via CURL) to pre-register inbound caller info.
"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
import redis_client
from voiceai_common.flow_engine import get_entry_node

router = APIRouter()


@router.get("/agents/{slug}/snapshot")
async def get_snapshot(slug: str):
    """
    Return the full denormalized agent config snapshot.
    Also warms the Redis cache as a side-effect.
    """
    snapshot = await db.build_snapshot(slug)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found or inactive")

    # Warm Redis (best-effort — don't fail if Redis is down)
    await redis_client.push_agent_snapshot(snapshot)

    return JSONResponse(content=snapshot)


class PreRegisterRequest(BaseModel):
    call_uuid: str
    caller_id: str | None = None   # CALLERID(num) from Asterisk
    did: str | None = None         # dialed extension / DID


@router.post("/calls/pre-register")
async def pre_register_call(body: PreRegisterRequest):
    """
    Called by the Asterisk dialplan (CURL) before AudioSocket for inbound calls.
    Pre-creates the call_log row with caller_id and did so the UI shows caller
    info as soon as the call is connected.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Resolve agent slug from the DID via phone_routes
        agent_row = await conn.fetchrow(
            "SELECT agent_slug FROM phone_routes WHERE did=$1 AND is_active=true LIMIT 1",
            body.did,
        ) if body.did else None
        # Fall back to catch-all route
        if not agent_row and body.did:
            agent_row = await conn.fetchrow(
                "SELECT agent_slug FROM phone_routes WHERE did='_X.' AND is_active=true LIMIT 1"
            )
        agent_slug = agent_row["agent_slug"] if agent_row else "unknown"

        await conn.execute(
            """INSERT INTO call_logs (call_uuid, agent_slug, direction, caller_id, did)
               VALUES ($1, $2, 'inbound', $3, $4)
               ON CONFLICT (call_uuid) DO UPDATE SET
                 caller_id = COALESCE(EXCLUDED.caller_id, call_logs.caller_id),
                 did       = COALESCE(EXCLUDED.did,       call_logs.did),
                 agent_slug = COALESCE(EXCLUDED.agent_slug, call_logs.agent_slug)""",
            body.call_uuid, agent_slug, body.caller_id or None, body.did or None,
        )
    return {"status": "ok"}


class InitFlowRequest(BaseModel):
    call_uuid: str
    flow_id: str


@router.post("/flows/init-execution")
async def init_flow_execution(body: InitFlowRequest):
    """
    Called by the agent at call start when the agent config has a flow_id
    but no flow execution has been pre-created (i.e. inbound calls).

    1. Fetches flow definition from Postgres
    2. Creates flow_executions row
    3. Pushes snapshot to Redis so the agent can read it immediately
    Returns the execution snapshot.
    """
    flow = await db.get_flow(body.flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail=f"Flow '{body.flow_id}' not found")

    flow_def = flow["definition"]
    entry_node = get_entry_node(flow_def)
    entry_node_id = entry_node["id"] if entry_node else ""

    exec_id = await db.create_flow_execution(body.flow_id, body.call_uuid, entry_node_id)

    snapshot = {
        "execution_id": exec_id,
        "flow_id": body.flow_id,
        "flow_def": flow_def,
        "current_node_id": entry_node_id,
        "state": {},
    }
    await redis_client.push_flow_execution(body.call_uuid, snapshot)

    return snapshot
