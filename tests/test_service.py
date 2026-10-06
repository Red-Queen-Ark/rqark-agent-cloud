import inspect
from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

from src.apps.chat import service as service_module
from src.apps.chat.scope import room_key


class StubRunner:
    def __init__(self):
        self.submitted = []
        self.stops = 0

    def submit(self, command):
        self.submitted.append(command)

    def stop(self):
        self.stops += 1


class FakeConn:
    def __init__(self, actions):
        self.query_params = {'token': 't'}
        self.state = SimpleNamespace()
        self.actions = list(actions)
        self.sent = []
        self.close_code = None

    async def accept(self):
        return None

    async def close(self, code=1000):
        self.close_code = code

    async def send_json(self, data):
        self.sent.append(data)

    async def send_text(self, data):
        self.sent.append(data)

    async def receive_text(self):
        if not self.actions:
            raise WebSocketDisconnect()
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            result = action()
            if inspect.isawaitable(result):
                await result
            return await self.receive_text()
        return action


async def ok_user(token):
    return {'id': 'u1'}


def fake_acquire(runner):
    async def acquire(session_id, token, user_id):
        return runner

    return acquire


def make_env(monkeypatch, allowed=True):
    runner = StubRunner()
    retired = []

    async def authorize(token, session_id, claim=False):
        return allowed

    monkeypatch.setattr(service_module, 'authenticate', ok_user)
    monkeypatch.setattr(service_module, 'authorize_session', authorize)
    monkeypatch.setattr(service_module, 'acquire_runner', fake_acquire(runner))
    monkeypatch.setattr(service_module, 'request_retire', lambda session_id, user_id: retired.append((session_id, user_id)))
    return runner, retired


async def test_disconnect_requests_retire(monkeypatch, room_env):
    runner, retired = make_env(monkeypatch)
    conn = FakeConn(['{"kind":"chat","content":"hi","provider":"p","agent":"a"}'])

    await service_module.chat_websocket(conn, 's1')

    assert len(runner.submitted) == 1
    assert retired == [('s1', 'u1')]


async def test_retire_is_requested_even_when_leave_fails(monkeypatch, room_env):
    runner, retired = make_env(monkeypatch)
    real_leave = service_module.leave

    async def broken_leave(room, websocket):
        await real_leave(room, websocket)
        raise ConnectionError('leave boom')

    monkeypatch.setattr(service_module, 'leave', broken_leave)
    conn = FakeConn([])

    await service_module.chat_websocket(conn, 's4')

    assert retired == [('s4', 'u1')]


async def test_sockets_are_scoped_to_the_user(monkeypatch, room_env):
    runner, retired = make_env(monkeypatch)
    seen: list[str] = []
    real_join = service_module.join

    async def capture_join(room, websocket):
        seen.append(room)
        await real_join(room, websocket)

    monkeypatch.setattr(service_module, 'join', capture_join)
    conn = FakeConn([])

    await service_module.chat_websocket(conn, 's9')

    assert seen == [room_key('u1', 's9')]
    assert retired == [('s9', 'u1')]


async def test_forbidden_session_is_closed(monkeypatch, room_env):
    runner, retired = make_env(monkeypatch, allowed=False)
    conn = FakeConn(['{"kind":"chat","content":"hi","provider":"p","agent":"a"}'])

    await service_module.chat_websocket(conn, 's10')

    assert conn.close_code == 4403
    assert runner.submitted == []
    assert retired == []
