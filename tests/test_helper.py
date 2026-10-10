import asyncio
from types import SimpleNamespace

import pytest
from rqark_app.cloud.client import DataCloudError
from rqark_app.types.session import Session
from rqark_core.types.chat import Chunk, UserMessage
from rqark_core.types.event import AgentPayload

from src.apps.chat import helper as helper_module
from src.apps.chat.scope import room_key


class FakeClient:
    def __init__(self, token):
        self.token = token
        self.calls: list[str] = []
        self.closed = False
        self.session_info: dict | None = {'id': 's1', 'title': '', 'plan_items': [], 'created_at': '2026-01-01T00:00:00+00:00', 'updated_at': '2026-01-01T00:00:00+00:00'}
        self.messages: list[dict] = []
        self.get_session_error: DataCloudError | None = None
        self.updated: dict | None = None

    async def list_agents(self):
        self.calls.append('list_agents')
        return [{'name': 'a'}]

    async def list_providers(self):
        self.calls.append('list_providers')
        return [{'name': 'p'}]

    async def get_session(self, uuid):
        self.calls.append('get_session')
        if self.get_session_error is not None:
            raise self.get_session_error
        return self.session_info or {}

    async def list_session_messages(self, uuid):
        self.calls.append('list_session_messages')
        return self.messages

    async def list_sessions(self, source=None):
        self.calls.append('list_sessions')
        return []

    async def update_session(self, uuid, payload, source=None):
        self.calls.append('update_session')
        self.updated = payload
        return {}

    async def create_session(self, payload, source=None):
        self.calls.append('create_session')
        return {}

    async def aclose(self):
        self.closed = True


class FakeProvider:
    def __init__(self, info):
        self.name = info['name']


class FakeProviderManager:
    def __init__(self, providers):
        self.providers = providers
        self.switched: int | None = None

    def switch_to(self, index):
        self.switched = index
        return True


class FakeSessionManager:
    def __init__(self, fail_save=False):
        self.sessions_map: dict[str, Session] = {}
        self.saved: list[str] = []
        self.fail_save = fail_save

    async def get_or_create(self, session_id):
        if session_id in self.sessions_map:
            return self.sessions_map[session_id]
        session = Session(uuid=session_id)
        self.sessions_map[session_id] = session
        return session

    async def save(self, uuid):
        if self.fail_save:
            raise DataCloudError(0, 'REQUEST_FAILED', 'save boom')
        self.saved.append(uuid)


class FakeInnerHelper:
    def __init__(self):
        self.titles: list[tuple[str, str]] = []
        self.fail_title = False

    async def gen_title(self, uuid, content):
        if self.fail_title:
            raise RuntimeError('title boom')
        self.titles.append((uuid, content))


class FakeApp:
    last: 'FakeApp | None' = None
    behavior = None
    fail_save = False

    def __init__(self, providers, storage):
        FakeApp.last = self
        self.provider_mgr = FakeProviderManager(providers)
        self.session_mgr = FakeSessionManager(FakeApp.fail_save)
        self.chat_helper = FakeInnerHelper()
        self._content = None

    def on_content(self):
        def register(fn):
            self._content = fn
            return fn

        return register

    async def chat(self, content, agent, session_id=None):
        if FakeApp.behavior is not None:
            await FakeApp.behavior(self)
        if self._content is not None:
            for piece in ('hel', 'lo'):
                await self._content(AgentPayload('a:s1', (), Chunk('content', piece)))
        await self.session_mgr.save(session_id or 's1')


@pytest.fixture
def helper_env(monkeypatch):
    FakeApp.last = None
    FakeApp.behavior = None
    FakeApp.fail_save = False
    monkeypatch.setattr(helper_module, '_build_client', FakeClient)
    monkeypatch.setattr(helper_module, '_build_provider', FakeProvider)
    monkeypatch.setattr(helper_module, '_build_agent', lambda info: SimpleNamespace(prompts=[], tools=[]))
    monkeypatch.setattr(helper_module, 'AgentApp', FakeApp)

    async def noop_ensure(self):
        return None

    monkeypatch.setattr(helper_module.Workspace, 'ensure', noop_ensure)

    async def noop(app, session_id):
        return None

    monkeypatch.setattr(helper_module, 'connect_agent_event_to_room', noop)
    yield
    FakeApp.behavior = None
    FakeApp.fail_save = False


