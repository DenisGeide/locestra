from __future__ import annotations

import hmac
import tempfile
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

from services.config import get_settings

SETTINGS = get_settings()
MODEL_NAME = SETTINGS.whisper_model
DEVICE = SETTINGS.whisper_device
COMPUTE_TYPE = SETTINGS.whisper_compute_type
VOICE_BEARER_CREDENTIAL = SETTINGS.gateway_credential.get_secret_value()

app = FastAPI(title="Local Voice Module", version="0.1.0")


@app.middleware("http")
async def authenticate_openai_boundary(request: Request, call_next):
    """Protect transcription while keeping bounded health probes available."""

    model_load_control = (
        request.url.path == "/health" and "load_model" in request.query_params
    )
    if not request.url.path.startswith("/v1/") and not model_load_control:
        return await call_next(request)
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if (
        not VOICE_BEARER_CREDENTIAL
        or not supplied
        or not hmac.compare_digest(supplied, VOICE_BEARER_CREDENTIAL)
    ):
        return JSONResponse(
            {
                "error": {
                    "message": "Voice service authentication is required.",
                    "type": "authentication_error",
                    "code": "authentication.required",
                }
            },
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


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
