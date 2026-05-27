"""
Minimal OpenAI-compatible TTS server wrapping Piper.
POST /v1/audio/speech  {"input": "text", ...}  → raw PCM (16-bit, 16 kHz, mono)
"""

import io
import wave

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from piper.voice import PiperVoice
from pydantic import BaseModel

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
        pcm = wav_file.readframes(wav_file.getnframes())

    return Response(content=pcm, media_type="audio/pcm")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
