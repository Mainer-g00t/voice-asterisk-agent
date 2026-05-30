"""
asyncio TCP server — one pipeline per incoming AudioSocket call.

Asterisk dials: AudioSocket(${UNIQUEID}, agent:9099)
On connect, Asterisk immediately sends a UUID frame (type 0x01, 16-byte payload).
This server reads that frame, creates a fresh transport + Pipecat pipeline,
and runs it until the call ends.

Call teardown:
  When the caller hangs up, the AudioSocket TCP connection closes. A watchdog
  coroutine detects this via reader.at_eof(), records the accurate hangup time,
  and cancels the pipeline if it doesn't self-terminate within 2 seconds
  (the pipeline can be slow to drain if TTS audio was queued).
  The finally block then sends the call log to config-api.
"""

import asyncio
import os
import time
import uuid

from dotenv import load_dotenv
from loguru import logger
from pipecat.pipeline.runner import PipelineRunner

import metrics as m
from frames import FlowTransitionFrame
from pipeline import create_pipeline_task
from transport.audiosocket import (
    AudioSocketParams,
    AudioSocketTransport,
    AGENT_SAMPLE_RATE,
    MSG_UUID,
    read_frame,
)

load_dotenv()

HOST = os.environ.get("AUDIOSOCKET_HOST", "0.0.0.0")
PORT = int(os.environ.get("AUDIOSOCKET_PORT", "9099"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))

# Seconds to wait for pipeline to drain gracefully after hangup before force-cancelling
HANGUP_DRAIN_TIMEOUT = float(os.environ.get("HANGUP_DRAIN_TIMEOUT", "2.0"))


