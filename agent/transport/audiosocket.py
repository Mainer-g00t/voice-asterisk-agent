"""
Pipecat transport for Asterisk's AudioSocket protocol.

Protocol: each frame is [type:1 byte][length:2 bytes big-endian][payload:N bytes]
  0x00 = hangup      (length 0, no payload)
  0x01 = UUID        (16-byte binary call UUID, sent by Asterisk on connect)
  0x03 = DTMF        (1 ASCII byte)
  0x10 = audio       (8 kHz, 16-bit signed, mono, little-endian PCM)

Asterisk sends audio at 8 kHz; most STT providers prefer 16 kHz.
This transport resamples in both directions so the pipeline always sees 16 kHz.
"""

import asyncio
import struct
import uuid
from typing import Optional

from loguru import logger
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

# ── Protocol constants ────────────────────────────────────────────────────────

MSG_HANGUP = 0x00
MSG_UUID = 0x01
MSG_DTMF = 0x03
MSG_AUDIO = 0x10

HEADER_FMT = ">BH"  # big-endian: 1 byte type + 2 byte unsigned length
HEADER_LEN = 3

ASTERISK_SAMPLE_RATE = 8_000
AGENT_SAMPLE_RATE = 16_000


# ── Low-level frame I/O ───────────────────────────────────────────────────────


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(HEADER_LEN)
    msg_type, length = struct.unpack(HEADER_FMT, header)
    payload = await reader.readexactly(length) if length > 0 else b""
    return msg_type, payload


async def write_frame(
    writer: asyncio.StreamWriter, msg_type: int, payload: bytes
) -> None:
    header = struct.pack(HEADER_FMT, msg_type, len(payload))
    writer.write(header + payload)
    await writer.drain()


# ── Input transport ───────────────────────────────────────────────────────────


class AudioSocketInputTransport(BaseInputTransport):
    def __init__(
        self,
        transport: "AudioSocketTransport",
        params: TransportParams,
        **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._reader: Optional[asyncio.StreamReader] = None
        self._call_uuid: str = ""
        self._read_task: Optional[asyncio.Task] = None
        self._resampler = create_stream_resampler()

    def set_connection(self, reader: asyncio.StreamReader, call_uuid: str) -> None:
        self._reader = reader
        self._call_uuid = call_uuid

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._read_task = self.create_task(self._read_loop())
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame) -> None:
        await self._cancel_read_task()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await self._cancel_read_task()
        await super().cancel(frame)

    async def _cancel_read_task(self) -> None:
        if self._read_task:
            await self.cancel_task(self._read_task)
            self._read_task = None

    async def _read_loop(self) -> None:
        try:
            while True:
                msg_type, payload = await read_frame(self._reader)

                if msg_type == MSG_HANGUP:
                    logger.info(f"Hangup received for call {self._call_uuid}")
                    await self.push_frame(EndFrame())
                    break

                elif msg_type == MSG_UUID:
                    # Asterisk re-sends UUID mid-call in some versions; ignore.
                    continue

                elif msg_type == MSG_DTMF:
                    digit = payload.decode("ascii", errors="ignore")
                    logger.debug(f"DTMF digit: {digit!r} (call {self._call_uuid})")

                elif msg_type == MSG_AUDIO:
                    resampled = await self._resampler.resample(
                        payload, ASTERISK_SAMPLE_RATE, AGENT_SAMPLE_RATE
                    )
                    await self.push_audio_frame(
                        InputAudioRawFrame(
                            audio=resampled,
                            sample_rate=AGENT_SAMPLE_RATE,
                            num_channels=1,
                        )
                    )

        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info(f"AudioSocket connection closed for call {self._call_uuid}")
            await self.push_frame(EndFrame())
        except Exception as e:
            logger.error(f"AudioSocket read error (call {self._call_uuid}): {e}")
            await self.push_frame(CancelFrame())


# ── Output transport ──────────────────────────────────────────────────────────


class AudioSocketOutputTransport(BaseOutputTransport):
    def __init__(
        self,
        transport: "AudioSocketTransport",
        params: TransportParams,
        **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._writer: Optional[asyncio.StreamWriter] = None
        self._resampler = create_stream_resampler()

    def set_connection(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        if not self._writer or self._writer.is_closing():
            return False
        try:
            downsampled = await self._resampler.resample(
                frame.audio, frame.sample_rate, ASTERISK_SAMPLE_RATE
            )
            await write_frame(self._writer, MSG_AUDIO, downsampled)
            return True
        except (ConnectionResetError, BrokenPipeError) as e:
            logger.warning(f"AudioSocket write failed: {e}")
            return False

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._transport.cleanup()


# ── Top-level transport ───────────────────────────────────────────────────────


class AudioSocketParams(TransportParams):
    pass


class AudioSocketTransport(BaseTransport):
    def __init__(
        self,
        params: Optional[AudioSocketParams] = None,
        input_name: Optional[str] = None,
        output_name: Optional[str] = None,
    ):
        super().__init__(input_name=input_name, output_name=output_name)
        self._params = params or AudioSocketParams(
            audio_in_enabled=True,
            audio_in_sample_rate=AGENT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=AGENT_SAMPLE_RATE,
        )
        self._input: Optional[AudioSocketInputTransport] = None
        self._output: Optional[AudioSocketOutputTransport] = None

        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    def input(self) -> AudioSocketInputTransport:
        if not self._input:
            self._input = AudioSocketInputTransport(
                self, self._params, name=self._input_name
            )
        return self._input

    def output(self) -> AudioSocketOutputTransport:
        if not self._output:
            self._output = AudioSocketOutputTransport(
                self, self._params, name=self._output_name
            )
        return self._output

    async def connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        call_uuid: str,
    ) -> None:
        self.input().set_connection(reader, call_uuid)
        self.output().set_connection(writer)
        await self._call_event_handler("on_client_connected", call_uuid)

    async def disconnect(self, call_uuid: str) -> None:
        await self._call_event_handler("on_client_disconnected", call_uuid)
        if self._output and self._output._writer:
            self._output._writer.close()

    async def cleanup(self) -> None:
        pass
