from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel

from services.config import get_settings

SETTINGS = get_settings()
MODEL_NAME = SETTINGS.whisper_model
DEVICE = SETTINGS.whisper_device
COMPUTE_TYPE = SETTINGS.whisper_compute_type

app = FastAPI(title="Local Voice Module", version="0.1.0")


@lru_cache(maxsize=1)
def model() -> WhisperModel:
    return WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)


@app.get("/health")
def health(load_model: bool = False) -> dict:
    result = {
        "status": "ok",
        "engine": "faster-whisper",
        "model": MODEL_NAME,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "loaded": model.cache_info().currsize > 0,
    }
    if load_model:
        model()
        result["loaded"] = True
    return result


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model_name: str | None = Form(None, alias="model"),
    language: str | None = Form(None),
) -> dict:
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary:
        temporary.write(await file.read())
        temporary_path = Path(temporary.name)
    try:
        segments, info = model().transcribe(
            str(temporary_path),
            language=language or None,
            vad_filter=True,
            beam_size=5,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return {"text": text, "language": info.language, "duration": info.duration}
    finally:
        temporary_path.unlink(missing_ok=True)
