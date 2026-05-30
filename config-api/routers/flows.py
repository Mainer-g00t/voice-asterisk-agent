"""
Flow management API — CRUD for call flow definitions.
"""

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

import auth
import db
import redis_client
from voiceai_common.flow_engine import validate_flow, get_entry_node

router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────────

class FlowIn(BaseModel):
    name: str
    description: str | None = None
    definition: dict[str, Any] = {}


class FlowExecutionIn(BaseModel):
    call_uuid: str
    status: str
    current_node_id: str | None = None
    state: dict[str, Any] = {}
    events: list[dict[str, Any]] = []


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.get("")
async def list_flows(request: Request):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, description, is_active, created_at, updated_at "
            "FROM flows "
            "WHERE ($1::uuid IS NULL OR owner_id = $1::uuid OR owner_id IS NULL) "
            "ORDER BY name",
            owner_id,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_flow(request: Request, body: FlowIn):
    owner_id = auth.get_owner_id(request)
    errors = validate_flow(body.definition)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO flows (name, description, definition, owner_id)
               VALUES ($1, $2, $3, $4::uuid)
               RETURNING id, name, description, is_active, created_at""",
            body.name, body.description, json.dumps(body.definition), owner_id,
        )
    logger.info(f"Flow created: '{body.name}' ({row['id']})")
    return dict(row)


@router.get("/{flow_id}")
async def get_flow(request: Request, flow_id: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM flows WHERE id=$1::uuid AND ($2::uuid IS NULL OR owner_id=$2::uuid OR owner_id IS NULL)",
            flow_id, owner_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Flow not found")
    d = dict(row)
    if isinstance(d.get("definition"), str):
        d["definition"] = json.loads(d["definition"])
    return d


@router.put("/{flow_id}")
async def update_flow(request: Request, flow_id: str, body: FlowIn):
    owner_id = auth.get_owner_id(request)
    errors = validate_flow(body.definition)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE flows SET name=$2, description=$3, definition=$4, updated_at=NOW()
               WHERE id=$1::uuid AND ($5::uuid IS NULL OR owner_id=$5::uuid)
               RETURNING id, name, description, is_active, updated_at""",
            flow_id, body.name, body.description, json.dumps(body.definition), owner_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Flow not found")
    logger.info(f"Flow updated: '{body.name}' ({flow_id})")
    return dict(row)


@router.delete("/{flow_id}", status_code=204)
async def deactivate_flow(request: Request, flow_id: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE flows SET is_active=false, updated_at=NOW() "
            "WHERE id=$1::uuid AND ($2::uuid IS NULL OR owner_id=$2::uuid)",
            flow_id, owner_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Flow not found")


# ── Execution history ─────────────────────────────────────────────────────────

@router.get("/{flow_id}/executions")
async def list_executions(request: Request, flow_id: str, limit: int = 50):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Verify flow is accessible to this user
        flow = await conn.fetchrow(
            "SELECT id FROM flows WHERE id=$1::uuid AND ($2::uuid IS NULL OR owner_id=$2::uuid OR owner_id IS NULL)",
            flow_id, owner_id,
        )
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        rows = await conn.fetch(
            """SELECT id, call_uuid, current_node_id, status, state, started_at, ended_at
               FROM flow_executions WHERE flow_id=$1::uuid
               ORDER BY started_at DESC LIMIT $2""",
            flow_id, limit,
        )
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("state"), str):
            d["state"] = json.loads(d["state"])
        result.append(d)
    return result


@router.get("/executions/{execution_id}/events")
async def get_execution_events(execution_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM flow_events WHERE execution_id=$1::uuid ORDER BY ts",
            execution_id,
        )
    return [dict(r) for r in rows]


# ── Internal: agent posts execution summary at call end ───────────────────────

@router.post("/executions/complete", status_code=200)
async def complete_execution(body: FlowExecutionIn):
    """Called by the agent container — no user context, no ownership filter."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        exec_row = await conn.fetchrow(
            "SELECT id FROM flow_executions WHERE call_uuid=$1", body.call_uuid
        )
        if not exec_row:
            logger.warning(f"No flow execution found for call {body.call_uuid}")
            return {"status": "not_found"}

        exec_id = exec_row["id"]
        await conn.execute(
            """UPDATE flow_executions SET
                 status=$2, current_node_id=$3, state=$4, ended_at=NOW()
               WHERE id=$1""",
            exec_id, body.status, body.current_node_id, json.dumps(body.state),
        )

        if body.events:
            await conn.executemany(
                """INSERT INTO flow_events (execution_id, ts, event_type, node_id, edge_id, data)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                [
                    (
                        exec_id,
                        datetime.fromisoformat(e["ts"]) if e.get("ts") else datetime.now(timezone.utc),
                        e.get("event_type", "unknown"),
                        e.get("node_id"),
                        e.get("edge_id"),
                        json.dumps(e.get("data", {})),
                    )
                    for e in body.events
                ],
            )

    return {"status": "ok", "execution_id": str(exec_id)}
