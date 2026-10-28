import logging
from collections import defaultdict
from typing import Any

from src.config import VOICE_LEASE_TTL, VOICE_MAX_CONNECTIONS, VOICE_MAX_PER_USER, VOICE_REDIS
from src.utils.redis import get_redis

logger = logging.getLogger(__name__)

USER_KEY = 'voice:user:{user_id}'
SLOT_KEY = 'voice:slot:{slot}'


class Registry:
    """Counts live voice sockets so one user cannot pin every model slot.

    In-process state is enough for one uvicorn worker, and production runs several, so the
    counts are also held in Redis as short leases: one key per user and one key per slot.
    A key is claimed with SET NX EX, which is atomic across workers, and expires on its own
    if a worker dies mid connection. Redis being absent degrades to per-worker counting
    rather than refusing every socket.
    """

    def __init__(self, max_connections: int = VOICE_MAX_CONNECTIONS, max_per_user: int = VOICE_MAX_PER_USER, ttl: int = VOICE_LEASE_TTL, use_redis: bool = VOICE_REDIS):
        self.max_connections = max(1, max_connections)
        self.max_per_user = max(1, max_per_user)
        self.ttl = max(30, ttl)
        self.use_redis = use_redis
        self._sockets: dict[str, set[str]] = defaultdict(set)
        self._slots: dict[str, int] = {}

    @property
    def total(self) -> int:
        return sum(len(sockets) for sockets in self._sockets.values())

    def reason_to_refuse(self, user_id: str) -> str | None:
        if self.total >= self.max_connections:
            return 'the voice service is at its connection limit'
        if len(self._sockets.get(user_id, ())) >= self.max_per_user:
            return 'this account already has a voice connection'
        return None

    def add(self, user_id: str, socket: Any) -> None:
        self._sockets[user_id].add(str(id(socket)))

    def remove(self, user_id: str, socket: Any) -> None:
        sockets = self._sockets.get(user_id)
        if not sockets:
            return
        sockets.discard(str(id(socket)))
        if not sockets:
            self._sockets.pop(user_id, None)

    async def acquire(self, user_id: str, socket_id: str) -> str | None:
        local = self.reason_to_refuse(user_id)
        if local is not None:
            return local
        redis = await self._redis()
        if redis is not None:
            remote = await self._claim(redis, user_id, socket_id)
            if remote is not None:
                return remote
        self._sockets[user_id].add(socket_id)
        return None

    async def release(self, user_id: str, socket_id: str) -> None:
        self._sockets.get(user_id, set()).discard(socket_id)
        if not self._sockets.get(user_id):
            self._sockets.pop(user_id, None)
        slot = self._slots.pop(socket_id, None)
        redis = await self._redis()
        if redis is None:
            return
        await self._drop(redis, USER_KEY.format(user_id=user_id), socket_id)
        if slot is not None:
            await self._drop(redis, SLOT_KEY.format(slot=slot), socket_id)

    async def refresh(self, user_id: str, socket_id: str) -> None:
        redis = await self._redis()
        if redis is None:
            return
        keys = [USER_KEY.format(user_id=user_id)]
        slot = self._slots.get(socket_id)
        if slot is not None:
            keys.append(SLOT_KEY.format(slot=slot))
        for key in keys:
            try:
                if await redis.get(key) == socket_id:
                    await redis.expire(key, self.ttl)
            except Exception as error:
                logger.warning('voice lease refresh failed key=%s error=%s', key, error)

    async def _claim(self, redis: Any, user_id: str, socket_id: str) -> str | None:
        user_key = USER_KEY.format(user_id=user_id)
        try:
            if not await redis.set(user_key, socket_id, nx=True, ex=self.ttl):
                return 'this account already has a voice connection'
            for slot in range(self.max_connections):
                if await redis.set(SLOT_KEY.format(slot=slot), socket_id, nx=True, ex=self.ttl):
                    self._slots[socket_id] = slot
                    return None
            await self._drop(redis, user_key, socket_id)
            return 'the voice service is at its connection limit'
        except Exception as error:
            logger.warning('voice registry degraded error=%s', error)
            return None

    async def _drop(self, redis: Any, key: str, socket_id: str) -> None:
        try:
            if await redis.get(key) == socket_id:
                await redis.delete(key)
        except Exception as error:
            logger.warning('voice lease release failed key=%s error=%s', key, error)

    async def _redis(self) -> Any:
        if not self.use_redis:
            return None
        try:
            return await get_redis()
        except Exception as error:
            logger.warning('voice registry degraded error=%s', error)
            return None

    def state(self) -> dict[str, int]:
        return {'connections': self.total, 'users': len(self._sockets), 'max_connections': self.max_connections, 'max_per_user': self.max_per_user}


registry = Registry()
