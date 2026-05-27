"""
Pipecat pipeline: STT → LLM → TTS, wired to an AudioSocketTransport.

Providers are selected via environment variables:
  STT_PROVIDER = local (default) | deepgram | openai
  LLM_PROVIDER = local (default) | anthropic | openai
  TTS_PROVIDER = local (default) | cartesia | openai

"local" points to the stt/llm/tts Docker services in docker-compose.yml.
Set the corresponding API key(s) in .env for cloud providers.
"""

import os

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)

from transport.audiosocket import AGENT_SAMPLE_RATE, AudioSocketParams, AudioSocketTransport

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Your responses will be spoken aloud "
    "over a phone call. Keep answers short and conversational — two or three "
    "sentences maximum. Avoid bullet points, markdown, or anything that "
    "doesn't speak naturally."
)


def _build_stt():
    provider = os.environ.get("STT_PROVIDER", "local").lower()
    if provider == "local":
        from pipecat.services.openai.stt import OpenAISTTService
        return OpenAISTTService(api_key="local", base_url="http://stt:8000/v1")
    elif provider == "openai":
        from pipecat.services.openai.stt import OpenAISTTService
        return OpenAISTTService(api_key=os.environ["OPENAI_API_KEY"])
    else:  # deepgram
        from pipecat.services.deepgram.stt import DeepgramSTTService
        return DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])


def _build_llm():
    provider = os.environ.get("LLM_PROVIDER", "local").lower()
    if provider == "local":
        from pipecat.services.ollama.llm import OLLamaLLMService
        return OLLamaLLMService(
            base_url="http://llm:11434/v1",
            model=os.environ.get("OLLAMA_MODEL", "smollm2:135m"),
        )
    elif provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService
        return OpenAILLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    else:  # anthropic
        from pipecat.services.anthropic.llm import AnthropicLLMService
        return AnthropicLLMService(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            settings=AnthropicLLMService.Settings(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            ),
        )


def _build_tts():
    provider = os.environ.get("TTS_PROVIDER", "local").lower()
    if provider == "local":
        from pipecat.services.openai.tts import OpenAITTSService
        return OpenAITTSService(
            api_key="local",
            base_url="http://tts:5000/v1",
            voice="default",
            sample_rate=AGENT_SAMPLE_RATE,
        )
    elif provider == "openai":
        from pipecat.services.openai.tts import OpenAITTSService
        return OpenAITTSService(
            api_key=os.environ["OPENAI_API_KEY"],
            voice=os.environ.get("OPENAI_TTS_VOICE", "alloy"),
            sample_rate=AGENT_SAMPLE_RATE,
        )
    else:  # cartesia
        from pipecat.services.cartesia.tts import CartesiaTTSService
        return CartesiaTTSService(
            api_key=os.environ["CARTESIA_API_KEY"],
            settings=CartesiaTTSService.Settings(
                voice=os.environ.get("CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"),
            ),
            sample_rate=AGENT_SAMPLE_RATE,
        )


async def create_pipeline_task(transport: AudioSocketTransport) -> PipelineTask:
    stt = _build_stt()
    llm = _build_llm()
    tts = _build_tts()

    logger.info(
        f"Pipeline: STT={os.environ.get('STT_PROVIDER', 'local')} "
        f"LLM={os.environ.get('LLM_PROVIDER', 'local')} "
        f"TTS={os.environ.get('TTS_PROVIDER', 'local')}"
    )

    context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}])
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
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(t, call_uuid: str) -> None:
        logger.info(f"Pipeline started for call {call_uuid}")
        context.add_message({"role": "user", "content": "Hello"})
        await task.queue_frames([LLMContextFrame(context)])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, call_uuid: str) -> None:
        logger.info(f"Pipeline ending for call {call_uuid}")
        await task.cancel()

    return task
