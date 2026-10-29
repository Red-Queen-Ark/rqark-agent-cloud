import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import numpy as np
from opencc import OpenCC

from src.apps.asr.engines import AsrEngine, build_engine
from src.apps.asr.errors import AsrBusyError, AsrTimeoutError
from src.apps.asr.postprocess import make_converter, postprocess
from src.config import ASR_CONCURRENCY, ASR_ENGINE, ASR_MODEL, ASR_QUEUE_DEPTH, ASR_TIMEOUT

logger = logging.getLogger(__name__)

_engine: AsrEngine | None = None
_converter: OpenCC | None = None
_converter_ready = False
_load_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_pending: set[Future] = set()
_pending_lock = threading.Lock()


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


def get_pool() -> ThreadPoolExecutor:
    """A pool of ASR_CONCURRENCY threads, which is what actually caps concurrency.

    A semaphore released by the awaiting coroutine leaks its permit when a request times
    out: the thread keeps running, so the slot must stay taken. Letting the executor own the
    limit means a timed out job still holds exactly one worker and nothing is over counted,
    and a stuck engine can never lock the endpoint out for good.
    """
    global _pool
    if _pool is not None:
        return _pool
    with _load_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=max(1, ASR_CONCURRENCY), thread_name_prefix='asr')
    return _pool


def _forget(future: Future) -> None:
    with _pending_lock:
        _pending.discard(future)


def _submit(engine: AsrEngine, audio: np.ndarray) -> Future:
    pool = get_pool()
    with _pending_lock:
        if len(_pending) >= max(1, ASR_CONCURRENCY) + max(0, ASR_QUEUE_DEPTH):
            raise AsrBusyError(f'the asr queue is full, {ASR_CONCURRENCY} transcription(s) already running')
        future = pool.submit(engine.transcribe, audio)
        _pending.add(future)
    future.add_done_callback(_forget)
    return future


def reset() -> None:
    global _engine, _converter, _converter_ready, _pool
    _engine = None
    _converter = None
    _converter_ready = False
    with _pending_lock:
        _pending.clear()
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def state() -> dict[str, Any]:
    with _pending_lock:
        queued = len(_pending)
    return {
        'loaded': _engine is not None,
        'engine': _engine.name if _engine is not None else None,
        'configured': ASR_ENGINE or 'auto',
        'model': ASR_MODEL or 'default',
        'concurrency': ASR_CONCURRENCY,
        'queued': queued,
        'opencc': get_converter() is not None,
    }


async def transcribe(audio: np.ndarray) -> str:
    """Transcribe one utterance and normalise the Cantonese text.

    Inference runs on a worker thread so the event loop keeps serving other requests. A
    timeout answers 504 and leaves the job running: Python cannot cancel a thread, and the
    pool slot stays occupied on purpose so a stuck engine cannot be oversubscribed.
    """
    engine = await asyncio.to_thread(get_engine)
    future = _submit(engine, audio)
    try:
        raw = await asyncio.wait_for(asyncio.wrap_future(future), timeout=ASR_TIMEOUT)
    except TimeoutError as error:
        logger.warning('asr timeout engine=%s seconds=%s', engine.name, ASR_TIMEOUT)
        raise AsrTimeoutError(f'transcription took longer than {ASR_TIMEOUT:g}s') from error
    converter = await asyncio.to_thread(get_converter)
    return postprocess(str(raw), converter)
