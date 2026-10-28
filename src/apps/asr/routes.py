import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from src.apps.asr import audio as audio_module
from src.apps.asr import engine as engine_module
from src.apps.asr.errors import AudioTooLargeError
from src.config import ASR_MAX_BYTES
from src.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/asr', tags=['Asr'])

User = Annotated[dict, Depends(get_current_user)]

READ_CHUNK = 1024 * 1024


class TranscriptionModel(BaseModel):
    text: str
    duration_ms: int
    elapsed_ms: int


class HealthModel(BaseModel):
    loaded: bool
    engine: str | None
    configured: str
    model: str
    concurrency: int
    opencc: bool


async def _read(upload: UploadFile) -> bytes:
    buffer = bytearray()
    try:
        while chunk := await upload.read(READ_CHUNK):
            buffer.extend(chunk)
            if len(buffer) > ASR_MAX_BYTES:
                raise AudioTooLargeError(f'audio is larger than {ASR_MAX_BYTES} bytes')
    finally:
        await upload.close()
    return bytes(buffer)


@router.get('/health', response_model=HealthModel)
async def health():
    return HealthModel(**engine_module.state())


@router.post('', response_model=TranscriptionModel)
async def transcribe(user: User, file: UploadFile = File(...)):
    """Transcribe one uploaded utterance and return Cantonese normalised text.

    The engine is loaded on first call, so the first request pays the load cost and later
    ones only pay inference.
    """
    data = await _read(file)
    samples = await asyncio.to_thread(audio_module.decode, data)
    started = time.monotonic()
    text = await engine_module.transcribe(samples)
    elapsed = int((time.monotonic() - started) * 1000)
    logger.info('asr done user=%s bytes=%d duration_ms=%d elapsed_ms=%d chars=%d', user.get('id'), len(data), audio_module.duration_ms(samples), elapsed, len(text))
    return TranscriptionModel(text=text, duration_ms=audio_module.duration_ms(samples), elapsed_ms=elapsed)
