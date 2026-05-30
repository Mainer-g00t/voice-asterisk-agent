"""
Internal endpoints — reachable only within the voiceai Docker network.
Used by agent/pipeline.py as a fallback when the Redis key is missing.
"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
import redis_client
from flow_engine import get_entry_node

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
