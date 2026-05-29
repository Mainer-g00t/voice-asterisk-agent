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
from routers.agents import _push_snapshot

router = APIRouter()
templates = Jinja2Templates(directory="templates")

STT_OPTIONS = ["local", "openai", "deepgram"]
LLM_OPTIONS = ["local", "openai", "anthropic"]
TTS_OPTIONS = ["local", "openai", "cartesia"]


@router.get("", response_class=HTMLResponse)
async def admin_root(request: Request):
    return RedirectResponse("/admin/agents")


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
