import asyncio
import json

import pytest
from conftest import BlockingWebSocket, FakeWebSocket

from src.apps.chat import room


async def wait_until(predicate, timeout=1.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def numbers(socket):
    return [json.loads(item)['n'] for item in socket.sent]


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(room, '_RETRY_INITIAL', 0.01)
    monkeypatch.setattr(room, '_RETRY_MAX', 0.05)


async def test_publish_reaches_joined_socket(room_env):
    socket = FakeWebSocket()
    await room.join('s1', socket)
    await wait_until(lambda: room.is_listening('s1'))
    await room.publish('s1', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])
    assert numbers(socket) == [1]


async def test_publish_delivers_locally_without_listener(room_env):
    socket = FakeWebSocket()
    room._sockets['s2'].add(socket)
    assert room.is_listening('s2') is False
    await room.publish('s2', {'n': 1})
    assert numbers(socket) == [1]


async def test_second_socket_joined_shares_listener(room_env):
    first = FakeWebSocket()
    second = FakeWebSocket()
    await room.join('s3', first)
    await room.join('s3', second)
    await wait_until(lambda: room.is_listening('s3'))
    assert len(room_env.subs['chat:s3']) == 1
    await room.publish('s3', {'n': 1})
    await wait_until(lambda: numbers(first) == [1] and numbers(second) == [1])
    assert numbers(second) == [1]


async def test_broken_socket_does_not_block_others(room_env):
    broken = FakeWebSocket(fail=True)
    healthy = FakeWebSocket()
    await room.join('s4', broken)
    await room.join('s4', healthy)
    await wait_until(lambda: room.is_listening('s4'))
    await room.publish('s4', {'n': 1})
    await wait_until(lambda: numbers(healthy) == [1])
    assert broken.sent == []


async def test_publish_falls_back_to_local_while_listener_down(room_env):
    socket = FakeWebSocket()
    await room.join('s10', socket)
    await wait_until(lambda: room.is_listening('s10'))

    room_env.drop_connections()
    await wait_until(lambda: not room.is_listening('s10'), timeout=2.0)

    await room.publish('s10', {'n': 1})
    assert numbers(socket) == [1]


async def test_listener_recovers_from_pubsub_drop(room_env, fast_retry):
    socket = FakeWebSocket()
    await room.join('s5', socket)
    await wait_until(lambda: room.is_listening('s5'))
    await room.publish('s5', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])

    room_env.drop_connections()
    await wait_until(lambda: not room.is_listening('s5'))
    await wait_until(lambda: room.is_listening('s5'))

    await room.publish('s5', {'n': 2})
    await wait_until(lambda: numbers(socket) == [1, 2])
    assert numbers(socket) == [1, 2]


async def test_listener_keeps_working_after_repeated_drops(room_env, fast_retry):
    socket = FakeWebSocket()
    await room.join('s11', socket)
    await wait_until(lambda: room.is_listening('s11'))
    for index in range(1, 4):
        room_env.drop_connections()
        await wait_until(lambda: not room.is_listening('s11'))
        await wait_until(lambda: room.is_listening('s11'))
        await room.publish('s11', {'n': index})
    await wait_until(lambda: numbers(socket) == [1, 2, 3])
    assert numbers(socket) == [1, 2, 3]


async def test_dead_listener_task_is_replaced_on_join(room_env):
    async def boom():
        raise ConnectionError('listener died hard')

    dead = asyncio.create_task(boom())
    await asyncio.sleep(0)
    assert isinstance(dead.exception(), ConnectionError)
    room._listeners['s12'] = dead

    socket = FakeWebSocket()
    await room.join('s12', socket)
    await wait_until(lambda: room.is_listening('s12'))
    await room.publish('s12', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])
    assert numbers(socket) == [1]


async def test_publish_reaches_redis_without_local_listener(room_env):
    other_worker = room_env.pubsub()
    await other_worker.subscribe('chat:s14')

    socket = FakeWebSocket()
    room._sockets['s14'].add(socket)
    await room.publish('s14', {'n': 1})

    assert other_worker.queue.qsize() == 1
    assert numbers(socket) == [1]


async def test_publish_is_not_duplicated_when_local_listener_is_up(room_env):
    socket = FakeWebSocket()
    await room.join('s15', socket)
    await wait_until(lambda: room.is_listening('s15'))
    await room.publish('s15', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])
    await asyncio.sleep(0.05)
    assert numbers(socket) == [1]


async def test_leave_survives_dead_listener(room_env):
    async def boom():
        raise ConnectionError('listener died hard')

    dead = asyncio.create_task(boom())
    await asyncio.sleep(0)
    assert isinstance(dead.exception(), ConnectionError)
    socket = FakeWebSocket()
    room._sockets['s8'].add(socket)
    room._listeners['s8'] = dead

    assert await room.leave('s8', socket) is True


async def test_leave_during_listener_outage(room_env):
    socket = FakeWebSocket()
    await room.join('s9', socket)
    await wait_until(lambda: room.is_listening('s9'))
    room_env.drop_connections()
    await wait_until(lambda: not room.is_listening('s9'))
    assert await room.leave('s9', socket) is True
    assert room.is_listening('s9') is False


async def test_slow_socket_is_dropped(room_env, monkeypatch):
    monkeypatch.setattr(room, '_SEND_TIMEOUT', 0.05)
    slow = BlockingWebSocket()
    fast = FakeWebSocket()
    await room.join('s13', slow)
    await room.join('s13', fast)
    await wait_until(lambda: room.is_listening('s13'))

    await room.publish('s13', {'n': 1})
    await wait_until(lambda: numbers(fast) == [1])
    await room.publish('s13', {'n': 2})
    await wait_until(lambda: numbers(fast) == [1, 2])

    assert numbers(fast) == [1, 2]
    assert slow.closed is True
    assert slow not in room._sockets['s13']


async def test_leave_cancels_listener_when_last_socket_leaves(room_env):
    first = FakeWebSocket()
    second = FakeWebSocket()
    await room.join('s7', first)
    await room.join('s7', second)
    assert await room.leave('s7', first) is False
    assert await room.leave('s7', second) is True
    assert 's7' not in room._listeners
    assert room.is_listening('s7') is False
    assert not room.has_clients('s7')
