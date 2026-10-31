import asyncio
import contextlib
import json

import pytest
from rqark_app.cloud.client import DataCloudError

from src.apps.chat import lock as lock_module
from src.apps.chat import replay
from src.apps.chat import runner as runner_module
from src.apps.chat.runner import ChatCommand, SessionRunner, acquire_runner
from src.apps.chat.scope import room_key

USER = 'u1'


def room(session_id):
    return room_key(USER, session_id)


async def acquire(session_id, token='t'):
    return await acquire_runner(session_id, token, USER)


class FakeHelper:
    instances: list['FakeHelper'] = []
    behavior = None

    def __init__(self, session_id, token, user_id):
        self.session_id = session_id
        self.token = token
        self.user_id = user_id
        self.calls: list[tuple] = []
        self.images: list[list[str]] = []
        self.updated_tokens: list[str | None] = []
        self.closed = False
        self.started = asyncio.Event()
        self.persisted = False
        FakeHelper.instances.append(self)

    async def chat(self, content, provider, agent, images=None):
        self.calls.append((content, provider, agent))
        self.images.append(list(images) if images else [])
        self.started.set()
        if FakeHelper.behavior is not None:
            await FakeHelper.behavior(self, content, provider, agent)
        self.persisted = True

    async def update_token(self, token):
        self.updated_tokens.append(token)
        self.token = token

    async def close(self):
        self.closed = True


async def wait_until(predicate, timeout=1.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def never_stop(session_id, on_stop):
    await asyncio.Event().wait()


class Env:
    def __init__(self):
        self.clients = True
        self.published: list[dict] = []
        self.stops = 0

    def errors(self):
        return [json.loads(json.dumps(item)) for item in self.published]


@pytest.fixture
async def env(monkeypatch):
    FakeHelper.instances.clear()
    FakeHelper.behavior = None
    state = Env()

    async def fake_publish(session_id, payload):
        state.published.append(payload)

    async def fake_broadcast(session_id):
        state.stops += 1

    async def fake_clear(session_id):
        return None

    monkeypatch.setattr(runner_module, 'clear', fake_clear)
    monkeypatch.setattr(runner_module, 'ChatHelper', FakeHelper)
    monkeypatch.setattr(runner_module, 'listen_stop', never_stop)
    monkeypatch.setattr(runner_module, 'publish', fake_publish)
    monkeypatch.setattr(runner_module, 'broadcast_stop', fake_broadcast)
    monkeypatch.setattr(runner_module, 'has_clients', lambda session_id: state.clients)

    async def no_redis():
        return None

    monkeypatch.setattr(lock_module, 'get_redis', no_redis)
    runner_module._runners.clear()
    yield state

    FakeHelper.behavior = None
    for instance in FakeHelper.instances:
        instance.behavior = None
    for session_runner in list(runner_module._runners.values()):
        await shutdown(session_runner)
    runner_module._runners.clear()


async def shutdown(session_runner: SessionRunner):
    chat = session_runner._chat
    if chat is not None and not chat.done():
        chat.cancel()
    session_runner._worker.cancel()
    session_runner._control.cancel()
    for task in (chat, session_runner._worker, session_runner._control):
        if task is None:
            continue
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def error_messages(state):
    messages = []
    for item in state.published:
        payload = item.get('payload')
        if isinstance(payload, dict) and payload.get('type') == 'error':
            messages.append(payload.get('content'))
    return messages


async def test_acquire_runner_adopts_a_new_token(env):
    runner = await acquire('s20', 'old')
    same = await acquire('s20', 'old')
    assert same is runner
    assert runner._helper.token == 'old'

    updated = await acquire('s20', 'new')
    assert updated is runner
    assert runner._helper.token == 'new'


async def test_chat_completes(env):
    runner = await acquire('s1', 't')

    async def behavior(self, content, provider, agent):
        return None

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances and FakeHelper.instances[0].calls)
    assert FakeHelper.instances[0].calls == [('hi', 'p', 'a')]


async def test_chat_error_is_published_and_runner_survives(env):
    runner = await acquire('s2', 't')

    async def behavior(self, content, provider, agent):
        raise DataCloudError(500, 'BOOM', 'provider exploded')

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: 'provider exploded' in error_messages(env))

    FakeHelper.behavior = None
    runner.submit(ChatCommand('again', 'p', 'a'))
    await wait_until(lambda: len(FakeHelper.instances[0].calls) == 2)
    assert len(FakeHelper.instances[0].calls) == 2


