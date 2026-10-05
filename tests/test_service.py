import inspect
from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

from src.apps.chat import service as service_module


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
    async def acquire(session_id, token):
        return runner

    return acquire


def make_env(monkeypatch):
    runner = StubRunner()
    retired = []
    monkeypatch.setattr(service_module, 'authenticate', ok_user)
    monkeypatch.setattr(service_module, 'acquire_runner', fake_acquire(runner))
    monkeypatch.setattr(service_module, 'request_retire', lambda session_id: retired.append(session_id))
    return runner, retired


async def test_disconnect_requests_retire(monkeypatch, room_env):
    runner, retired = make_env(monkeypatch)
    conn = FakeConn(['{"kind":"chat","content":"hi","provider":"p","agent":"a"}'])

    await service_module.chat_websocket(conn, 's1')

    assert len(runner.submitted) == 1
    assert retired == ['s1']


async def test_retire_is_requested_even_when_leave_fails(monkeypatch, room_env):
    runner, retired = make_env(monkeypatch)
    real_leave = service_module.leave

    async def broken_leave(session_id, websocket):
        await real_leave(session_id, websocket)
        raise ConnectionError('leave boom')

    monkeypatch.setattr(service_module, 'leave', broken_leave)
    conn = FakeConn([])

    await service_module.chat_websocket(conn, 's4')

    assert retired == ['s4']
