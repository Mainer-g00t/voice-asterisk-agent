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
    task, call_log, flow_controller, llm_context = await create_pipeline_task(
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
        import httpx as _httpx
        from pipecat.frames.frames import LLMContextFrame

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
                logger.info(f"[flow] End node — hanging up call {call_uuid}")
                await task.cancel()
                break

            elif ntype == "transfer":
                destination = cfg.get("destination", "")
                context = cfg.get("dialplan_context", "default")
                logger.info(f"[flow] Transfer → {destination!r}")
                try:
                    from ami_client import ami_redirect
                    channel_var = os.environ.get("ACTIVE_CHANNEL", "")
                    await ami_redirect(channel_var or call_uuid, destination, context)
                except Exception as exc:
                    logger.warning(f"[flow] AMI redirect failed: {exc}")
                await task.cancel()
                break

            elif ntype == "say":
                # Inject a fixed message: override system prompt to "say this verbatim",
                # then add a synthetic user turn to trigger LLM → TTS.
                message = cfg.get("message", "")
                if message and flow_controller:
                    logger.info(f"[flow] Say node: {message[:60]}…")
                    llm_context.messages[0] = {
                        "role": "system",
                        "content": f"Say this to the caller exactly as written, with no additions: {message}",
                    }
                    llm_context.add_message({"role": "user", "content": "proceed"})
                    await task.queue_frames([LLMContextFrame(llm_context)])
                    # After LLM speaks (turn_end event), FlowController fires default edge

            elif ntype == "conversation":
                # Entering a new conversation phase — update system prompt and optionally
                # inject a greeting to kick off the LLM.
                new_prompt = cfg.get("system_prompt", "")
                greeting = cfg.get("greeting", "")
                if new_prompt:
                    llm_context.messages[0] = {"role": "system", "content": new_prompt}
                    logger.info(f"[flow] Conversation node: updated system prompt")
                if greeting:
                    llm_context.add_message({"role": "user", "content": greeting})
                    await task.queue_frames([LLMContextFrame(llm_context)])

            elif ntype == "webhook":
                # POST to the configured URL with current flow state, then let
                # FlowController evaluate webhook_field edges on the response.
                url = cfg.get("url", "")
                if url and flow_controller:
                    try:
                        timeout = float(cfg.get("timeout", 10))
                        async with _httpx.AsyncClient() as client:
                            resp = await client.post(
                                url,
                                json={"state": flow_controller.state, "node_id": frame.node_id},
                                timeout=timeout,
                            )
                            result = resp.json()
                        logger.info(f"[flow] Webhook {url} → {resp.status_code}")
                        await flow_controller.on_webhook_result(result)
                    except Exception as exc:
                        logger.warning(f"[flow] Webhook failed: {exc}")
                        if flow_controller:
                            await flow_controller.on_webhook_result({"error": str(exc)})

            elif ntype in ("set_variable", "condition"):
                # Instant nodes: update state / evaluate edges without audio.
                if flow_controller:
                    await flow_controller.execute_instant_node(frame.node_id, ntype, cfg)

            elif ntype == "gather_dtmf":
                # DTMF is already forwarded by the transport as DTMFInputFrame.
                # FlowWatcherProcessor routes it to FlowController.on_dtmf().
                # Nothing extra needed here — just log the wait.
                timeout = cfg.get("dtmf_timeout", 10)
                logger.info(f"[flow] Gather DTMF node — waiting up to {timeout}s")

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
