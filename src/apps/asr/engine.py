import asyncio
import logging
import threading
from typing import Any

import numpy as np
from opencc import OpenCC

from src.apps.asr.engines import AsrEngine, build_engine
from src.apps.asr.errors import AsrBusyError, AsrTimeoutError
from src.apps.asr.postprocess import make_converter, postprocess
from src.config import ASR_CONCURRENCY, ASR_ENGINE, ASR_MODEL, ASR_QUEUE_TIMEOUT, ASR_TIMEOUT

logger = logging.getLogger(__name__)

_engine: AsrEngine | None = None
_converter: OpenCC | None = None
_converter_ready = False
_load_lock = threading.Lock()
_slots = threading.BoundedSemaphore(ASR_CONCURRENCY)


def get_engine() -> AsrEngine:
    """Load the configured engine once and keep it resident.

    Loading takes seconds and gigabytes, so it happens on first use rather than at
    startup, and the lock keeps concurrent requests from loading it twice.
    """
    global _engine
    if _engine is not None:
        return _engine
    with _load_lock:
        if _engine is None:
            chosen = ASR_ENGINE or 'auto'
            logger.info('asr engine loading configured=%s model=%s', chosen, ASR_MODEL or 'default')
            _engine = build_engine(ASR_ENGINE, ASR_MODEL)
            logger.info('asr engine ready name=%s', _engine.name)
    return _engine


def get_converter() -> OpenCC | None:
    global _converter, _converter_ready
    if not _converter_ready:
        with _load_lock:
            if not _converter_ready:
                _converter = make_converter()
                _converter_ready = True
    return _converter


def reset() -> None:
    global _engine, _converter, _converter_ready
    _engine = None
    _converter = None
    _converter_ready = False


def state() -> dict[str, Any]:
    return {'loaded': _engine is not None, 'engine': _engine.name if _engine is not None else None, 'configured': ASR_ENGINE or 'auto', 'model': ASR_MODEL or 'default', 'concurrency': ASR_CONCURRENCY, 'opencc': get_converter() is not None}


def _transcribe_sync(engine: AsrEngine, audio: np.ndarray) -> str:
    if not _slots.acquire(timeout=ASR_QUEUE_TIMEOUT):
        raise AsrBusyError(f'the asr queue is full, {ASR_CONCURRENCY} transcription(s) already running')
    try:
        return engine.transcribe(audio)
    finally:
        _slots.release()


async def transcribe(audio: np.ndarray) -> str:
    """Transcribe one utterance and normalise the Cantonese text.

    Inference runs in a worker thread so the event loop keeps serving. A timeout answers
    504 but cannot kill the running thread, which is why ASR_CONCURRENCY bounds how many
    can pile up.
    """
    engine = await asyncio.to_thread(get_engine)
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_transcribe_sync, engine, audio), timeout=ASR_TIMEOUT)
    except TimeoutError as error:
        logger.warning('asr timeout engine=%s seconds=%s', engine.name, ASR_TIMEOUT)
        raise AsrTimeoutError(f'transcription took longer than {ASR_TIMEOUT:g}s') from error
    converter = await asyncio.to_thread(get_converter)
    return postprocess(raw, converter)
