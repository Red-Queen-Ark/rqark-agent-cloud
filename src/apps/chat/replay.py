import json
import logging

from src.utils.redis import get_redis

logger = logging.getLogger(__name__)

_TTL = 3600


def _key(room: str) -> str:
    return f'chat:replay:{room}'


async def clear(room: str) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.delete(_key(room))
    except Exception as error:
        logger.debug('replay clear failed room=%s error=%s', room, error)


async def record(room: str, payload: dict) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        key = _key(room)
        pipeline = redis.pipeline()
        pipeline.rpush(key, json.dumps(payload))
        pipeline.expire(key, _TTL)
        await pipeline.execute()
    except Exception as error:
        logger.debug('replay record failed room=%s error=%s', room, error)


async def read(room: str) -> list[dict]:
    redis = await get_redis()
    if redis is None:
        return []
    try:
        items = await redis.lrange(_key(room), 0, -1)
    except Exception as error:
        logger.debug('replay read failed room=%s error=%s', room, error)
        return []
    events: list[dict] = []
    for item in items:
        try:
            events.append(json.loads(item))
        except ValueError:
            continue
    return events