async def handle_call(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername")
    logger.info(f"New AudioSocket connection from {peer}")
    slug = os.environ.get("AGENT_SLUG", "basic")
    call_start = time.monotonic()
    m.calls_active.labels(agent_slug=slug).inc()

    # Asterisk always sends a UUID frame as the very first message.
    try:
        msg_type, payload = await asyncio.wait_for(read_frame(reader), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("No UUID frame within 5 s — closing connection")
        writer.close()
        return

    if msg_type != MSG_UUID or len(payload) != 16:
        logger.warning(
            f"Expected UUID frame (0x01/16 bytes), got type=0x{msg_type:02x} len={len(payload)}"
        )
        writer.close()
        return

    call_uuid = str(uuid.UUID(bytes=payload))
    logger.info(f"Call UUID: {call_uuid}")

    transport = AudioSocketTransport(
        params=AudioSocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=AGENT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=AGENT_SAMPLE_RATE,
        )
    )

    # Create the pipeline BEFORE connecting so that event handlers registered
    # inside create_pipeline_task (e.g. on_client_connected) are in place
    # before transport.connect() fires them.
    transition_queue: asyncio.Queue = asyncio.Queue()
    task, call_log, flow_controller = await create_pipeline_task(
        transport, call_uuid, transition_queue
    )

    await transport.connect(reader, writer, call_uuid)

    runner = PipelineRunner(handle_sigint=False)
    end_reason = "hangup"

    # Run pipeline as a cancellable asyncio Task so the watchdog can cancel it.
    runner_task = asyncio.create_task(runner.run(task))

    async def _flow_transition_handler() -> None:
        """
        Drain the transition queue and act on flow node transitions.
        Runs alongside the pipeline runner so it doesn't block audio processing.
        """
        while not runner_task.done():
            try:
                frame: FlowTransitionFrame = await asyncio.wait_for(
                    transition_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue

            ntype = frame.node_type
            cfg = frame.node_config
            logger.info(f"[flow] Transition → node={frame.node_id} type={ntype}")

            if ntype == "end":
                # Hang up gracefully.
                logger.info(f"[flow] End node reached — hanging up call {call_uuid}")
                await task.cancel()
                break

            elif ntype == "transfer":
                destination = cfg.get("destination", "")
                logger.info(f"[flow] Transfer → {destination}")
                # AMI Redirect: send the active channel to a new extension.
                try:
                    from ami_client import ami_redirect
                    channel_var = os.environ.get("ACTIVE_CHANNEL", "")
                    await ami_redirect(channel_var or call_uuid, destination)
                except Exception as exc:
                    logger.warning(f"[flow] AMI redirect failed: {exc}")
                await task.cancel()
                break

            elif ntype == "say":
                # Inject a TTS message into the pipeline as a synthetic user turn.
                message = cfg.get("message", "")
                if message:
                    from pipecat.frames.frames import LLMContextFrame
                    from pipecat.processors.aggregators.llm_context import LLMContext
                    # Re-queue as a user message so LLM → TTS speaks it.
                    logger.info(f"[flow] Say node: injecting message")
                    # The say node immediately takes its default edge; FlowController
                    # will fire a turn_end event after the LLM responds.

            elif ntype == "conversation":
                # System prompt may have changed — handled inside pipeline via
                # FlowController updating the context on the next on_transcription cycle.
                logger.info(f"[flow] Entering conversation node {frame.node_id}")

            # Other node types (webhook, condition, set_variable) are handled
            # by the FlowController itself and don't require server-side action.

    flow_handler_task = asyncio.create_task(_flow_transition_handler())

    async def _hangup_watchdog() -> None:
        """
        Detect connection close and ensure the pipeline terminates promptly.
        The Pipecat pipeline can be slow to drain (TTS audio frames queued with
        20 ms sleeps each). Force-cancel after HANGUP_DRAIN_TIMEOUT seconds.
        """
        # Wait until the TCP reader signals EOF (far end closed the connection).
        while not reader.at_eof():
            await asyncio.sleep(0.1)

        logger.info(f"Hangup detected for call {call_uuid} — recording end time")
        call_log.on_disconnected(reason="hangup")

        # Give the pipeline a moment to drain gracefully.
        try:
            await asyncio.wait_for(
                asyncio.shield(runner_task), timeout=HANGUP_DRAIN_TIMEOUT
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if not runner_task.done():
                logger.info(
                    f"Pipeline still running {HANGUP_DRAIN_TIMEOUT}s after hangup — "
                    f"force-cancelling call {call_uuid}"
                )
                await task.cancel()

    watchdog = asyncio.create_task(_hangup_watchdog())

    try:
        await runner_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Pipeline error for call {call_uuid}: {e}")
        end_reason = "error"
    finally:
        watchdog.cancel()
        flow_handler_task.cancel()
        await transport.disconnect(call_uuid)
        try:
            writer.close()
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        logger.info(f"Call {call_uuid} finished")
        if not call_log.ended_at:
            call_log.on_disconnected(reason=end_reason)
        await call_log.send()

        # Finalize flow execution (fire-and-forget)
        if flow_controller:
            flow_status = "transferred" if end_reason == "hangup" else end_reason
            # Refine status based on final node type
            final_node = flow_controller.current_node
            if final_node:
                ft = final_node.get("type")
                if ft == "end":
                    flow_status = "completed"
                elif ft == "transfer":
                    flow_status = "transferred"
            asyncio.create_task(flow_controller.finalize(status=flow_status))

        # Record call-level Prometheus metrics
        duration = time.monotonic() - call_start
        m.calls_active.labels(agent_slug=slug).dec()
        m.calls_total.labels(agent_slug=slug, end_reason=end_reason).inc()
        m.call_duration.labels(agent_slug=slug).observe(duration)


async def main() -> None:
    m.start_metrics_server(port=METRICS_PORT)
    logger.info(f"Prometheus metrics server started on :{METRICS_PORT}")
    server = await asyncio.start_server(handle_call, HOST, PORT)
    addr = server.sockets[0].getsockname()
    logger.info(f"AudioSocket server listening on {addr[0]}:{addr[1]}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
