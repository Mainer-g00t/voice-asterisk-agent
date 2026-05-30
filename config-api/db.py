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
            """
            SELECT tool_name, handler_type, description, parameters,
                   required_params, sort_order, handler_config
            FROM tool_definitions WHERE agent_id = $1
            UNION ALL
            SELECT td.tool_name, td.handler_type, td.description, td.parameters,
                   td.required_params, atr.sort_order, td.handler_config
            FROM tool_definitions td
            JOIN agent_tool_refs atr ON atr.tool_id = td.id
            WHERE atr.agent_id = $1
            ORDER BY sort_order
            """,
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

    # Merge: agent-specific tools take precedence over globals with the same name.
    seen_tool_names: set[str] = set()
    tools = []
    for r in tools_rows:
        name = r["tool_name"]
        if name in seen_tool_names:
            continue
        seen_tool_names.add(name)
        hc = r["handler_config"]
        tools.append({
            "tool_name": name,
            "handler_type": r["handler_type"],
            "description": r["description"],
            "parameters": json.loads(r["parameters"]) if isinstance(r["parameters"], str) else dict(r["parameters"]),
            "required_params": list(r["required_params"]),
            "sort_order": r["sort_order"],
            "handler_config": (json.loads(hc) if isinstance(hc, str) else dict(hc)) if hc else {},
        })

    specialists = {
        r["specialist_key"]: {
            "display_name": r["display_name"],
            "system_prompt": r["system_prompt"],
            "subagent_model": r["subagent_model"],
        }
        for r in specialists_rows
    }

    snapshot = {
        "slug": agent["slug"],
        "display_name": agent["display_name"],
        "system_prompt": agent["system_prompt"],
        "greeting_trigger": agent["greeting_trigger"],
        "providers": providers,
        "tools": tools,
        "specialists": specialists,
    }
    # Include flow_id if one is attached to the agent
    if agent.get("flow_id"):
        snapshot["flow_id"] = str(agent["flow_id"])
    return snapshot


# ── agent CRUD helpers ────────────────────────────────────────────────────────

async def list_agents(owner_id: str | None = None) -> list[dict]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slug, display_name, is_active, updated_at FROM agents "
            "WHERE ($1::uuid IS NULL OR owner_id = $1::uuid OR owner_id IS NULL) "
            "ORDER BY display_name",
            owner_id,
        )
    return [dict(r) for r in rows]


async def get_agent_full(slug: str, owner_id: str | None = None) -> dict | None:
    """Return agent row + related tables as dicts. owner_id=None means no restriction."""
    pool = get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow(
            "SELECT * FROM agents WHERE slug = $1 AND ($2::uuid IS NULL OR owner_id = $2::uuid OR owner_id IS NULL)",
            slug, owner_id,
        )
        if not agent:
            return None
        agent_id = agent["id"]

        providers = await conn.fetch(
            "SELECT * FROM provider_configs WHERE agent_id = $1", agent_id
        )
        tools = await conn.fetch(
            """
            SELECT td.*, NULL::uuid AS ref_agent_id, td.sort_order AS effective_sort,
                   FALSE AS is_assigned_global
            FROM tool_definitions td WHERE td.agent_id = $1
            UNION ALL
            SELECT td.*, atr.agent_id AS ref_agent_id, atr.sort_order AS effective_sort,
                   TRUE AS is_assigned_global
            FROM tool_definitions td
            JOIN agent_tool_refs atr ON atr.tool_id = td.id
            WHERE atr.agent_id = $1
            ORDER BY effective_sort
            """,
            agent_id,
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
            elif isinstance(v, str) and k in ("parameters", "handler_config", "extra_config"):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    pass
        return d

    return {
        "agent": dict(agent),
        "providers": [row2dict(r) for r in providers],
        "tools": [row2dict(r) for r in tools],
        "specialists": [row2dict(r) for r in specialists],
        "versions": [row2dict(r) for r in versions],
    }


async def list_global_tools() -> list[dict]:
    """Return all global tool definitions (is_global = TRUE)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tool_definitions WHERE is_global = TRUE ORDER BY tool_name"
        )
        # Count how many agents each global tool is assigned to
        counts = await conn.fetch(
            "SELECT tool_id, COUNT(*) AS agent_count FROM agent_tool_refs GROUP BY tool_id"
        )
    count_map = {str(r["tool_id"]): r["agent_count"] for r in counts}
    result = []
    for r in rows:
        d = dict(r)
        d["agent_count"] = count_map.get(str(d["id"]), 0)
        if d.get("parameters"):
            d["parameters"] = json.loads(d["parameters"]) if isinstance(d["parameters"], str) else dict(d["parameters"])
        if d.get("handler_config"):
            d["handler_config"] = json.loads(d["handler_config"]) if isinstance(d["handler_config"], str) else dict(d["handler_config"])
        result.append(d)
    return result


# ── flow helpers ─────────────────────────────────────────────────────────────

async def get_flow(flow_id: str) -> dict | None:
    """Fetch flow definition from Postgres. Returns None if not found / inactive."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, definition FROM flows WHERE id=$1::uuid AND is_active=true",
            flow_id,
        )
    if not row:
        return None
    d = dict(row)
    defn = d.get("definition")
    if isinstance(defn, str):
        d["definition"] = json.loads(defn)
    elif hasattr(defn, "keys"):
        d["definition"] = dict(defn)
    return d


async def create_flow_execution(flow_id: str, call_uuid: str, entry_node_id: str) -> str:
    """Insert a new flow_executions row. Returns the execution UUID as a string."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO flow_executions (flow_id, call_uuid, current_node_id, status)
               VALUES ($1::uuid, $2, $3, 'running')
               RETURNING id""",
            flow_id, call_uuid, entry_node_id,
        )
    return str(row["id"])


# ── version helpers ───────────────────────────────────────────────────────────

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
