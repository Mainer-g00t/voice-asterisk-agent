"""
Admin UI — Jinja2 + htmx HTML pages.
No separate frontend; everything served from this FastAPI service.

Note: Starlette 1.x changed TemplateResponse signature to (request, name, context)
— request is the first positional arg, not part of the context dict.
"""

import json as _json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth
import db
import docker_manager
from voiceai_common.flow_engine import validate_flow
from routers.agents import _push_snapshot, _get_agent_id

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["tojson"] = lambda v: _json.dumps(v, default=str)

STT_OPTIONS = ["local", "openai", "deepgram"]
LLM_OPTIONS = ["local", "openai", "anthropic"]
TTS_OPTIONS = ["local", "openai", "cartesia"]


@router.get("", response_class=HTMLResponse)
async def admin_root(request: Request):
    return RedirectResponse("/admin/routes")


# ── Phone routes UI ───────────────────────────────────────────────────────────

@router.get("/routes", response_class=HTMLResponse)
async def routes_list(request: Request, flash: str = ""):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        routes = await conn.fetch(
            """SELECT pr.* FROM phone_routes pr
               JOIN agents a ON pr.agent_slug = a.slug
               WHERE ($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)
               ORDER BY pr.did""",
            owner_id,
        )
        agents = await conn.fetch(
            """SELECT slug, display_name FROM agents
               WHERE is_active=true
               AND ($1::uuid IS NULL OR owner_id = $1::uuid OR owner_id IS NULL)
               ORDER BY display_name""",
            owner_id,
        )
    containers = docker_manager.list_running_agents()
    return templates.TemplateResponse(
        request,
        "routes/list.html",
        {
            "routes": [dict(r) for r in routes],
            "agents": [dict(a) for a in agents],
            "containers": containers,
            "flash": flash or None,
        },
    )


@router.post("/routes", response_class=HTMLResponse)
async def create_route(
    request: Request,
    did: str = Form(...),
    agent_slug: str = Form(...),
    description: str = Form(""),
):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Verify the agent is accessible to this owner before creating route
        agent_row = await conn.fetchrow(
            "SELECT slug FROM agents WHERE slug=$1 AND ($2::uuid IS NULL OR owner_id = $2::uuid OR owner_id IS NULL)",
            agent_slug, owner_id,
        )
        if not agent_row:
            return RedirectResponse("/admin/routes?flash=Agent+not+found", status_code=303)
        try:
            await conn.execute(
                "INSERT INTO phone_routes (did, agent_slug, description, owner_id) VALUES ($1, $2, $3, $4::uuid)",
                did, agent_slug, description or None, owner_id,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                pass  # silently ignore duplicate for now
            else:
                raise
    return RedirectResponse("/admin/routes?flash=Route+added", status_code=303)


@router.post("/routes/{route_id}/edit", response_class=HTMLResponse)
async def edit_route(
    request: Request,
    route_id: str,
    agent_slug: str = Form(...),
    description: str = Form(""),
    is_active: str = Form("off"),
):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE phone_routes pr
               SET agent_slug=$2, description=$3, is_active=$4
               FROM agents a
               WHERE pr.id=$1
               AND pr.agent_slug = a.slug
               AND ($5::uuid IS NULL OR a.owner_id = $5::uuid)""",
            route_id, agent_slug, description or None, is_active == "on", owner_id,
        )
    return RedirectResponse("/admin/routes?flash=Route+updated", status_code=303)


@router.post("/routes/{route_id}/delete", response_class=HTMLResponse)
async def delete_route(request: Request, route_id: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """DELETE FROM phone_routes pr
               USING agents a
               WHERE pr.id=$1
               AND pr.agent_slug = a.slug
               AND ($2::uuid IS NULL OR a.owner_id = $2::uuid)""",
            route_id, owner_id,
        )
    return RedirectResponse("/admin/routes?flash=Route+deleted", status_code=303)


# ── Call history UI ───────────────────────────────────────────────────────────

@router.get("/calls", response_class=HTMLResponse)
async def calls_list(
    request: Request,
    page: int = 1,
    agent: str = "",
    direction: str = "",
):
    owner_id = auth.get_owner_id(request)
    page_size = 50
    page = max(1, page)
    offset = (page - 1) * page_size

    # Build WHERE clause from filters
    conditions = ["($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)"]
    params: list = [owner_id]
    if agent:
        params.append(agent)
        conditions.append(f"cl.agent_slug = ${len(params)}")
    if direction:
        params.append(direction)
        conditions.append(f"cl.direction = ${len(params)}")
    where = "WHERE " + " AND ".join(conditions)

    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT cl.call_uuid, cl.agent_slug, cl.did, cl.destination, cl.direction,
                      cl.started_at, cl.ended_at, cl.duration_seconds, cl.turn_count,
                      cl.stt_provider, cl.llm_provider, cl.tts_provider, cl.end_reason
               FROM call_logs cl
               JOIN agents a ON cl.agent_slug = a.slug
               {where}
               ORDER BY cl.started_at DESC NULLS LAST
               LIMIT {page_size} OFFSET {offset}""",
            *params,
        )
        total = await conn.fetchval(
            f"""SELECT COUNT(*) FROM call_logs cl
                JOIN agents a ON cl.agent_slug = a.slug
                {where}""",
            *params,
        )
        agent_slugs = await conn.fetch(
            """SELECT DISTINCT cl.agent_slug FROM call_logs cl
               JOIN agents a ON cl.agent_slug = a.slug
               WHERE ($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)
               ORDER BY cl.agent_slug""",
            owner_id,
        )
    import math
    total_pages = max(1, math.ceil(total / page_size))
    return templates.TemplateResponse(
        request,
        "calls/list.html",
        {
            "calls": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "filter_agent": agent,
            "filter_direction": direction,
            "agent_slugs": [r["agent_slug"] for r in agent_slugs],
        },
    )