async def test_stop_cancels_chat_and_notifies(env):
    runner = await acquire('s3', 't')

    async def behavior(self, content, provider, agent):
        await asyncio.sleep(3600)

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.stop()
    await wait_until(lambda: 'chat stopped' in error_messages(env))
    assert env.stops == 1

    FakeHelper.behavior = None
    runner.submit(ChatCommand('again', 'p', 'a'))
    await wait_until(lambda: len(FakeHelper.instances[0].calls) == 2)
    assert len(FakeHelper.instances[0].calls) == 2


async def test_chat_keeps_running_after_client_disconnect(env):
    env.clients = False
    runner = await acquire('s4', 't')
    release = asyncio.Event()

    async def behavior(self, content, provider, agent):
        await release.wait()

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    release.set()
    await wait_until(lambda: FakeHelper.instances[0].calls and runner._chat is not None and runner._chat.done())
    assert error_messages(env) == []


async def test_runner_retires_when_chat_done_without_clients(env):
    env.clients = False
    runner = await acquire('s5', 't')
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: room('s5') not in runner_module._runners)
    assert FakeHelper.instances[0].closed is True


async def test_retire_is_deferred_while_busy(env):
    env.clients = False
    runner = await acquire('s6', 't')
    release = asyncio.Event()

    async def behavior(self, content, provider, agent):
        await release.wait()

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.request_retire()
    await asyncio.sleep(0.05)
    assert room('s6') in runner_module._runners
    release.set()
    await wait_until(lambda: room('s6') not in runner_module._runners)


async def test_retire_is_skipped_while_clients_connected(env):
    runner = await acquire('s7', 't')
    runner.request_retire()
    await asyncio.sleep(0.05)
    assert room('s7') in runner_module._runners

    FakeHelper.behavior = None
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances and FakeHelper.instances[0].calls)


