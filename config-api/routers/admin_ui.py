"""
Admin UI — Jinja2 + htmx HTML pages.
No separate frontend; everything served from this FastAPI service.

Note: Starlette 1.x changed TemplateResponse signature to (request, name, context)
— request is the first positional arg, not part of the context dict.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db
import docker_manager
from routers.agents import _push_snapshot

router = APIRouter()
templates = Jinja2Templates(directory="templates")

STT_OPTIONS = ["local", "openai", "deepgram"]
LLM_OPTIONS = ["local", "openai", "anthropic"]
TTS_OPTIONS = ["local", "openai", "cartesia"]


@router.get("", response_class=HTMLResponse)
async def admin_root(request: Request):
    return RedirectResponse("/admin/routes")


# ── Phone routes UI ───────────────────────────────────────────────────────────

@router.get("/routes", response_class=HTMLResponse)
async def routes_list(request: Request, flash: str = ""):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        routes = await conn.fetch("SELECT * FROM phone_routes ORDER BY did")
        agents = await conn.fetch("SELECT slug, display_name FROM agents WHERE is_active=true ORDER BY display_name")
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
    pool = db.get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO phone_routes (did, agent_slug, description) VALUES ($1, $2, $3)",
                did, agent_slug, description or None,
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
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE phone_routes
               SET agent_slug=$2, description=$3, is_active=$4
               WHERE id=$1""",
            route_id, agent_slug, description or None, is_active == "on",
        )
    return RedirectResponse("/admin/routes?flash=Route+updated", status_code=303)


@router.post("/routes/{route_id}/delete", response_class=HTMLResponse)
async def delete_route(request: Request, route_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM phone_routes WHERE id=$1", route_id)
    return RedirectResponse("/admin/routes?flash=Route+deleted", status_code=303)


# ── Call history UI ───────────────────────────────────────────────────────────

@router.get("/calls", response_class=HTMLResponse)
async def calls_list(request: Request):
    import json as _json
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT call_uuid, agent_slug, did, started_at, ended_at,
                      duration_seconds, turn_count, stt_provider, llm_provider,
                      tts_provider, end_reason
               FROM call_logs ORDER BY started_at DESC NULLS LAST LIMIT 50"""
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM call_logs")
    return templates.TemplateResponse(
        request,
        "calls/list.html",
        {"calls": [dict(r) for r in rows], "total": total},
    )


@router.get("/calls/{call_uuid}", response_class=HTMLResponse)
async def call_detail(request: Request, call_uuid: str):
    import json as _json
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM call_logs WHERE call_uuid=$1", call_uuid)
    if not row:
        return RedirectResponse("/admin/calls")
    call = dict(row)
    if isinstance(call.get("transcript"), str):
        call["transcript"] = _json.loads(call["transcript"])
    return templates.TemplateResponse(request, "calls/detail.html", {"call": call})


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(request: Request):
    agents = await db.list_agents()
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
async def edit_agent_form(request: Request, slug: str):
    data = await db.get_agent_full(slug)
    if not data:
        return RedirectResponse("/admin/agents")

    providers_by_type = {p["provider_type"]: p for p in data["providers"]}

    return templates.TemplateResponse(
        request,
        "agents/edit.html",
        {
            "agent": data["agent"],
            "providers_by_type": providers_by_type,
            "tools": data["tools"],
            "specialists": data["specialists"],
            "versions": data["versions"],
            "stt_options": STT_OPTIONS,
            "llm_options": LLM_OPTIONS,
            "tts_options": TTS_OPTIONS,
            "flash": None,
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
    stt_provider: str = Form("local"),
    stt_model: str = Form(""),
    llm_provider: str = Form("local"),
    llm_model: str = Form(""),
    tts_provider: str = Form("local"),
    tts_model: str = Form(""),
):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            active = is_active == "on"
            await conn.execute(
                """UPDATE agents
                   SET display_name=$2, system_prompt=$3, greeting_trigger=$4, is_active=$5
                   WHERE slug=$1""",
                slug, display_name, system_prompt, greeting_trigger, active,
            )
            for ptype, pname, model in [
                ("stt", stt_provider, stt_model or None),
                ("llm", llm_provider, llm_model or None),
                ("tts", tts_provider, tts_model or None),
            ]:
                await conn.execute(
                    """INSERT INTO provider_configs (agent_id, provider_type, provider_name, model)
                       SELECT id, $2, $3, $4 FROM agents WHERE slug=$1
                       ON CONFLICT (agent_id, provider_type)
                       DO UPDATE SET provider_name=EXCLUDED.provider_name, model=EXCLUDED.model""",
                    slug, ptype, pname, model,
                )

    await _push_snapshot(slug)
    data = await db.get_agent_full(slug)
    providers_by_type = {p["provider_type"]: p for p in data["providers"]}

    return templates.TemplateResponse(
        request,
        "agents/edit.html",
        {
            "agent": data["agent"],
            "providers_by_type": providers_by_type,
            "tools": data["tools"],
            "specialists": data["specialists"],
            "versions": data["versions"],
            "stt_options": STT_OPTIONS,
            "llm_options": LLM_OPTIONS,
            "tts_options": TTS_OPTIONS,
            "flash": "✓ Saved and pushed to Redis",
        },
    )
