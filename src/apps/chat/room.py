import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from fastapi import WebSocket
from redis.asyncio.client import PubSub

from src.utils.redis import get_redis

logger = logging.getLogger(__name__)

_SEND_TIMEOUT = 10.0
_RETRY_INITIAL = 1.0
_RETRY_MAX = 30.0

_sockets: dict[str, set[WebSocket]] = defaultdict(set)
_listeners: dict[str, asyncio.Task[None]] = {}
_subscribed: set[str] = set()


def _channel(session_id: str) -> str:
    return f'chat:{session_id}'


def _control_channel(session_id: str) -> str:
    return f'chat:control:{session_id}'


def has_clients(session_id: str) -> bool:
    return bool(_sockets.get(session_id))


def is_listening(session_id: str) -> bool:
    return session_id in _subscribed


async def _subscribe(channel: str, session_id: str) -> PubSub | None:
    redis = await get_redis()
    if redis is None:
        return None
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
    except Exception as error:
        logger.warning('pubsub subscribe failed channel=%s session=%s error=%s', channel, session_id, error)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        return None
    return pubsub


async def _drop(session_id: str, websocket: WebSocket) -> None:
    _sockets[session_id].discard(websocket)
    with contextlib.suppress(Exception):
        await websocket.close(code=1011)


async def _deliver_local(session_id: str, data: str) -> None:
    sockets = list(_sockets.get(session_id, ()))
    if not sockets:
        return
    results = await asyncio.gather(*(asyncio.wait_for(websocket.send_text(data), _SEND_TIMEOUT) for websocket in sockets), return_exceptions=True)
    for websocket, result in zip(sockets, results):
        if isinstance(result, Exception):
            logger.warning('deliver failed session=%s error=%s', session_id, result)
            await _drop(session_id, websocket)


async def _forward(session_id: str) -> None:
    delay = _RETRY_INITIAL
    try:
        while True:
            pubsub = await _subscribe(_channel(session_id), session_id)
            if pubsub is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX)
                continue
            delay = _RETRY_INITIAL
            _subscribed.add(session_id)
            try:
                async for message in pubsub.listen():
                    if message['type'] == 'message':
                        await _deliver_local(session_id, message['data'])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('listener lost session=%s error=%s', session_id, error)
            finally:
                _subscribed.discard(session_id)
                with contextlib.suppress(Exception):
                    await pubsub.aclose()
            await asyncio.sleep(_RETRY_INITIAL)
    finally:
        _subscribed.discard(session_id)


async def join(session_id: str, websocket: WebSocket) -> None:
    _sockets[session_id].add(websocket)
    listener = _listeners.get(session_id)
    if listener is not None and not listener.done():
        return
    _listeners[session_id] = asyncio.create_task(_forward(session_id))


async def leave(session_id: str, websocket: WebSocket) -> bool:
    _sockets[session_id].discard(websocket)
    if _sockets.get(session_id):
        return False
    _sockets.pop(session_id, None)
    listener = _listeners.pop(session_id, None)
    if listener is None:
        return True
    listener.cancel()
    try:
        await listener
    except asyncio.CancelledError:
        pass
    except Exception as error:
        logger.warning('listener shutdown failed session=%s error=%s', session_id, error)
    return True


async def publish(session_id: str, payload: dict) -> None:
    data = json.dumps(payload)
    redis = await get_redis()
    if redis is not None:
        try:
            await redis.publish(_channel(session_id), data)
        except Exception as error:
            logger.warning('publish failed session=%s error=%s', session_id, error)
    if not is_listening(session_id):
        await _deliver_local(session_id, data)


async def broadcast_stop(session_id: str) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.publish(_control_channel(session_id), 'stop')
    except Exception as error:
        logger.warning('broadcast stop failed session=%s error=%s', session_id, error)


async def listen_stop(session_id: str, on_stop: Callable[[], Awaitable[None]]) -> None:
    channel = _control_channel(session_id)
    delay = _RETRY_INITIAL
    while True:
        pubsub = await _subscribe(channel, session_id)
        if pubsub is None:
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX)
            continue
        delay = _RETRY_INITIAL
        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    await on_stop()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('listen stop lost session=%s error=%s', session_id, error)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
        await asyncio.sleep(_RETRY_INITIAL)
