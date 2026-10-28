import asyncio
import time
from collections import defaultdict

import pytest

from src.apps.chat import lock as lock_module
from src.apps.chat import replay as replay_module
from src.apps.chat import room as room_module

_DROP = object()


class FakePubSub:
    def __init__(self, redis):
        self.redis = redis
        self.channels: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel):
        self.channels.add(channel)
        self.redis.subs[channel].add(self)

    async def listen(self):
        while True:
            item = await self.queue.get()
            if item is _DROP:
                raise ConnectionError('pubsub connection lost')
            yield item

    async def aclose(self):
        self.closed = True
        for channel in self.channels:
            self.redis.subs[channel].discard(self)

    def deliver(self, channel, data):
        self.queue.put_nowait({'type': 'message', 'channel': channel, 'data': data})


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def rpush(self, key, value):
        self.ops.append(('rpush', key, value))
        return self

    def expire(self, key, ttl):
        self.ops.append(('expire', key, ttl))
        return self

    async def execute(self):
        for op in self.ops:
            if op[0] == 'rpush':
                self.redis.lists[op[1]].append(op[2])
            elif op[0] == 'expire':
                self.redis.expiry[op[1]] = time.monotonic() + float(op[2])
        return [True for _ in self.ops]


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.expiry: dict[str, float] = {}
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.subs: dict[str, set[FakePubSub]] = defaultdict(set)
        self.pubsubs: list[FakePubSub] = []
        self.renew_fail = False
        self.publish_fail = False

    def _alive(self, key):
        if key in self.expiry and time.monotonic() >= self.expiry[key]:
            self.store.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.store

    def pubsub(self):
        pubsub = FakePubSub(self)
        self.pubsubs.append(pubsub)
        return pubsub

    def pipeline(self):
        return FakePipeline(self)

    def drop_connections(self):
        for pubsub in self.pubsubs:
            pubsub.queue.put_nowait(_DROP)

    async def publish(self, channel, data):
        if self.publish_fail:
            raise ConnectionError('publish boom')
        for pubsub in list(self.subs.get(channel, ())):
            pubsub.deliver(channel, data)
        return len(self.subs.get(channel, ()))

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.lists:
                del self.lists[key]
                removed += 1
            if key in self.store:
                del self.store[key]
                self.expiry.pop(key, None)
                removed += 1
        return removed

    async def rpush(self, key, *values):
        self.lists[key].extend(values)
        return len(self.lists[key])

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        self.expiry[key] = time.monotonic() + float(ttl)
        return True

    async def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        if end == -1:
            return list(items[start:])
        return list(items[start : end + 1])

    async def set(self, key, value, nx=False, ex=None):
        if nx and self._alive(key):
            return None
        self.store[key] = value
        if ex is not None:
            self.expiry[key] = time.monotonic() + float(ex)
        return True

    async def get(self, key):
        return self.store.get(key) if self._alive(key) else None

    async def eval(self, script, numkeys, *args):
        key, owner = args[0], args[1]
        if 'expire' in script:
            if self.renew_fail:
                raise ConnectionError('renew boom')
            if self._alive(key) and self.store.get(key) == owner:
                self.expiry[key] = time.monotonic() + float(args[2])
                return 1
            return 0
        if not (self._alive(key) and self.store.get(key) == owner):
            return 0
        del self.store[key]
        self.expiry.pop(key, None)
        return 1


class FakeWebSocket:
    def __init__(self, fail=False):
        self.sent: list[str] = []
        self.fail = fail
        self.closed = False

    async def send_text(self, data):
        if self.fail:
            raise ConnectionError('socket closed')
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed = True


class BlockingWebSocket(FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def send_text(self, data):
        await self.gate.wait()
        self.sent.append(data)


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def room_env(monkeypatch, fake_redis):
    import src.apps.chat.room as room

    monkeypatch.setattr(room, 'get_redis', lambda: _returns(fake_redis))
    monkeypatch.setattr(replay_module, 'get_redis', lambda: _returns(fake_redis))
    monkeypatch.setattr(lock_module, 'get_redis', lambda: _returns(fake_redis))
    return fake_redis


def _returns(value):
    async def inner():
        return value

    return inner()


@pytest.fixture(autouse=True)
async def _reset_room():
    room_module._sockets.clear()
    room_module._listeners.clear()
    room_module._subscribed.clear()
    yield
    for listener in list(room_module._listeners.values()):
        listener.cancel()
    room_module._listeners.clear()
    room_module._sockets.clear()
    room_module._subscribed.clear()
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    """Keeps the task lease off a real Redis, and the task loop off a real data cloud."""
    from src.apps.tasks import leader as leader_module
    from src.apps.tasks import loop as loop_module

    fake = FakeRedis()

    async def get_redis():
        return fake

    monkeypatch.setattr(leader_module, 'get_redis', get_redis)
    monkeypatch.setattr(loop_module, 'DATA_CLOUD_URL', '')
    monkeypatch.setattr(loop_module, 'INTERNAL_TOKEN', '')
