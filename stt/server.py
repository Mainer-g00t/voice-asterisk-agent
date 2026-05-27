"""
Minimal OpenAI-compatible STT server wrapping faster-whisper.
POST /v1/audio/transcriptions  (multipart: file + model)  → {"text": "..."}
"""

import os
import tempfile

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "tiny")

app = FastAPI()
_model: WhisperModel | None = None


@app.on_event("startup")
async def startup() -> None:
    global _model
    _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    language: str = Form(default="en"),
) -> JSONResponse:
    audio = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio)
        tmp_path = tmp.name
    try:
        segments, _ = _model.transcribe(tmp_path, language=language)
        text = " ".join(s.text.strip() for s in segments)
    finally:
        os.unlink(tmp_path)
    return JSONResponse({"text": text})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
