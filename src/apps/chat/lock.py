import asyncio
from uuid import uuid4

from redis.asyncio import Redis

from src.utils.redis import get_redis

_TTL = 30

_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"


class ChatLock:
    def __init__(self, session_id: str):
        self._key = f'chat:busy:{session_id}'
        self._owner = uuid4().hex
        self._redis: Redis | None = None
        self._heartbeat: asyncio.Task[None] | None = None

    async def _try(self, redis: Redis) -> bool:
        return bool(await redis.set(self._key, self._owner, nx=True, ex=_TTL))

    async def acquire(self) -> bool:
        redis = await get_redis()
        if redis is None:
            return True
        try:
            if not await self._try(redis):
                return False
        except Exception:
            return True
        self._redis = redis
        self._heartbeat = asyncio.create_task(self._renew())
        return True

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(_TTL / 2)
            if self._redis is None:
                return
            try:
                await self._redis.eval(_RENEW, 1, self._key, self._owner, _TTL)
            except Exception:
                return

    async def release(self) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        redis, self._redis = self._redis, None
        if redis is None:
            return
        try:
            await redis.eval(_RELEASE, 1, self._key, self._owner)
        except Exception:
            pass
