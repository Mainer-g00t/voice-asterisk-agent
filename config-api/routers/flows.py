"""
Flow management API — CRUD for call flow definitions.

Flows define multi-step, branching call logic (nodes + edges) that can be
assigned to phone routes or agents. The flow engine runs inside the agent
container; config-api stores definitions and execution history.
"""

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

import db
import redis_client
from voiceai_common.flow_engine import validate_flow, get_entry_node

router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────────

class FlowIn(BaseModel):
    name: str
    description: str | None = None
    definition: dict[str, Any] = {}  # nodes + edges JSON


class FlowExecutionIn(BaseModel):
    """Posted by the agent when a flow execution ends."""
    call_uuid: str
    status: str
    current_node_id: str | None = None
    state: dict[str, Any] = {}
    events: list[dict[str, Any]] = []  # [{event_type, node_id, edge_id, data, ts}, ...]


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.get("")
async def list_flows():
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, description, is_active, created_at, updated_at "
            "FROM flows ORDER BY name"
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_flow(body: FlowIn):
    errors = validate_flow(body.definition)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO flows (name, description, definition)
               VALUES ($1, $2, $3)
               RETURNING id, name, description, is_active, created_at""",
            body.name,
            body.description,
            json.dumps(body.definition),
        )
    logger.info(f"Flow created: '{body.name}' ({row['id']})")
    return dict(row)


@router.get("/{flow_id}")
async def get_flow(flow_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM flows WHERE id=$1::uuid", flow_id)
    if not row:
        raise HTTPException(status_code=404, detail="Flow not found")
    d = dict(row)
    if isinstance(d.get("definition"), str):
        d["definition"] = json.loads(d["definition"])
    return d


@router.put("/{flow_id}")
async def update_flow(flow_id: str, body: FlowIn):
    errors = validate_flow(body.definition)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE flows SET name=$2, description=$3, definition=$4, updated_at=NOW()
               WHERE id=$1::uuid
               RETURNING id, name, description, is_active, updated_at""",
            flow_id, body.name, body.description, json.dumps(body.definition),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Flow not found")
    logger.info(f"Flow updated: '{body.name}' ({flow_id})")
    return dict(row)


@router.delete("/{flow_id}", status_code=204)
async def deactivate_flow(flow_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE flows SET is_active=false, updated_at=NOW() WHERE id=$1::uuid",
            flow_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Flow not found")


# ── Execution history ─────────────────────────────────────────────────────────

@router.get("/{flow_id}/executions")
async def list_executions(flow_id: str, limit: int = 50):
    pool = db.get_pool()
    async with pool.acquire() as conn:
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
    """
    Called by the agent container after the call ends.
    Updates the execution row and bulk-inserts the event log.
    """
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
