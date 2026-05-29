"""
Pipecat pipeline: STT → LLM → TTS, wired to an AudioSocketTransport.

Agent configuration is loaded from Redis at the start of each call.
On a cache miss, the config is fetched from config-api (/internal/agents/{slug}/snapshot)
and the cache is warmed. This means:
  - Config changes made in the admin UI take effect on the next call, no restart needed.
  - In-flight calls finish with the config they started with.

Provider selection, model, specialist prompts, and tool schemas are all data-driven.
Tool *handlers* are registered via HANDLER_REGISTRY (agent/tool_handlers/).
"""

import json
import os

import httpx
import redis.asyncio as aioredis
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)

from call_logger import CallLogger
from transport.audiosocket import AGENT_SAMPLE_RATE, AudioSocketParams, AudioSocketTransport

# ── Redis client (shared across calls) ───────────────────────────────────────

_redis: aioredis.Redis | None = None

AGENT_CONFIG_TTL = 300


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
        )
    return _redis


# ── Config loading ────────────────────────────────────────────────────────────

async def _load_agent_config(slug: str) -> dict:
    """
    1. Try Redis (fast path, ~1 ms).
    2. On miss, fetch from config-api and warm Redis.
    """
    try:
        raw = await _get_redis().get(f"agent:config:{slug}")
        if raw:
            logger.debug(f"Config cache hit for agent '{slug}'")
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"Redis read failed: {e}")

    logger.info(f"Config cache miss for '{slug}' — fetching from config-api")
    config_api_url = os.environ.get("CONFIG_API_URL", "http://config-api:8080")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{config_api_url}/internal/agents/{slug}/snapshot",
            timeout=5.0,
        )
        resp.raise_for_status()
        config = resp.json()

    # Warm cache (best-effort)
    try:
        await _get_redis().setex(
            f"agent:config:{slug}", AGENT_CONFIG_TTL, json.dumps(config)
        )
    except Exception as e:
        logger.warning(f"Redis write failed (cache not warmed): {e}")

    return config


# ── Provider builders ─────────────────────────────────────────────────────────

def _build_stt(provider_cfg: dict):
    name = provider_cfg.get("name", "local")
    if name == "local":
        from pipecat.services.openai.stt import OpenAISTTService
        return OpenAISTTService(api_key="local", base_url="http://stt:8000/v1")
    elif name == "openai":
        from pipecat.services.openai.stt import OpenAISTTService
        return OpenAISTTService(api_key=os.environ["OPENAI_API_KEY"])
    else:  # deepgram
        from pipecat.services.deepgram.stt import DeepgramSTTService
        return DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])


def _build_llm(provider_cfg: dict):
    name = provider_cfg.get("name", "local")
    if name == "local":
        from pipecat.services.ollama.llm import OLLamaLLMService
        return OLLamaLLMService(
            model=provider_cfg.get("model") or os.environ.get("OLLAMA_MODEL", "smollm2:135m"),
            base_url="http://llm:11434/v1",
        )
    elif name == "openai":
        from pipecat.services.openai.llm import OpenAILLMService
        return OpenAILLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            model=provider_cfg.get("model") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    else:  # anthropic
        from pipecat.services.anthropic.llm import AnthropicLLMService
        return AnthropicLLMService(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=provider_cfg.get("model") or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        )


