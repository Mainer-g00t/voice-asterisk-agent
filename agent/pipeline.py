"""
Pipecat pipeline: STT → LLM → TTS, wired to an AudioSocketTransport.

Default providers: Deepgram (STT), Anthropic Claude (LLM), Cartesia (TTS).
Swap by changing the service imports and instantiation below.
All credentials are read from environment variables.
"""

import os

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMMessagesFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.anthropic import AnthropicLLMService
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.deepgram import DeepgramSTTService

from transport.audiosocket import AudioSocketTransport, AudioSocketParams, AGENT_SAMPLE_RATE

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Your responses will be spoken aloud "
    "over a phone call. Keep answers short and conversational — two or three "
    "sentences maximum. Avoid bullet points, markdown, or anything that "
    "doesn't speak naturally."
)


async def create_pipeline_task(transport: AudioSocketTransport) -> PipelineTask:
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
    )

    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-sonnet-4-6",
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        # Aura — clean neutral voice; swap voice_id as preferred
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",
        sample_rate=AGENT_SAMPLE_RATE,
    )

    context = OpenAILLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}]
    )
    context_aggregator = llm.create_context_aggregator(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
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
        await task.queue_frames([LLMMessagesFrame(context.messages)])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, call_uuid: str) -> None:
        logger.info(f"Pipeline ending for call {call_uuid}")
        await task.cancel()

    return task
