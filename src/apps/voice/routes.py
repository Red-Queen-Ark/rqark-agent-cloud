import logging

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel

from src.apps.asr import engine as asr_engine
from src.apps.voice import registry as registry_module
from src.apps.voice import vad, wake
from src.apps.voice.service import voice_websocket
from src.config import VOICE_MAX_SECONDS, VOICE_PREROLL_MS, VOICE_VAD_THRESHOLD, VOICE_WAKE_THRESHOLD

logger = logging.getLogger(__name__)

router = APIRouter(tags=['Voice'], prefix='/voice')


class HealthModel(BaseModel):
    connections: int
    users: int
    max_connections: int
    max_per_user: int
    wake_words: list[str]
    wake_threshold: float
    vad: bool
    vad_threshold: float
    max_seconds: int
    preroll_ms: int
    asr_queued: int


def _wake_words() -> list[str]:
    try:
        return wake.model_names()
    except wake.WakeUnavailableError as error:
        logger.warning('voice health cannot resolve the wake models error=%s', error.message)
        return []


@router.websocket('/ws')
async def voice_socket(websocket: WebSocket):
    await voice_websocket(websocket)


@router.get('/health', response_model=HealthModel)
async def health():
    state = registry_module.registry.state()
    return HealthModel(
        connections=state['connections'],
        users=state['users'],
        max_connections=state['max_connections'],
        max_per_user=state['max_per_user'],
        wake_words=_wake_words(),
        wake_threshold=VOICE_WAKE_THRESHOLD,
        vad=vad.available(),
        vad_threshold=VOICE_VAD_THRESHOLD,
        max_seconds=VOICE_MAX_SECONDS,
        preroll_ms=VOICE_PREROLL_MS,
        asr_queued=asr_engine.state()['queued'],
    )
