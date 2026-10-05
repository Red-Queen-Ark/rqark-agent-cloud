import asyncio
import json

from conftest import BlockingWebSocket, FakeWebSocket

from src.apps.chat import room


async def wait_until(predicate, timeout=1.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def numbers(socket):
    return [json.loads(item)['n'] for item in socket.sent]


async def test_publish_reaches_joined_socket(room_env):
    socket = FakeWebSocket()
    await room.join('s1', socket)
    await wait_until(lambda: 'chat:s1' in room_env.subs)
    await room.publish('s1', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])
    assert numbers(socket) == [1]


async def test_publish_delivers_locally_without_listener(room_env):
    socket = FakeWebSocket()
    room._sockets['s2'].add(socket)
    room._listeners['s2'] = None
    await room.publish('s2', {'n': 1})
    assert numbers(socket) == [1]


async def test_second_socket_joined_shares_listener(room_env):
    first = FakeWebSocket()
    second = FakeWebSocket()
    await room.join('s3', first)
    await room.join('s3', second)
    await wait_until(lambda: len(room_env.pubsubs) == 1)
    await room.publish('s3', {'n': 1})
    await wait_until(lambda: numbers(first) == [1] and numbers(second) == [1])
    assert numbers(second) == [1]


async def test_broken_socket_does_not_block_others(room_env):
    broken = FakeWebSocket(fail=True)
    healthy = FakeWebSocket()
    await room.join('s4', broken)
    await room.join('s4', healthy)
    await wait_until(lambda: 'chat:s4' in room_env.subs)
    await room.publish('s4', {'n': 1})
    await wait_until(lambda: numbers(healthy) == [1])
    assert broken.sent == []


async def test_events_survive_pubsub_drop(room_env):
    socket = FakeWebSocket()
    await room.join('s5', socket)
    await wait_until(lambda: 'chat:s5' in room_env.subs)
    await room.publish('s5', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])

    room_env.drop_connections()
    await room.publish('s5', {'n': 2})
    await asyncio.sleep(0.1)
    await room.publish('s5', {'n': 3})
    await asyncio.sleep(0.1)

    assert numbers(socket) == [1, 2, 3]


async def test_reconnect_after_pubsub_drop_resubscribes(room_env):
    socket = FakeWebSocket()
    await room.join('s6', socket)
    await wait_until(lambda: 'chat:s6' in room_env.subs)
    await room.publish('s6', {'n': 1})
    await wait_until(lambda: numbers(socket) == [1])

    room_env.drop_connections()
    await room.publish('s6', {'n': 2})
    await wait_until(lambda: all(pubsub.closed for pubsub in room_env.pubsubs))

    reconnected = FakeWebSocket()
    await room.join('s6', reconnected)
    assert bool(room_env.subs['chat:s6'])
    await room.publish('s6', {'n': 3})
    await wait_until(lambda: numbers(reconnected) == [3])
    assert numbers(reconnected) == [3]


async def test_leave_survives_dead_listener(room_env):
    socket = FakeWebSocket()
    await room.join('s8', socket)
    await wait_until(lambda: 'chat:s8' in room_env.subs)
    room_env.drop_connections()
    await room.publish('s8', {'n': 1})
    await wait_until(lambda: all(pubsub.closed for pubsub in room_env.pubsubs))
    assert await room.leave('s8', socket) is True


async def test_slow_socket_does_not_stall_session(room_env):
    slow = BlockingWebSocket()
    fast = FakeWebSocket()
    await room.join('s9', slow)
    await room.join('s9', fast)
    await wait_until(lambda: 'chat:s9' in room_env.subs)
    await room.publish('s9', {'n': 1})
    await room.publish('s9', {'n': 2})
    await asyncio.sleep(0.1)
    assert numbers(fast) == [1, 2]
    slow.gate.set()


async def test_leave_cancels_listener_when_last_socket_leaves(room_env):
    first = FakeWebSocket()
    second = FakeWebSocket()
    await room.join('s7', first)
    await room.join('s7', second)
    assert await room.leave('s7', first) is False
    assert await room.leave('s7', second) is True
    assert 's7' not in room._listeners
    assert not room.has_clients('s7')
