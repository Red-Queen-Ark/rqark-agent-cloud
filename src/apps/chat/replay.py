import json
import logging

from src.utils.redis import get_redis

logger = logging.getLogger(__name__)

_TTL = 3600
# The counter outlives the replay list on purpose: a client that keeps its
# last seen seq across a turn must never see the numbers restart underneath it,
# or its dedup would drop live frames as if they were replays.
_SEQ_TTL = 86400 * 7


def _key(room: str) -> str:
    return f'chat:replay:{room}'


def _seq_key(room: str) -> str:
    return f'chat:seq:{room}'


async def clear(room: str) -> None:
    redis = await get_redis()
    if redis is None:
        return
    try:
        await redis.delete(_key(room))
    except Exception as error:
        logger.warning('replay clear failed room=%s error=%s', room, error)


async def record(room: str, payload: dict) -> dict:
    """Append one frame and stamp it with a per-room monotonic seq.

    The seq is what makes replay and live delivery safe to overlap: a reader
    that has applied seq N drops anything it sees again at or below N, so a
    frame that is both replayed and broadcast lands exactly once. The stamped
    frame is returned because the same dict goes out on the wire.
    """
    redis = await get_redis()
    if redis is None:
        return payload
    try:
        seq_key = _seq_key(room)
        seq = await redis.incr(seq_key)
        await redis.expire(seq_key, _SEQ_TTL)
        stamped = {**payload, 'seq': seq}
        key = _key(room)
        pipeline = redis.pipeline()
        pipeline.rpush(key, json.dumps(stamped))
        pipeline.expire(key, _TTL)
        await pipeline.execute()
        return stamped
    except Exception as error:
        logger.warning('replay record failed room=%s error=%s', room, error)
        return payload


async def read(room: str) -> list[dict]:
    redis = await get_redis()
    if redis is None:
        return []
    try:
        items = await redis.lrange(_key(room), 0, -1)
    except Exception as error:
        logger.warning('replay read failed room=%s error=%s', room, error)
        return []
    events: list[dict] = []
    for item in items:
        try:
            events.append(json.loads(item))
        except ValueError:
            continue
    return events