@router.get("/calls/{call_uuid}", response_class=HTMLResponse)
async def call_detail(request: Request, call_uuid: str):
    import json as _json
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT cl.* FROM call_logs cl
               JOIN agents a ON cl.agent_slug = a.slug
               WHERE cl.call_uuid=$1
               AND ($2::uuid IS NULL OR a.owner_id = $2::uuid OR a.owner_id IS NULL)""",
            call_uuid, owner_id,
        )
    if not row:
        return RedirectResponse("/admin/calls")
    call = dict(row)
    if isinstance(call.get("transcript"), str):
        call["transcript"] = _json.loads(call["transcript"])
    return templates.TemplateResponse(request, "calls/detail.html", {"call": call})


# ── Global tool library UI ────────────────────────────────────────────────────

@router.get("/tools", response_class=HTMLResponse)
async def tools_list(request: Request, flash: str = ""):
    tools = await db.list_global_tools()
    return templates.TemplateResponse(
        request, "tools/list.html",
        {"tools": tools, "flash": flash or None},
    )


@router.post("/tools/{tool_name}/delete", response_class=HTMLResponse)
async def delete_global_tool_ui(request: Request, tool_name: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tool_definitions WHERE tool_name=$1 AND is_global=TRUE", tool_name
        )
    return RedirectResponse("/admin/tools?flash=Tool+deleted", status_code=303)


# ── Agent-tool assignment UI helpers ─────────────────────────────────────────

@router.post("/agents/{slug}/tools/{tool_name}/delete", response_class=HTMLResponse)
async def delete_agent_tool_ui(request: Request, slug: str, tool_name: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug)
            await conn.execute(
                "DELETE FROM tool_definitions WHERE agent_id=$1 AND tool_name=$2",
                agent_id, tool_name,
            )
    await _push_snapshot(slug)
    return RedirectResponse(f"/admin/agents/{slug}/edit?flash=Tool+deleted", status_code=303)


@router.post("/agents/{slug}/tools/{tool_id}/unassign", response_class=HTMLResponse)
async def unassign_global_tool_ui(request: Request, slug: str, tool_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            agent_id = await _get_agent_id(conn, slug)
            await conn.execute(
                "DELETE FROM agent_tool_refs WHERE agent_id=$1 AND tool_id=$2",
                agent_id, tool_id,
            )
    await _push_snapshot(slug)
    return RedirectResponse(f"/admin/agents/{slug}/edit?flash=Tool+unassigned", status_code=303)


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request):
    owner_id = auth.get_owner_id(request)
    agents = await db.list_agents(owner_id)
    return templates.TemplateResponse(request, "agents/list.html", {"agents": agents})


@router.get("/agents/new", response_class=HTMLResponse)
async def new_agent_form(request: Request):
    return templates.TemplateResponse(
        request,
        "agents/edit.html",
        {
            "agent": None,
            "providers_by_type": {},
            "tools": [],
            "specialists": [],
            "versions": [],
            "stt_options": STT_OPTIONS,
            "llm_options": LLM_OPTIONS,
            "tts_options": TTS_OPTIONS,
            "flash": None,
        },
    )


@router.get("/agents/{slug}/edit", response_class=HTMLResponse)
async def edit_agent_form(request: Request, slug: str, flash: str = ""):
    owner_id = auth.get_owner_id(request)
    data = await db.get_agent_full(slug, owner_id)
    if not data:
        return RedirectResponse("/admin/agents")

    providers_by_type = {p["provider_type"]: p for p in data["providers"]}
    global_tools = await db.list_global_tools()
    pool = db.get_pool()
    async with pool.acquire() as conn:
        flows = await conn.fetch(
            """SELECT id, name FROM flows
               WHERE is_active=true
               AND ($1::uuid IS NULL OR owner_id = $1::uuid OR owner_id IS NULL)
               ORDER BY name""",
            owner_id,
        )

    return templates.TemplateResponse(
        request,
        "agents/edit.html",
        {
            "agent": data["agent"],
            "providers_by_type": providers_by_type,
            "tools": data["tools"],
            "specialists": data["specialists"],
            "versions": data["versions"],
            "global_tools": global_tools,
            "flows": [dict(f) for f in flows],
            "stt_options": STT_OPTIONS,
            "llm_options": LLM_OPTIONS,
            "tts_options": TTS_OPTIONS,
            "flash": flash or None,
        },
    )


@router.post("/agents/{slug}", response_class=HTMLResponse)
async def save_agent(
    request: Request,
    slug: str,
    display_name: str = Form(...),
    system_prompt: str = Form(...),
    greeting_trigger: str = Form(...),
    is_active: str = Form("on"),
    flow_id: str = Form(""),
    stt_provider: str = Form("local"),
    stt_model: str = Form(""),
    llm_provider: str = Form("local"),
    llm_model: str = Form(""),
    tts_provider: str = Form("local"),
    tts_model: str = Form(""),
):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            active = is_active == "on"
            clean_flow_id = flow_id.strip() or None
            await conn.execute(
                """UPDATE agents
                   SET display_name=$2, system_prompt=$3, greeting_trigger=$4,
                       is_active=$5, flow_id=$6::uuid
                   WHERE slug=$1
                   AND ($7::uuid IS NULL OR owner_id = $7::uuid)""",
                slug, display_name, system_prompt, greeting_trigger, active,
                clean_flow_id, owner_id,
            )
            def _clean_model(v: str) -> str | None:
                """Return None for empty or literal 'None' strings."""
                return v.strip() or None if v and v.strip() != "None" else None

            for ptype, pname, model in [
                ("stt", stt_provider, _clean_model(stt_model)),
                ("llm", llm_provider, _clean_model(llm_model)),
                ("tts", tts_provider, _clean_model(tts_model)),
            ]:
                await conn.execute(
                    """INSERT INTO provider_configs (agent_id, provider_type, provider_name, model)
                       SELECT id, $2, $3, $4 FROM agents
                       WHERE slug=$1
                       AND ($5::uuid IS NULL OR owner_id = $5::uuid)
                       ON CONFLICT (agent_id, provider_type)
                       DO UPDATE SET provider_name=EXCLUDED.provider_name, model=EXCLUDED.model""",
                    slug, ptype, pname, model, owner_id,
                )

    await _push_snapshot(slug)
    data = await db.get_agent_full(slug, owner_id)
    providers_by_type = {p["provider_type"]: p for p in data["providers"]}
    global_tools = await db.list_global_tools()
    pool = db.get_pool()
    async with pool.acquire() as conn:
        flows = await conn.fetch(
            """SELECT id, name FROM flows
               WHERE is_active=true
               AND ($1::uuid IS NULL OR owner_id = $1::uuid OR owner_id IS NULL)
               ORDER BY name""",
            owner_id,
        )

    return templates.TemplateResponse(
        request,
        "agents/edit.html",
        {
            "agent": data["agent"],
            "providers_by_type": providers_by_type,
            "tools": data["tools"],
            "specialists": data["specialists"],
            "versions": data["versions"],
            "global_tools": global_tools,
            "flows": [dict(f) for f in flows],
            "stt_options": STT_OPTIONS,
            "llm_options": LLM_OPTIONS,
            "tts_options": TTS_OPTIONS,
            "flash": "✓ Saved and pushed to Redis",
        },
    )


# ── Flows UI ──────────────────────────────────────────────────────────────────

@router.get("/flows", response_class=HTMLResponse)
async def flows_list(request: Request, flash: str = ""):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        flows = await conn.fetch(
            """SELECT id, name, description, is_active, updated_at FROM flows
               WHERE ($1::uuid IS NULL OR owner_id = $1::uuid OR owner_id IS NULL)
               ORDER BY name""",
            owner_id,
        )
        # Count executions per flow
        counts = await conn.fetch(
            "SELECT flow_id, COUNT(*) AS run_count FROM flow_executions GROUP BY flow_id"
        )
    count_map = {str(r["flow_id"]): r["run_count"] for r in counts}
    flow_list = []
    for f in flows:
        d = dict(f)
        d["run_count"] = count_map.get(str(d["id"]), 0)
        flow_list.append(d)
    return templates.TemplateResponse(
        request, "flows/list.html",
        {"flows": flow_list, "flash": flash or None},
    )


@router.get("/flows/new", response_class=HTMLResponse)
async def new_flow_form(request: Request):
    starter = {
        "entry_node_id": "n1",
        "nodes": [
            {"id": "n1", "type": "conversation", "label": "Main", "config": {
                "system_prompt": "You are a helpful assistant.",
                "greeting": "Hello! How can I help you today?"
            }},
            {"id": "n2", "type": "end", "label": "End", "config": {}}
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2",
             "condition": {"type": "keyword_matched", "words": ["goodbye", "bye"]}}
        ]
    }
    return templates.TemplateResponse(
        request, "flows/edit.html",
        {"flow": None, "definition_json": _json.dumps(starter, indent=2), "errors": []},
    )


@router.post("/flows/new", response_class=HTMLResponse)
async def create_flow_ui(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    definition_json: str = Form("{}"),
):
    owner_id = auth.get_owner_id(request)
    errors = []
    definition = {}
    try:
        definition = _json.loads(definition_json)
    except Exception:
        errors.append("Invalid JSON in definition field")

    if not errors:
        errors = validate_flow(definition)

    if errors:
        return templates.TemplateResponse(
            request, "flows/edit.html",
            {"flow": None, "name": name, "description": description,
             "definition_json": definition_json, "errors": errors},
        )

    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO flows (name, description, definition, owner_id) VALUES ($1,$2,$3,$4::uuid) RETURNING id",
            name, description or None, _json.dumps(definition), owner_id,
        )
    return RedirectResponse(f"/admin/flows/{row['id']}/edit?flash=Flow+created", status_code=303)


@router.get("/flows/{flow_id}/edit", response_class=HTMLResponse)
async def edit_flow_form(request: Request, flow_id: str, flash: str = ""):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM flows WHERE id=$1::uuid AND ($2::uuid IS NULL OR owner_id = $2::uuid OR owner_id IS NULL)",
            flow_id, owner_id,
        )
    if not row:
        return RedirectResponse("/admin/flows")
    flow = dict(row)
    defn = flow.get("definition", {})
    if isinstance(defn, str):
        defn = _json.loads(defn)
    elif hasattr(defn, "keys"):
        defn = dict(defn)
    return templates.TemplateResponse(
        request, "flows/edit.html",
        {"flow": flow, "definition_json": _json.dumps(defn, indent=2),
         "errors": [], "flash": flash or None},
    )


@router.post("/flows/{flow_id}/edit", response_class=HTMLResponse)
async def save_flow_ui(
    request: Request,
    flow_id: str,
    name: str = Form(...),
    description: str = Form(""),
    definition_json: str = Form("{}"),
):
    owner_id = auth.get_owner_id(request)
    errors = []
    definition = {}
    try:
        definition = _json.loads(definition_json)
    except Exception:
        errors.append("Invalid JSON in definition field")

    if not errors:
        errors = validate_flow(definition)

    pool = db.get_pool()
    async with pool.acquire() as conn:
        flow_row = await conn.fetchrow(
            "SELECT * FROM flows WHERE id=$1::uuid AND ($2::uuid IS NULL OR owner_id = $2::uuid)",
            flow_id, owner_id,
        )

    if not flow_row:
        return RedirectResponse("/admin/flows")

    if errors:
        flow = dict(flow_row)
        return templates.TemplateResponse(
            request, "flows/edit.html",
            {"flow": flow, "name": name, "description": description,
             "definition_json": definition_json, "errors": errors},
        )

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE flows SET name=$2, description=$3, definition=$4, updated_at=NOW()
               WHERE id=$1::uuid
               AND ($5::uuid IS NULL OR owner_id = $5::uuid)""",
            flow_id, name, description or None, _json.dumps(definition), owner_id,
        )
    return RedirectResponse(f"/admin/flows/{flow_id}/edit?flash=Saved", status_code=303)