def _build_tts(provider_cfg: dict):
    name = provider_cfg.get("name", "local")
    if name == "local":
        from pipecat.services.openai.tts import OpenAITTSService
        return OpenAITTSService(
            api_key="local",
            base_url="http://tts:5000/v1",
            settings=OpenAITTSService.Settings(voice="alloy"),
            sample_rate=24_000,
        )
    elif name == "openai":
        from pipecat.services.openai.tts import OpenAITTSService
        return OpenAITTSService(
            api_key=os.environ["OPENAI_API_KEY"],
            voice=provider_cfg.get("voice") or os.environ.get("OPENAI_TTS_VOICE", "alloy"),
            sample_rate=AGENT_SAMPLE_RATE,
        )
    else:  # cartesia
        from pipecat.services.cartesia.tts import CartesiaTTSService
        return CartesiaTTSService(
            api_key=os.environ["CARTESIA_API_KEY"],
            settings=CartesiaTTSService.Settings(
                voice=provider_cfg.get("voice_id") or os.environ.get("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
            ),
            sample_rate=AGENT_SAMPLE_RATE,
        )


# ── Tool schema + handler wiring ──────────────────────────────────────────────

def _build_tools_schema(tool_configs: list[dict]) -> ToolsSchema | None:
    if not tool_configs:
        return None
    schemas = [
        FunctionSchema(
            name=tc["tool_name"],
            description=tc["description"],
            properties=tc["parameters"],
            required=tc["required_params"],
        )
        for tc in sorted(tool_configs, key=lambda x: x.get("sort_order", 0))
    ]
    return ToolsSchema(standard_tools=schemas)


def _register_tools(llm, tool_configs: list[dict], agent_config: dict) -> None:
    from tool_handlers import HANDLER_REGISTRY
    for tc in tool_configs:
        handler_type = tc["handler_type"]
        tool_name = tc["tool_name"]
        factory = HANDLER_REGISTRY.get(handler_type)
        if factory is None:
            logger.warning(f"No handler for type={handler_type!r}, skipping tool '{tool_name}'")
            continue
        # Factories that need the full config (e.g. specialist_router) accept it;
        # simple handlers can be registered directly.
        try:
            handler = factory(agent_config)
        except TypeError:
            handler = factory()
        llm.register_function(tool_name, handler)
        logger.info(f"Registered tool '{tool_name}' (handler_type={handler_type!r})")


# ── Pipeline factory ──────────────────────────────────────────────────────────

async def create_pipeline_task(
    transport: AudioSocketTransport, call_uuid: str
) -> tuple[PipelineTask, CallLogger]:
    slug = os.environ.get("AGENT_SLUG", "basic")
    config = await _load_agent_config(slug)

    system_prompt = config["system_prompt"]
    greeting_trigger = config.get("greeting_trigger", "Hello")
    providers = config.get("providers", {})
    tool_configs = config.get("tools", [])

    stt = _build_stt(providers.get("stt", {}))
    llm = _build_llm(providers.get("llm", {}))
    tts = _build_tts(providers.get("tts", {}))

    if tool_configs:
        _register_tools(llm, tool_configs, config)

    call_log = CallLogger(
        call_uuid=call_uuid,
        agent_slug=slug,
        providers=providers,
        config_api_url=os.environ.get("CONFIG_API_URL", "http://config-api:8080"),
        greeting_trigger=greeting_trigger,
    )

    logger.info(
        f"Pipeline: agent={slug} call={call_uuid} "
        f"STT={providers.get('stt', {}).get('name', 'local')} "
        f"LLM={providers.get('llm', {}).get('name', 'local')} "
        f"TTS={providers.get('tts', {}).get('name', 'local')}"
    )

    tools_schema = _build_tools_schema(tool_configs)
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt}],
        **( {"tools": tools_schema} if tools_schema is not None else {} ),
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            audio_in_sample_rate=AGENT_SAMPLE_RATE,
            audio_out_sample_rate=AGENT_SAMPLE_RATE,
        ),
        enable_rtvi=False,
        idle_timeout_secs=None,
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(t, uuid: str) -> None:
        logger.info(f"Pipeline started for call {uuid}")
        call_log.on_connected(context)
        context.add_message({"role": "user", "content": greeting_trigger})
        await task.queue_frames([LLMContextFrame(context)])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, uuid: str) -> None:
        logger.info(f"Pipeline ending for call {uuid}")
        call_log.on_disconnected(reason="hangup")
        await task.cancel()

    return task, call_log
