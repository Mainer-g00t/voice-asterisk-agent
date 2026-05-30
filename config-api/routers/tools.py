"""
Global tool library API — tools that can be assigned to multiple agents.
"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db

router = APIRouter()

HANDLER_TYPES = ["specialist_router", "webhook", "transfer_call"]


class GlobalToolIn(BaseModel):
    tool_name: str
    handler_type: str
    description: str
    parameters: dict[str, Any] = {}
    required_params: list[str] = []
    sort_order: int = 0
    handler_config: dict[str, Any] | None = None


@router.get("")
async def list_global_tools():
    return await db.list_global_tools()


@router.post("", status_code=201)
async def create_global_tool(body: GlobalToolIn):
    if body.handler_type not in HANDLER_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown handler_type. Valid: {HANDLER_TYPES}")
    pool = db.get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO tool_definitions
                       (tool_name, handler_type, description, parameters,
                        required_params, sort_order, handler_config, is_global, agent_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, NULL)
                   RETURNING id""",
                body.tool_name, body.handler_type, body.description,
                json.dumps(body.parameters), body.required_params, body.sort_order,
                json.dumps(body.handler_config) if body.handler_config else None,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail=f"Global tool '{body.tool_name}' already exists")
            raise
    return {"status": "created", "id": str(row["id"])}


@router.put("/{tool_name}")
async def update_global_tool(tool_name: str, body: GlobalToolIn):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE tool_definitions
               SET handler_type   = $2,
                   description    = $3,
                   parameters     = $4,
                   required_params = $5,
                   sort_order     = $6,
                   handler_config = $7
               WHERE tool_name = $1 AND is_global = TRUE""",
            tool_name, body.handler_type, body.description,
            json.dumps(body.parameters), body.required_params, body.sort_order,
            json.dumps(body.handler_config) if body.handler_config else None,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail=f"Global tool '{tool_name}' not found")

    # Re-push snapshots for all agents that have this tool assigned
    pool2 = db.get_pool()
    async with pool2.acquire() as conn:
        refs = await conn.fetch(
            """SELECT a.slug FROM agents a
               JOIN agent_tool_refs atr ON atr.agent_id = a.id
               JOIN tool_definitions td ON td.id = atr.tool_id
               WHERE td.tool_name = $1 AND td.is_global = TRUE""",
            tool_name,
        )
    for r in refs:
        from routers.agents import _push_snapshot
        await _push_snapshot(r["slug"])

    return {"status": "updated"}


@router.delete("/{tool_name}")
async def delete_global_tool(tool_name: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM tool_definitions WHERE tool_name = $1 AND is_global = TRUE",
            tool_name,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Global tool '{tool_name}' not found")
    return {"status": "deleted"}
