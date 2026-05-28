"""
asyncio TCP server — one pipeline per incoming AudioSocket call.

Asterisk dials: AudioSocket(${UNIQUEID}, agent:9099)
On connect, Asterisk immediately sends a UUID frame (type 0x01, 16-byte payload).
This server reads that frame, creates a fresh transport + Pipecat pipeline,
and runs it until the call ends.
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
    task = await create_pipeline_task(transport)

    await transport.connect(reader, writer, call_uuid)

    # handle_sigint=False: only the outer process should handle SIGINT
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception as e:
        logger.error(f"Pipeline error for call {call_uuid}: {e}")
    finally:
        await transport.disconnect(call_uuid)
        try:
            writer.close()
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass  # remote side already closed the connection
        logger.info(f"Call {call_uuid} finished")


async def main() -> None:
    server = await asyncio.start_server(handle_call, HOST, PORT)
    addr = server.sockets[0].getsockname()
    logger.info(f"AudioSocket server listening on {addr[0]}:{addr[1]}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
