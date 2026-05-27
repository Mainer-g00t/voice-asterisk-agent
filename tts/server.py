"""
Minimal OpenAI-compatible TTS server wrapping Piper.
POST /v1/audio/speech  {"input": "text", ...}  → raw PCM (16-bit, 24 kHz, mono)

Piper's Amy-low model synthesises at 16 kHz; we upsample to 24 kHz here so
that Pipecat's OpenAITTSService (which always expects 24 kHz) receives correct
audio and can resample it properly for Asterisk (8 kHz).
"""

import io
import wave

import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from piper.voice import PiperVoice
from pydantic import BaseModel
from scipy import signal

TARGET_SAMPLE_RATE = 24_000  # Hz expected by Pipecat's OpenAITTSService

MODEL_PATH = "/models/voice.onnx"
CONFIG_PATH = "/models/voice.onnx.json"

app = FastAPI()
_voice: PiperVoice | None = None


@app.on_event("startup")
async def startup() -> None:
    global _voice
    _voice = PiperVoice.load(MODEL_PATH, config_path=CONFIG_PATH)


class SpeechRequest(BaseModel):
    input: str
    model: str = "piper"
    voice: str = "default"
    response_format: str = "pcm"
    speed: float = 1.0


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> Response:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        _voice.synthesize(req.input, wav_file)

    buf.seek(0)
    with wave.open(buf, "rb") as wav_file:
        src_rate = wav_file.getframerate()
        pcm = wav_file.readframes(wav_file.getnframes())

    # Upsample to TARGET_SAMPLE_RATE (24 kHz) if the model outputs at a
    # different rate (Amy-low is 16 kHz).  scipy.signal.resample_poly keeps
    # integer up/down ratios and avoids floating-point drift.
    if src_rate != TARGET_SAMPLE_RATE:
        from math import gcd
        g = gcd(TARGET_SAMPLE_RATE, src_rate)
        up, down = TARGET_SAMPLE_RATE // g, src_rate // g
        audio = np.frombuffer(pcm, dtype=np.int16)
        audio = signal.resample_poly(audio, up, down).astype(np.int16)
        pcm = audio.tobytes()

    return Response(content=pcm, media_type="audio/pcm")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