async def test_chat_submitted_after_retire_still_runs(env):
    env.clients = False
    runner = await acquire('s8', 't')
    release = asyncio.Event()

    async def behavior(self, content, provider, agent):
        await release.wait()

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('first', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.request_retire()
    env.clients = True
    release.set()
    await wait_until(lambda: runner._chat is not None and runner._chat.done())
    await asyncio.sleep(0.05)

    FakeHelper.behavior = None
    runner.submit(ChatCommand('second', 'p', 'a'))
    await wait_until(lambda: len(FakeHelper.instances[0].calls) == 2)
    assert FakeHelper.instances[0].calls[1][0] == 'second'


async def test_second_chat_while_busy_is_rejected(env):
    runner = await acquire('s9', 't')
    release = asyncio.Event()

    async def behavior(self, content, provider, agent):
        await release.wait()

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('first', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.submit(ChatCommand('second', 'p', 'a'))
    await wait_until(lambda: 'already has a running chat' in ' '.join(str(m) for m in error_messages(env)))
    assert len(FakeHelper.instances[0].calls) == 1
    release.set()


async def test_stop_notifies_even_when_cancel_save_fails(env):
    runner = await acquire('s10', 't')

    async def behavior(self, content, provider, agent):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise DataCloudError(0, 'REQUEST_FAILED', 'partial save failed') from None

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.stop()
    await wait_until(lambda: 'chat stopped' in error_messages(env))
    assert 'chat stopped' in error_messages(env)


async def test_commands_are_processed_after_cancel_save_failure(env):
    runner = await acquire('s11', 't')

    async def behavior(self, content, provider, agent):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise DataCloudError(0, 'REQUEST_FAILED', 'partial save failed') from None

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.stop()
    await asyncio.sleep(0.1)

    FakeHelper.behavior = None
    runner.submit(ChatCommand('again', 'p', 'a'))
    await wait_until(lambda: len(FakeHelper.instances[0].calls) == 2)
    assert len(FakeHelper.instances[0].calls) == 2


async def test_replay_is_cleared_at_turn_start(monkeypatch, room_env):
    FakeHelper.instances.clear()
    release = asyncio.Event()

    async def behavior(self, content, provider, agent):
        await release.wait()

    FakeHelper.behavior = behavior

    async def noop_publish(session_id, payload):
        return None

    async def noop_broadcast(session_id):
        return None

    async def no_redis():
        return None

    monkeypatch.setattr(runner_module, 'ChatHelper', FakeHelper)
    monkeypatch.setattr(runner_module, 'listen_stop', never_stop)
    monkeypatch.setattr(runner_module, 'publish', noop_publish)
    monkeypatch.setattr(runner_module, 'broadcast_stop', noop_broadcast)
    monkeypatch.setattr(runner_module, 'has_clients', lambda session_id: False)
    monkeypatch.setattr(lock_module, 'get_redis', no_redis)
    runner_module._runners.clear()

    await replay.record(room('s12'), {'stale': True})
    runner = await acquire('s12', 't')
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    assert await replay.read(room('s12')) == []
    release.set()
    await wait_until(lambda: room('s12') not in runner_module._runners)


async def test_replay_is_cleared_after_persisted_chat(monkeypatch, room_env):
    FakeHelper.instances.clear()

    async def behavior(self, content, provider, agent):
        await replay.record(room('s13'), {'n': 1})

    FakeHelper.behavior = behavior

    async def noop_publish(session_id, payload):
        return None

    async def no_redis():
        return None

    monkeypatch.setattr(runner_module, 'ChatHelper', FakeHelper)
    monkeypatch.setattr(runner_module, 'listen_stop', never_stop)
    monkeypatch.setattr(runner_module, 'publish', noop_publish)
    monkeypatch.setattr(runner_module, 'has_clients', lambda session_id: True)
    monkeypatch.setattr(lock_module, 'get_redis', no_redis)
    runner_module._runners.clear()

    runner = await acquire('s13', 't')
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: runner._chat is not None and runner._chat.done())
    assert FakeHelper.instances[0].persisted is True
    assert await replay.read(room('s13')) == []


async def test_replay_is_kept_when_chat_not_persisted(monkeypatch, room_env):
    FakeHelper.instances.clear()

    async def behavior(self, content, provider, agent):
        await replay.record(room('s14'), {'n': 1})
        raise DataCloudError(500, 'BOOM', 'provider exploded')

    FakeHelper.behavior = behavior

    async def noop_publish(session_id, payload):
        return None

    async def no_redis():
        return None

    monkeypatch.setattr(runner_module, 'ChatHelper', FakeHelper)
    monkeypatch.setattr(runner_module, 'listen_stop', never_stop)
    monkeypatch.setattr(runner_module, 'publish', noop_publish)
    monkeypatch.setattr(runner_module, 'has_clients', lambda session_id: True)
    monkeypatch.setattr(lock_module, 'get_redis', no_redis)
    runner_module._runners.clear()

    runner = await acquire('s14', 't')
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: runner._chat is not None and runner._chat.done())
    assert FakeHelper.instances[0].persisted is False
    assert await replay.read(room('s14')) == [{'n': 1, 'seq': 1}]


async def test_stop_clears_the_replay_of_a_persisted_turn(env, room_env, monkeypatch):
    async def real_clear(session_id):
        await replay.clear(session_id)

    monkeypatch.setattr(runner_module, 'clear', real_clear)
    runner = await acquire('s15', 't')

    async def behavior(self, content, provider, agent):
        await replay.record(room('s15'), {'n': 1})
        self.persisted = True
        await asyncio.sleep(3600)

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.stop()
    await wait_until(lambda: 'chat stopped' in error_messages(env))

    # The stored session is the truth for a stopped turn; leaving the replay
    # behind is what let a resyncing reader fold the same turn in twice.
    assert await replay.read(room('s15')) == []


async def test_stop_keeps_the_replay_when_the_turn_was_not_stored(env, room_env, monkeypatch):
    async def real_clear(session_id):
        await replay.clear(session_id)

    monkeypatch.setattr(runner_module, 'clear', real_clear)
    runner = await acquire('s16', 't')

    async def behavior(self, content, provider, agent):
        await replay.record(room('s16'), {'n': 1})
        await asyncio.sleep(3600)

    FakeHelper.behavior = behavior
    runner.submit(ChatCommand('hi', 'p', 'a'))
    await wait_until(lambda: FakeHelper.instances[0].started.is_set())
    runner.stop()
    await wait_until(lambda: 'chat stopped' in error_messages(env))

    # Nothing stored this turn, so the replay is its only copy.
    assert await replay.read(room('s16')) == [{'n': 1, 'seq': 1}]
