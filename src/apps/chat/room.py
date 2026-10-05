import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from fastapi import WebSocket
from redis.asyncio.client import PubSub

from src.utils.redis import get_redis

logger = logging.getLogger(__name__)

_sockets: dict[str, set[WebSocket]] = defaultdict(set)
_listeners: dict[str, asyncio.Task[None] | None] = {}


def _channel(session_id: str) -> str:
    return f'chat:{session_id}'


def _control_channel(session_id: str) -> str:
    return f'chat:control:{session_id}'


async def _deliver_local(session_id: str, data: str) -> None:
    sockets = list(_sockets.get(session_id, ()))
    if not sockets:
        return
    results = await asyncio.gather(*(websocket.send_text(data) for websocket in sockets), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.warning('deliver failed session=%s error=%s', session_id, result)


async def _forward(session_id: str, pubsub: PubSub) -> None:
    try:
        async for message in pubsub.listen():
            if message['type'] == 'message':
                await _deliver_local(session_id, message['data'])
    finally:
        await pubsub.aclose()


async def _subscribe(session_id: str) -> PubSub | None:
    redis = await get_redis()
    if redis is None:
        return None
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(_channel(session_id))
    except Exception as error:
        logger.warning('pubsub subscribe failed session=%s error=%s', session_id, error)
        await pubsub.aclose()
        return None
    return pubsub


def has_clients(session_id: str) -> bool:
    return bool(_sockets.get(session_id))


async def join(session_id: str, websocket: WebSocket) -> None:
    _sockets[session_id].add(websocket)
    if session_id in _listeners:
        return
    pubsub = await _subscribe(session_id)
    _listeners[session_id] = None if pubsub is None else asyncio.create_task(_forward(session_id, pubsub))


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
    return True


async def publish(session_id: str, payload: dict) -> None:
    data = json.dumps(payload)
    if _listeners.get(session_id) is None:
        await _deliver_local(session_id, data)
        return
    redis = await get_redis()
    if redis is None:
        await _deliver_local(session_id, data)
        return
    try:
        await redis.publish(_channel(session_id), data)
    except Exception as error:
        logger.warning('publish failed session=%s error=%s', session_id, error)
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
    redis = await get_redis()
    if redis is None:
        return
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(_control_channel(session_id))
    except Exception as error:
        logger.warning('listen stop subscribe failed session=%s error=%s', session_id, error)
        await pubsub.aclose()
        return
    try:
        async for message in pubsub.listen():
            if message['type'] == 'message':
                await on_stop()
    finally:
        await pubsub.aclose()
