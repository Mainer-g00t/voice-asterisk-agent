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
import uuid

from dotenv import load_dotenv
from loguru import logger
from pipecat.pipeline.runner import PipelineRunner

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

# Seconds to wait for pipeline to drain gracefully after hangup before force-cancelling
HANGUP_DRAIN_TIMEOUT = float(os.environ.get("HANGUP_DRAIN_TIMEOUT", "2.0"))


async def handle_call(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername")
    logger.info(f"New AudioSocket connection from {peer}")

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
    task, call_log = await create_pipeline_task(transport, call_uuid)

    await transport.connect(reader, writer, call_uuid)

    runner = PipelineRunner(handle_sigint=False)
    end_reason = "hangup"

    # Run pipeline as a cancellable asyncio Task so the watchdog can cancel it.
    runner_task = asyncio.create_task(runner.run(task))

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


async def main() -> None:
    server = await asyncio.start_server(handle_call, HOST, PORT)
    addr = server.sockets[0].getsockname()
    logger.info(f"AudioSocket server listening on {addr[0]}:{addr[1]}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
