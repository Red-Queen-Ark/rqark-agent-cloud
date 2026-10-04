import asyncio
import json
from collections import defaultdict

from fastapi import WebSocket
from redis.asyncio.client import PubSub

from src.utils.redis import get_redis

_sockets: dict[str, set[WebSocket]] = defaultdict(set)
_listeners: dict[str, asyncio.Task[None] | None] = {}


def _channel(session_id: str) -> str:
    return f'chat:{session_id}'


async def _deliver_local(session_id: str, data: str) -> None:
    sockets = list(_sockets.get(session_id, ()))
    if not sockets:
        return
    await asyncio.gather(*(websocket.send_text(data) for websocket in sockets), return_exceptions=True)


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
    except Exception:
        await pubsub.aclose()
        return None
    return pubsub


async def join(session_id: str, websocket: WebSocket) -> None:
    _sockets[session_id].add(websocket)
    if session_id in _listeners:
        return
    pubsub = await _subscribe(session_id)
    _listeners[session_id] = None if pubsub is None else asyncio.create_task(_forward(session_id, pubsub))


async def leave(session_id: str, websocket: WebSocket) -> None:
    _sockets[session_id].discard(websocket)
    if _sockets.get(session_id):
        return
    _sockets.pop(session_id, None)
    listener = _listeners.pop(session_id, None)
    if listener is None:
        return
    listener.cancel()
    try:
        await listener
    except asyncio.CancelledError:
        pass


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
    except Exception:
        await _deliver_local(session_id, data)
