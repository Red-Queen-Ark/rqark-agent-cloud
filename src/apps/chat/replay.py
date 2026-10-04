import json

from src.utils.redis import get_redis

_TTL = 3600


def _key(session_id: str) -> str:
    return f'chat:replay:{session_id}'


async def clear(session_id: str) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.delete(_key(session_id))
    except Exception:
        pass


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
    except Exception:
        pass


async def read(session_id: str) -> list[dict]:
    redis = await get_redis()
    if redis is None:
        return []
    try:
        items = await redis.lrange(_key(session_id), 0, -1)
    except Exception:
        return []
    events: list[dict] = []
    for item in items:
        try:
            events.append(json.loads(item))
        except ValueError:
            continue
    return events
