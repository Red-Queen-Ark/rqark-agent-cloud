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


def _channel(room: str) -> str:
    return f'chat:{room}'


def _control_channel(room: str) -> str:
    return f'chat:control:{room}'


def has_clients(room: str) -> bool:
    return bool(_sockets.get(room))


def is_listening(room: str) -> bool:
    return room in _subscribed


async def _subscribe(channel: str, room: str) -> PubSub | None:
    redis = await get_redis()
    if redis is None:
        return None
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
    except Exception as error:
        logger.warning('pubsub subscribe failed channel=%s room=%s error=%s', channel, room, error)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        return None
    return pubsub


async def _drop(room: str, websocket: WebSocket) -> None:
    _sockets[room].discard(websocket)
    with contextlib.suppress(Exception):
        await websocket.close(code=1011)


async def _deliver_local(room: str, data: str) -> None:
    sockets = list(_sockets.get(room, ()))
    if not sockets:
        return
    results = await asyncio.gather(*(asyncio.wait_for(websocket.send_text(data), _SEND_TIMEOUT) for websocket in sockets), return_exceptions=True)
    for websocket, result in zip(sockets, results):
        if isinstance(result, Exception):
            logger.warning('deliver failed room=%s error=%s', room, result)
            await _drop(room, websocket)


async def _forward(room: str) -> None:
    delay = _RETRY_INITIAL
    try:
        while True:
            pubsub = await _subscribe(_channel(room), room)
            if pubsub is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX)
                continue
            delay = _RETRY_INITIAL
            _subscribed.add(room)
            try:
                async for message in pubsub.listen():
                    if message['type'] == 'message':
                        await _deliver_local(room, message['data'])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('listener lost room=%s error=%s', room, error)
            finally:
                _subscribed.discard(room)
                with contextlib.suppress(Exception):
                    await pubsub.aclose()
            await asyncio.sleep(_RETRY_INITIAL)
    finally:
        _subscribed.discard(room)


async def join(room: str, websocket: WebSocket) -> None:
    _sockets[room].add(websocket)
    listener = _listeners.get(room)
    if listener is not None and not listener.done():
        return
    _listeners[room] = asyncio.create_task(_forward(room))


async def leave(room: str, websocket: WebSocket) -> bool:
    _sockets[room].discard(websocket)
    if _sockets.get(room):
        return False
    _sockets.pop(room, None)
    listener = _listeners.pop(room, None)
    if listener is None:
        return True
    listener.cancel()
    try:
        await listener
    except asyncio.CancelledError:
        pass
    except Exception as error:
        logger.warning('listener shutdown failed room=%s error=%s', room, error)
    return True


async def publish(room: str, payload: dict) -> None:
    data = json.dumps(payload)
    redis = await get_redis()
    if redis is not None:
        try:
            await redis.publish(_channel(room), data)
        except Exception as error:
            logger.warning('publish failed room=%s error=%s', room, error)
    if not is_listening(room):
        await _deliver_local(room, data)


async def broadcast_stop(room: str) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.publish(_control_channel(room), 'stop')
    except Exception as error:
        logger.warning('broadcast stop failed room=%s error=%s', room, error)


async def listen_stop(room: str, on_stop: Callable[[], Awaitable[None]]) -> None:
    channel = _control_channel(room)
    delay = _RETRY_INITIAL
    while True:
        pubsub = await _subscribe(channel, room)
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
            logger.warning('listen stop lost room=%s error=%s', room, error)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
        await asyncio.sleep(_RETRY_INITIAL)
