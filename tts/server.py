"""
Minimal OpenAI-compatible TTS server wrapping Piper.
POST /v1/audio/speech  {"input": "text", ...}  → raw PCM (16-bit, 24 kHz, mono)

Piper's Amy-low model synthesises at 16 kHz; we upsample to 24 kHz here so
that Pipecat's OpenAITTSService (which always expects 24 kHz) receives correct
audio and can resample it properly for Asterisk (8 kHz).
"""

import logging
from math import gcd

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from piper.voice import PiperVoice
from pydantic import BaseModel
from scipy import signal

# wave/io no longer needed — piper's synthesize() generator yields raw PCM chunks directly

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 24_000  # Hz — Pipecat's OpenAITTSService requires 24 kHz

MODEL_PATH = "/models/voice.onnx"
CONFIG_PATH = "/models/voice.onnx.json"

app = FastAPI()
_voice: PiperVoice | None = None


@app.on_event("startup")
async def startup() -> None:
    global _voice
    try:
        log.info("Loading Piper TTS model from %s", MODEL_PATH)
        _voice = PiperVoice.load(MODEL_PATH, config_path=CONFIG_PATH)
        log.info("Piper TTS model loaded (sample_rate=%d Hz)", _voice.config.sample_rate)
    except Exception as exc:
        log.exception("Failed to load Piper model: %s", exc)


class SpeechRequest(BaseModel):
    input: str
    model: str = "piper"
    voice: str = "alloy"
    response_format: str = "pcm"
    speed: float = 1.0


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    if _voice is None:
        raise HTTPException(status_code=503, detail="TTS model not loaded yet")

    # In this version of piper-tts, synthesize() is a generator that yields
    # audio chunks with .audio_int16_bytes and .sample_rate attributes.
    try:
        chunks = list(_voice.synthesize(req.input))
    except Exception as exc:
        log.exception("Piper synthesis failed")
        raise HTTPException(status_code=500, detail=f"Synthesis error: {exc}")

    if not chunks:
        raise HTTPException(status_code=500, detail="Synthesis produced no audio")

    src_rate = chunks[0].sample_rate
    pcm = b"".join(c.audio_int16_bytes for c in chunks)

    # Upsample to TARGET_SAMPLE_RATE (24 kHz) if the model outputs at a
    # different rate (Amy-low is 16 kHz).  scipy.signal.resample_poly keeps
    # integer up/down ratios and avoids floating-point drift.
    if src_rate != TARGET_SAMPLE_RATE:
        g = gcd(TARGET_SAMPLE_RATE, src_rate)
        up, down = TARGET_SAMPLE_RATE // g, src_rate // g
        audio = np.frombuffer(pcm, dtype=np.int16)
        audio = signal.resample_poly(audio, up, down).astype(np.int16)
        pcm = audio.tobytes()

    return Response(content=pcm, media_type="audio/pcm")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
