"""
Agent CRUD API — write to Postgres, push snapshot to Redis on every mutation.
"""

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

import auth
import db
import redis_client

router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────────

class AgentBase(BaseModel):
    display_name: str
    system_prompt: str
    greeting_trigger: str = "Hello"
    is_active: bool = True


class AgentCreate(AgentBase):
    slug: str


class AgentUpdate(BaseModel):
    display_name: str | None = None
    system_prompt: str | None = None
    greeting_trigger: str | None = None
    is_active: bool | None = None


class ProviderConfig(BaseModel):
    provider_name: str
    model: str | None = None
    extra_config: dict[str, Any] | None = None


class ToolDefinition(BaseModel):
    tool_name: str
    handler_type: str
    description: str
    parameters: dict[str, Any]
    required_params: list[str] = []
    sort_order: int = 0
    handler_config: dict[str, Any] | None = None


class SpecialistConfig(BaseModel):
    specialist_key: str
    display_name: str
    system_prompt: str
    subagent_model: str | None = None
    sort_order: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_agent_id(conn, slug: str, owner_id=None) -> str:
    row = await conn.fetchrow(
        "SELECT id FROM agents WHERE slug = $1 AND ($2::uuid IS NULL OR owner_id = $2::uuid)",
        slug, owner_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    return str(row["id"])


async def _push_snapshot(slug: str) -> None:
    snapshot = await db.build_snapshot(slug)
    if snapshot:
        synced = await redis_client.push_agent_snapshot(snapshot)
        if not synced:
            logger.warning(f"Config saved to Postgres but Redis push failed for '{slug}'")


async def _save_version(conn, agent_id: str, snapshot: dict) -> None:
    await conn.execute(
        "INSERT INTO config_versions (agent_id, snapshot) VALUES ($1, $2)",
        agent_id,
        json.dumps(snapshot),
    )


# ── Agent endpoints ───────────────────────────────────────────────────────────

@router.get("")
async def list_agents(request: Request):
    owner_id = auth.get_owner_id(request)
    return await db.list_agents(owner_id=owner_id)


@router.post("", status_code=201)
async def create_agent(request: Request, body: AgentCreate):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                row = await conn.fetchrow(
                    "INSERT INTO agents (slug, display_name, system_prompt, greeting_trigger, is_active, owner_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6::uuid) RETURNING *",
                    body.slug, body.display_name, body.system_prompt,
                    body.greeting_trigger, body.is_active, owner_id,
                )
            except Exception as e:
                if "unique" in str(e).lower():
                    raise HTTPException(status_code=409, detail=f"Slug '{body.slug}' already exists")
                raise

    await _push_snapshot(body.slug)
    return dict(row)


@router.get("/{slug}")
async def get_agent(request: Request, slug: str):
    owner_id = auth.get_owner_id(request)
    data = await db.get_agent_full(slug, owner_id=owner_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    return data


@router.put("/{slug}")
async def update_agent(request: Request, slug: str, body: AgentUpdate):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            updates, params = [], [agent_id]
            for field in ("display_name", "system_prompt", "greeting_trigger", "is_active"):
                val = getattr(body, field)
                if val is not None:
                    params.append(val)
                    updates.append(f"{field} = ${len(params)}")
            if not updates:
                raise HTTPException(status_code=422, detail="No fields to update")
            await conn.execute(
                f"UPDATE agents SET {', '.join(updates)} WHERE id = $1", *params
            )
            snapshot = await db.build_snapshot(slug)
            if snapshot:
                await _save_version(conn, agent_id, snapshot)

    await _push_snapshot(slug)
    return {"status": "updated"}


@router.delete("/{slug}")
async def delete_agent(request: Request, slug: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE agents SET is_active = false WHERE slug = $1 AND ($2::uuid IS NULL OR owner_id = $2::uuid)",
                slug, owner_id,
            )
            if result == "UPDATE 0":
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    await redis_client.delete_agent_snapshot(slug)
    return {"status": "deactivated"}


# ── Provider config endpoints ─────────────────────────────────────────────────

@router.put("/{slug}/providers/{provider_type}")
async def upsert_provider(request: Request, slug: str, provider_type: str, body: ProviderConfig):
    owner_id = auth.get_owner_id(request)
    if provider_type not in ("stt", "llm", "tts"):
        raise HTTPException(status_code=422, detail="provider_type must be stt, llm, or tts")
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            await conn.execute(
                """INSERT INTO provider_configs (agent_id, provider_type, provider_name, model, extra_config)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (agent_id, provider_type)
                   DO UPDATE SET provider_name = EXCLUDED.provider_name,
                                 model = EXCLUDED.model,
                                 extra_config = EXCLUDED.extra_config""",
                agent_id, provider_type, body.provider_name, body.model,
                json.dumps(body.extra_config) if body.extra_config else None,
            )
    await _push_snapshot(slug)
    return {"status": "updated"}


# ── Tool definition endpoints ─────────────────────────────────────────────────

@router.get("/{slug}/tools")
async def list_tools(request: Request, slug: str):
    owner_id = auth.get_owner_id(request)
    data = await db.get_agent_full(slug, owner_id=owner_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    return data["tools"]


@router.put("/{slug}/tools/{tool_name}")
async def upsert_tool(request: Request, slug: str, tool_name: str, body: ToolDefinition):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            await conn.execute(
                """INSERT INTO tool_definitions
                       (agent_id, tool_name, handler_type, description, parameters,
                        required_params, sort_order, handler_config)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT ON CONSTRAINT tool_definitions_agent_tool_name_idx DO UPDATE SET
                       handler_type   = EXCLUDED.handler_type,
                       description    = EXCLUDED.description,
                       parameters     = EXCLUDED.parameters,
                       required_params = EXCLUDED.required_params,
                       sort_order     = EXCLUDED.sort_order,
                       handler_config = EXCLUDED.handler_config""",
                agent_id, tool_name, body.handler_type, body.description,
                json.dumps(body.parameters), body.required_params, body.sort_order,
                json.dumps(body.handler_config) if body.handler_config else None,
            )
    await _push_snapshot(slug)
    return {"status": "updated"}


@router.post("/{slug}/tools/assign/{tool_id}", status_code=201)
async def assign_global_tool(request: Request, slug: str, tool_id: str, sort_order: int = 0):
    """Assign a global tool to an agent."""
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            # Verify tool exists and is global
            tool = await conn.fetchrow(
                "SELECT id FROM tool_definitions WHERE id = $1 AND is_global = TRUE", tool_id
            )
            if not tool:
                raise HTTPException(status_code=404, detail="Global tool not found")
            await conn.execute(
                """INSERT INTO agent_tool_refs (agent_id, tool_id, sort_order)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (agent_id, tool_id) DO UPDATE SET sort_order = EXCLUDED.sort_order""",
                agent_id, tool_id, sort_order,
            )
    await _push_snapshot(slug)
    return {"status": "assigned"}


@router.delete("/{slug}/tools/unassign/{tool_id}")
async def unassign_global_tool(request: Request, slug: str, tool_id: str):
    """Remove a global tool assignment from an agent."""
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            await conn.execute(
                "DELETE FROM agent_tool_refs WHERE agent_id = $1 AND tool_id = $2",
                agent_id, tool_id,
            )
    await _push_snapshot(slug)
    return {"status": "unassigned"}


@router.delete("/{slug}/tools/{tool_name}")
async def delete_tool(request: Request, slug: str, tool_name: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            await conn.execute(
                "DELETE FROM tool_definitions WHERE agent_id = $1 AND tool_name = $2",
                agent_id, tool_name,
            )
    await _push_snapshot(slug)
    return {"status": "deleted"}


# ── Specialist config endpoints ───────────────────────────────────────────────

@router.get("/{slug}/specialists")
async def list_specialists(request: Request, slug: str):
    owner_id = auth.get_owner_id(request)
    data = await db.get_agent_full(slug, owner_id=owner_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    return data["specialists"]


@router.put("/{slug}/specialists/{specialist_key}")
async def upsert_specialist(request: Request, slug: str, specialist_key: str, body: SpecialistConfig):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            await conn.execute(
                """INSERT INTO specialist_configs
                       (agent_id, specialist_key, display_name, system_prompt, subagent_model, sort_order)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (agent_id, specialist_key) DO UPDATE SET
                       display_name = EXCLUDED.display_name,
                       system_prompt = EXCLUDED.system_prompt,
                       subagent_model = EXCLUDED.subagent_model,
                       sort_order = EXCLUDED.sort_order""",
                agent_id, specialist_key, body.display_name, body.system_prompt,
                body.subagent_model, body.sort_order,
            )
    await _push_snapshot(slug)
    return {"status": "updated"}


@router.delete("/{slug}/specialists/{specialist_key}")
async def delete_specialist(request: Request, slug: str, specialist_key: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug, owner_id)
            await conn.execute(
                "DELETE FROM specialist_configs WHERE agent_id = $1 AND specialist_key = $2",
                agent_id, specialist_key,
            )
    await _push_snapshot(slug)
    return {"status": "deleted"}


# ── Version history ───────────────────────────────────────────────────────────

@router.get("/{slug}/versions")
async def list_versions(request: Request, slug: str):
    owner_id = auth.get_owner_id(request)
    data = await db.get_agent_full(slug, owner_id=owner_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
    return data["versions"]


@router.post("/{slug}/versions/{version_id}/restore")
async def restore_version(request: Request, slug: str, version_id: str):
    owner_id = auth.get_owner_id(request)
    data = await db.get_agent_full(slug, owner_id=owner_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")

    agent_id = str(data["agent"]["id"])
    snapshot = await db.get_version_snapshot(agent_id, version_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Version not found")

    synced = await redis_client.push_agent_snapshot(snapshot)
    return {"status": "restored", "redis_synced": synced}