async def test_successful_chat_is_persisted(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')
    assert helper.persisted is True
    assert FakeApp.last.session_mgr.saved == ['s1']
    assert FakeApp.last.chat_helper.titles == [('s1', 'hi')]


async def test_cancelled_chat_saves_partial_content_and_is_persisted(helper_env):
    async def behavior(app):
        for piece in ('par', 'tial'):
            await app._content(AgentPayload('a:s1', (), Chunk('content', piece)))
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert helper.persisted is True
    assert FakeApp.last.session_mgr.saved == ['s1']
    assert [type(message).__name__ for message in session.history] == ['UserMessage', 'AssistantMessage']
    assert session.history[0].content == 'hi'
    assert session.history[1].content == 'partial'


async def test_failed_partial_save_is_not_persisted(helper_env):
    async def behavior(app):
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    FakeApp.fail_save = True
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is False


async def test_already_saved_history_is_persisted_without_saving(helper_env):
    async def behavior(app):
        app.session_mgr.sessions_map['s1'].history.append(UserMessage('already in cloud'))
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is True
    assert FakeApp.last.session_mgr.saved == []


async def test_title_failure_after_saved_chat_still_counts_as_persisted(helper_env):
    async def behavior(app):
        app.chat_helper.fail_title = True

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(RuntimeError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is True


async def test_persisted_flag_is_reset_each_turn(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')
    assert helper.persisted is True

    async def behavior(app):
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    FakeApp.fail_save = True

    with pytest.raises(DataCloudError):
        await helper.chat('bye', 'p', 'a')

    assert helper.persisted is False


async def test_session_is_loaded_with_two_requests(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        client.messages = [{'seq': 0, 'data': {'role': 'user', 'content': [{'type': 'text', 'text': 'older'}], 'created_at': '2026-01-01T00:00:00+00:00'}}]
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')

    client = seen[0]
    assert 'list_sessions' not in client.calls
    assert client.calls == ['list_agents', 'list_providers', 'get_session', 'list_session_messages']
    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert [message.content for message in session.history] == ['older']
    assert session.title == ''
    assert FakeApp.last.session_mgr.saved == ['s1']


async def test_missing_session_is_created_empty(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        client.get_session_error = DataCloudError(404, 'NOT_FOUND', 'missing')
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')

    client = seen[0]
    assert client.calls == ['list_agents', 'list_providers', 'get_session']
    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert session.history == []


async def test_session_load_error_is_raised(helper_env, monkeypatch):
    def build(token):
        client = FakeClient(token)
        client.get_session_error = DataCloudError(500, 'INTERNAL', 'cloud down')
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'a')
    assert helper.persisted is False


async def test_update_token_rebuilds_the_client(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 'old-token', 'u1')
    first = helper.client

    await helper.update_token('new-token')

    assert helper.client is not first
    assert helper.client.token == 'new-token'
    assert first.closed is True
    assert helper.token == 'new-token'


async def test_update_token_ignores_the_same_token(helper_env, monkeypatch):
    built: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        built.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 'same', 'u1')
    await helper.update_token('same')
    await helper.update_token('same')
    assert len(built) == 1
    assert helper.client.closed is False


async def test_chat_uses_the_updated_token(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 'stale', 'u1')
    await helper.update_token('fresh')
    await helper.chat('hi', 'p', 'a')

    assert seen[1].token == 'fresh'
    assert seen[1].calls == ['list_agents', 'list_providers', 'get_session', 'list_session_messages']


async def test_agent_not_found_is_not_persisted(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'missing')
    assert helper.persisted is False


async def test_room_and_workspace_are_scoped_to_the_user(helper_env, monkeypatch):
    seen: list[str] = []

    async def capture(app, room):
        seen.append(room)

    monkeypatch.setattr(helper_module, 'connect_agent_event_to_room', capture)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')

    assert seen == [room_key('u1', 's1')]
    assert helper.room == room_key('u1', 's1')
    assert helper.workspace.user_id == 'u1'
    assert helper.workspace.container_name == 'rqark-user-u1-workspace'


async def test_another_user_gets_a_different_room(helper_env):
    a = helper_module.ChatHelper('s1', 't', 'u1')
    b = helper_module.ChatHelper('s1', 't', 'u2')
    assert a.room != b.room
    assert a.workspace.container_name != b.workspace.container_name


async def test_workspace_is_warmed_before_the_agent_runs(helper_env, monkeypatch):
    order: list[str] = []

    async def record_ensure(self):
        order.append('ensure')

    real_init = FakeApp.__init__

    def init(self, providers, storage):
        order.append('app')
        real_init(self, providers, storage)

    monkeypatch.setattr(helper_module.Workspace, 'ensure', record_ensure)
    monkeypatch.setattr(FakeApp, '__init__', init)

    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')

    assert order == ['ensure', 'app']


async def test_unknown_agent_does_not_warm_a_workspace(helper_env, monkeypatch):
    warmed: list[str] = []

    async def record_ensure(self):
        warmed.append('ensure')

    monkeypatch.setattr(helper_module.Workspace, 'ensure', record_ensure)

    helper = helper_module.ChatHelper('s1', 't', 'u1')
    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'missing')

    assert warmed == []


async def test_warmup_failure_aborts_the_turn(helper_env, monkeypatch):
    async def failing_ensure(self):
        raise RuntimeError('docker unreachable')

    monkeypatch.setattr(helper_module.Workspace, 'ensure', failing_ensure)

    helper = helper_module.ChatHelper('s1', 't', 'u1')
    with pytest.raises(RuntimeError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is False
    assert FakeApp.last is None


def _user(seq, message_id, text):
    return {'seq': seq, 'data': {'role': 'user', 'content': [{'type': 'text', 'text': text}], 'created_at': '2026-01-01T00:00:00+00:00', 'id': message_id}}


def _assistant(seq, message_id, text):
    return {'seq': seq, 'data': {'role': 'assistant', 'content': text, 'created_at': '2026-01-01T00:00:00+00:00', 'id': message_id}}


def _revert_client(seen):
    def build(token):
        client = FakeClient(token)
        client.messages = [_user(0, 'm1', 'first'), _assistant(1, 'a1', 'answer'), _user(2, 'm2', 'second')]
        seen.append(client)
        return client

    return build


async def test_revert_trims_and_persists_the_session(helper_env, monkeypatch):
    seen: list[FakeClient] = []
    monkeypatch.setattr(helper_module, '_build_client', _revert_client(seen))
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    session = await helper.revert('m2')

    assert session is not None
    assert [message.content for message in session.history] == ['first', 'answer']
    client = seen[0]
    assert client.calls == ['get_session', 'list_session_messages', 'update_session']
    assert [message['content'] for message in client.updated['history']] == [[{'type': 'text', 'text': 'first'}], 'answer']


async def test_revert_rejects_an_unknown_message(helper_env, monkeypatch):
    seen: list[FakeClient] = []
    monkeypatch.setattr(helper_module, '_build_client', _revert_client(seen))
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    assert await helper.revert('missing') is None
    assert seen[0].calls == ['get_session', 'list_session_messages']


async def test_revert_is_refused_with_a_single_user_turn(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        client.messages = [_user(0, 'm1', 'only')]
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    assert await helper.revert('m1') is None
    assert seen[0].calls == ['get_session', 'list_session_messages']


async def test_revert_session_closes_the_client(helper_env, monkeypatch):
    seen: list[FakeClient] = []
    monkeypatch.setattr(helper_module, '_build_client', _revert_client(seen))

    assert await helper_module.revert_session('s1', 'm2', 't', 'u1') is True
    assert seen[0].closed is True
