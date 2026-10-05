import json
import logging

from src.utils.redis import get_redis

logger = logging.getLogger(__name__)

_TTL = 3600


def _key(session_id: str) -> str:
    return f'chat:replay:{session_id}'


async def clear(session_id: str) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.delete(_key(session_id))
    except Exception as error:
        logger.debug('replay clear failed session=%s error=%s', session_id, error)


async def record(session_id: str, payload: dict) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        key = _key(session_id)
        pipeline = redis.pipeline()
        pipeline.rpush(key, json.dumps(payload))
        pipeline.expire(key, _TTL)
        await pipeline.execute()
    except Exception as error:
        logger.debug('replay record failed session=%s error=%s', session_id, error)


async def read(session_id: str) -> list[dict]:
    redis = await get_redis()
    if redis is None:
        return []
    try:
        items = await redis.lrange(_key(session_id), 0, -1)
    except Exception as error:
        logger.debug('replay read failed session=%s error=%s', session_id, error)
        return []
    events: list[dict] = []
    for item in items:
        try:
            events.append(json.loads(item))
        except ValueError:
            continue
    return events