@router.post("/flows/{flow_id}/delete", response_class=HTMLResponse)
async def delete_flow_ui(request: Request, flow_id: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE flows SET is_active=false, updated_at=NOW()
               WHERE id=$1::uuid
               AND ($2::uuid IS NULL OR owner_id = $2::uuid)""",
            flow_id, owner_id,
        )
    return RedirectResponse("/admin/flows?flash=Flow+deactivated", status_code=303)


@router.get("/flows/{flow_id}/executions", response_class=HTMLResponse)
async def flow_executions(request: Request, flow_id: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        flow = await conn.fetchrow(
            "SELECT id, name FROM flows WHERE id=$1::uuid AND ($2::uuid IS NULL OR owner_id = $2::uuid OR owner_id IS NULL)",
            flow_id, owner_id,
        )
        execs = await conn.fetch(
            """SELECT id, call_uuid, current_node_id, status, state, started_at, ended_at
               FROM flow_executions WHERE flow_id=$1::uuid
               ORDER BY started_at DESC LIMIT 50""",
            flow_id,
        )
    if not flow:
        return RedirectResponse("/admin/flows")
    exec_list = []
    for e in execs:
        d = dict(e)
        if isinstance(d.get("state"), str):
            d["state"] = _json.loads(d["state"])
        exec_list.append(d)
    return templates.TemplateResponse(
        request, "flows/executions.html",
        {"flow": dict(flow), "executions": exec_list},
    )


# ── Settings / API keys UI ────────────────────────────────────────────────────

@router.get("/settings/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request):
    user = getattr(request.state, "user", None)
    is_user_account = user and user.get("user_id") not in ("admin", "dev", None)

    keys = []
    if is_user_account:
        pool = db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, label, created_at, last_used_at FROM api_keys WHERE user_id=$1 ORDER BY created_at DESC",
                user["user_id"],
            )
        keys = [dict(r) for r in rows]

    return templates.TemplateResponse(
        request, "settings/api_keys.html",
        {"keys": keys, "user_account": is_user_account},
    )
