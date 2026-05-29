"""
asyncpg connection pool + query helpers.
Builds the fully-denormalized snapshot dict used by Redis and the internal endpoint.
"""

import json
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str) -> None:
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool not initialised"
    return _pool


@asynccontextmanager
async def transaction():
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            yield conn


# ── snapshot builder ──────────────────────────────────────────────────────────

async def build_snapshot(slug: str) -> dict[str, Any] | None:
    """
    Return the fully-denormalized config dict for one agent, or None if not found.
    This is what gets stored in Redis and returned by the internal snapshot endpoint.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow(
            "SELECT * FROM agents WHERE slug = $1 AND is_active = true", slug
        )
        if not agent:
            return None

        agent_id = agent["id"]

        providers_rows = await conn.fetch(
            "SELECT provider_type, provider_name, model, extra_config "
            "FROM provider_configs WHERE agent_id = $1",
            agent_id,
        )

        tools_rows = await conn.fetch(
            "SELECT tool_name, handler_type, description, parameters, required_params, sort_order "
            "FROM tool_definitions WHERE agent_id = $1 ORDER BY sort_order",
            agent_id,
        )

        specialists_rows = await conn.fetch(
            "SELECT specialist_key, display_name, system_prompt, subagent_model, sort_order "
            "FROM specialist_configs WHERE agent_id = $1 ORDER BY sort_order",
            agent_id,
        )

        defaults_rows = await conn.fetch("SELECT key, value FROM system_settings")

    defaults = {r["key"]: r["value"] for r in defaults_rows}

    providers: dict[str, dict] = {}
    for r in providers_rows:
        extra = json.loads(r["extra_config"]) if r["extra_config"] else {}
        providers[r["provider_type"]] = {
            "name": r["provider_name"],
            **({"model": r["model"]} if r["model"] else {}),
            **extra,
        }

    # Fill missing providers from system defaults
    for ptype, default_key in [
        ("stt", "default_stt_provider"),
        ("llm", "default_llm_provider"),
        ("tts", "default_tts_provider"),
    ]:
        if ptype not in providers:
            providers[ptype] = {"name": defaults.get(default_key, "local")}

    tools = [
        {
            "tool_name": r["tool_name"],
            "handler_type": r["handler_type"],
            "description": r["description"],
            "parameters": json.loads(r["parameters"]) if isinstance(r["parameters"], str) else dict(r["parameters"]),
            "required_params": list(r["required_params"]),
            "sort_order": r["sort_order"],
        }
        for r in tools_rows
    ]

    specialists = {
        r["specialist_key"]: {
            "display_name": r["display_name"],
            "system_prompt": r["system_prompt"],
            "subagent_model": r["subagent_model"],
        }
        for r in specialists_rows
    }

    return {
        "slug": agent["slug"],
        "display_name": agent["display_name"],
        "system_prompt": agent["system_prompt"],
        "greeting_trigger": agent["greeting_trigger"],
        "providers": providers,
        "tools": tools,
        "specialists": specialists,
    }


# ── agent CRUD helpers ────────────────────────────────────────────────────────

async def list_agents() -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slug, display_name, is_active, updated_at FROM agents ORDER BY display_name"
        )
    return [dict(r) for r in rows]


async def get_agent_full(slug: str) -> dict | None:
    """Return agent row + related tables as dicts."""
    pool = get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow("SELECT * FROM agents WHERE slug = $1", slug)
        if not agent:
            return None
        agent_id = agent["id"]

        providers = await conn.fetch(
            "SELECT * FROM provider_configs WHERE agent_id = $1", agent_id
        )
        tools = await conn.fetch(
            "SELECT * FROM tool_definitions WHERE agent_id = $1 ORDER BY sort_order", agent_id
        )
        specialists = await conn.fetch(
            "SELECT * FROM specialist_configs WHERE agent_id = $1 ORDER BY sort_order", agent_id
        )
        versions = await conn.fetch(
            "SELECT id, changed_by, created_at FROM config_versions "
            "WHERE agent_id = $1 ORDER BY created_at DESC LIMIT 20",
            agent_id,
        )

    def row2dict(r):
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, asyncpg.Record):
                d[k] = dict(v)
        return d

    return {
        "agent": dict(agent),
        "providers": [row2dict(r) for r in providers],
        "tools": [row2dict(r) for r in tools],
        "specialists": [row2dict(r) for r in specialists],
        "versions": [row2dict(r) for r in versions],
    }


async def get_version_snapshot(agent_id: str, version_id: str) -> dict | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT snapshot FROM config_versions WHERE id = $1 AND agent_id = $2",
            version_id,
            agent_id,
        )
    if not row:
        return None
    snap = row["snapshot"]
    return json.loads(snap) if isinstance(snap, str) else dict(snap)
