import asyncio
from types import SimpleNamespace

import pytest
from rqark_app.cloud.client import DataCloudError
from rqark_app.types.session import Session, SummaryMessage
from rqark_core.types.chat import AssistantMessage, ToolCall, ToolMessage, UserMessage

from src.apps.chat import event as event_module
from src.apps.chat import helper as helper_module
from src.apps.chat.scope import room_key


class FakeConfig:
    def __init__(self, api_key):
        self.api_key = api_key


class FakeClient:
    sops: list[dict] = []
    projects: dict[str, dict] = {}

    def __init__(self, token):
        self.config = FakeConfig(token)
        self.calls: list[str] = []
        self.closed = False
        self.session_info: dict | None = {'id': 's1', 'title': '', 'project_id': None, 'plan_items': [], 'created_at': '2026-01-01T00:00:00+00:00', 'updated_at': '2026-01-01T00:00:00+00:00'}
        self.messages: list[dict] = []
        self.get_session_error: DataCloudError | None = None
        self.updated: dict | None = None

    @property
    def token(self) -> str:
        return self.config.api_key

    async def list_agents(self):
        self.calls.append('list_agents')
        return [{'name': 'a', 'id': 'agent-1'}]

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

    async def get_project(self, uuid):
        self.calls.append('get_project')
        if uuid not in type(self).projects:
            raise DataCloudError(404, 'NOT_FOUND', 'Project not found.')
        return type(self).projects[uuid]

    async def list_sessions(self, source=None):
        self.calls.append('list_sessions')
        return []

    async def list_sops(self):
        self.calls.append('list_sops')
        return self.sops

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

    async def get(self, session_id):
        return self.sessions_map.get(session_id)

    async def save(self, uuid):
        if self.fail_save:
            raise DataCloudError(0, 'REQUEST_FAILED', 'save boom')
        self.saved.append(uuid)


class FakeCompactor:
    summary: str | None = 'a summary'

    def __init__(self):
        self.summaries: list[list] = []

    async def get_summary(self, messages):
        self.summaries.append(list(messages))
        return SummaryMessage(self.summary) if self.summary is not None else None


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
        self.compactor = FakeCompactor()
        self.stream = event_module.RoomStream()
        self.chats: list[str] = []
        self.turn_writes: list[dict] = []

    async def write_turn_memory(self, *, session_id, source, session, extras=None, allow_state_update=True):
        self.turn_writes.append({'session_id': session_id, 'source': source, 'session': session, 'extras': extras, 'allow_state_update': allow_state_update})

    async def chat(self, content, agent, session_id=None, images=None, extras=None, partial=None):
        self.chats.append(content)
        self.images = list(images) if images else []
        session = await self.session_mgr.get(session_id or 's1')
        prefix = session.get_last_chunk_for_chat() if session is not None else []
        turn = [*prefix, UserMessage(content, images=list(images) if images else [])]
        if partial is not None:
            partial[:] = turn
        try:
            if FakeApp.behavior is not None:
                await FakeApp.behavior(self)
        except asyncio.CancelledError:
            # The core hands back the turn it had built; a turn the loop already
            # saved is longer than the prefix this fake started from.
            if partial is not None:
                grown = session.get_last_chunk_for_chat() if session is not None else []
                base = grown if len(grown) > len(prefix) else turn
                partial[:] = [*base, AssistantMessage(self.stream.text)] if self.stream.text else list(base)
            raise
        await self.session_mgr.save(session_id or 's1')


@pytest.fixture
def helper_env(monkeypatch):
    FakeApp.last = None
    FakeApp.behavior = None
    FakeApp.fail_save = False
    FakeClient.sops = []
    FakeClient.projects = {}
    FakeCompactor.summary = 'a summary'
    monkeypatch.setattr(helper_module, '_build_client', FakeClient)
    monkeypatch.setattr(helper_module, '_build_provider', FakeProvider)
    monkeypatch.setattr(helper_module, '_build_agent', lambda info: SimpleNamespace(prompts=[], tools=[]))
    monkeypatch.setattr(helper_module, 'AgentApp', FakeApp)

    async def noop_ensure(self):
        return None

    monkeypatch.setattr(helper_module.Workspace, 'ensure', noop_ensure)

    async def fake_connect(app, room):
        return app.stream

    monkeypatch.setattr(helper_module, 'connect_agent_event_to_room', fake_connect)
    yield
    FakeApp.behavior = None
    FakeApp.fail_save = False


async def test_a_refused_token_waits_for_a_fresh_one(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    assert helper.client.on_unauthorized == helper._wait_for_fresh_token


async def test_a_refresh_that_never_arrives_is_reported(helper_env, monkeypatch):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    monkeypatch.setattr(helper_module, 'TOKEN_REFRESH_WAIT', 0.05)

    with pytest.raises(DataCloudError) as raised:
        await helper._wait_for_fresh_token()

    assert raised.value.status_code == 401


async def test_successful_chat_is_persisted(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')
    assert helper.persisted is True
    assert FakeApp.last.session_mgr.saved == ['s1']
    assert FakeApp.last.chat_helper.titles == [('s1', 'hi')]


async def test_a_finished_turn_is_recorded_in_the_background(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')
    await asyncio.sleep(0)

    writes = FakeApp.last.turn_writes
    assert len(writes) == 1
    assert writes[0]['session_id'] == 's1'
    assert writes[0]['source'] == 'AGENT_CLOUD'
    assert writes[0]['allow_state_update'] is True
    assert set(writes[0]['extras']) == {'data_cloud', 'agent_id', 'source'}


async def test_a_slow_writer_does_not_delay_the_answer(helper_env, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_write(self, **kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(FakeApp, 'write_turn_memory', slow_write)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await asyncio.wait_for(helper.chat('hi', 'p', 'a'), 5)
    assert not started.is_set()
    await asyncio.sleep(0)
    assert started.is_set()
    release.set()
    await asyncio.sleep(0)


async def test_a_cancelled_turn_is_not_recorded(helper_env):
    async def behavior(app):
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')
    await asyncio.sleep(0)

    assert FakeApp.last.turn_writes == []


async def test_cancelled_chat_saves_partial_content_and_is_persisted(helper_env):
    async def behavior(app):
        app.stream.content.extend(['par', 'tial'])
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


async def test_already_saved_history_is_rewritten_without_duplicating(helper_env):
    async def behavior(app):
        app.session_mgr.sessions_map['s1'].history.append(UserMessage('already in cloud'))
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert [message.content for message in session.history] == ['already in cloud']
    assert FakeApp.last.session_mgr.saved == ['s1']
    assert helper.persisted is True


async def test_cancelled_chat_keeps_the_previous_history(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        client.messages = [
            {'seq': 0, 'data': {'role': 'user', 'content': [{'type': 'text', 'text': 'old question'}], 'created_at': '2026-01-01T00:00:00+00:00'}},
            {'seq': 1, 'data': {'role': 'assistant', 'content': 'old answer', 'created_at': '2026-01-01T00:00:00+00:00'}},
        ]
        seen.append(client)
        return client

    async def behavior(app):
        app.stream.content.extend(['par', 'tial'])
        raise asyncio.CancelledError()

    monkeypatch.setattr(helper_module, '_build_client', build)
    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('new question', 'p', 'a')

    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert [message.content for message in session.history] == ['old question', 'old answer', 'new question', 'partial']
    assert FakeApp.last.session_mgr.saved == ['s1']
    assert helper.persisted is True


async def test_cancelled_chat_rewrites_a_turn_the_app_already_grew(helper_env):
    async def behavior(app):
        session = app.session_mgr.sessions_map['s1']
        session.history.extend([UserMessage('hi'), AssistantMessage('whole')])
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert [message.content for message in session.history] == ['hi', 'whole']
    assert FakeApp.last.session_mgr.saved == ['s1']
    assert helper.persisted is True


async def test_a_stopped_tool_iteration_keeps_its_tool_records(helper_env):
    async def behavior(app):
        session = app.session_mgr.sessions_map['s1']
        session.history.extend([UserMessage('hi'), AssistantMessage('', tool_calls=[ToolCall('c1', 'function', 'read_file', '{}')]), ToolMessage('c1', 'print(1)', 'read_file', '{}')])
        app.stream.content.extend(['checking'])
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert [type(message).__name__ for message in session.history] == ['UserMessage', 'AssistantMessage', 'ToolMessage', 'AssistantMessage']
    assert session.history[2].content == 'print(1)'
    assert session.history[3].content == 'checking'
    assert helper.persisted is True


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
    assert client.calls == ['list_agents', 'list_providers', 'get_session', 'list_session_messages', 'list_sops']
    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert [message.content for message in session.history] == ['older']
    assert session.title == ''
    assert FakeApp.last.session_mgr.saved == ['s1']


async def test_the_session_keeps_the_agent_it_was_talked_to_with(helper_env, monkeypatch):
    def build(token):
        client = FakeClient(token)
        client.session_info['agent_id'] = 'agent-1'
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    await helper.chat('hi', 'p', 'a')

    assert FakeApp.last.session_mgr.sessions_map['s1'].agent_id == 'agent-1'


async def test_missing_session_is_refused_without_saving(helper_env, monkeypatch):
    seen: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        client.get_session_error = DataCloudError(404, 'NOT_FOUND', 'missing')
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'a')

    client = seen[0]
    assert client.calls == ['list_agents', 'list_providers', 'get_session']
    assert FakeApp.last.session_mgr.saved == []
    assert helper.persisted is False


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


async def test_update_token_swaps_the_token_in_place(helper_env, monkeypatch):
    built: list[FakeClient] = []

    def build(token):
        client = FakeClient(token)
        built.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 'old-token', 'u1')
    client = helper.client

    await helper.update_token('new-token')

    assert helper.client is client
    assert client.token == 'new-token'
    assert client.closed is False
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
    assert helper.token_generation == 0


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

    assert seen[0].token == 'fresh'
    assert seen[0].calls == ['list_agents', 'list_providers', 'get_session', 'list_session_messages', 'list_sops']


class ExpiredClient(FakeClient):
    def __init__(self, token, failures=1):
        super().__init__(token)
        self.failures = failures

    async def update_session(self, uuid, payload, source=None):
        self.calls.append('update_session')
        if self.failures > 0:
            self.failures -= 1
            raise DataCloudError(401, 'UNAUTHORIZED', 'token expired')
        self.updated = payload
        return {}


async def test_save_retries_once_the_socket_hands_over_a_fresh_token(helper_env, monkeypatch):
    seen: list[ExpiredClient] = []

    def build(token):
        client = ExpiredClient(token)
        seen.append(client)
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    monkeypatch.setattr(helper_module, 'TOKEN_REFRESH_WAIT', 5.0)
    helper = helper_module.ChatHelper('s1', 'stale', 'u1')
    storage = helper.storage()

    async def rotate():
        await asyncio.sleep(0.05)
        await helper.update_token('fresh')

    awaiting = asyncio.create_task(rotate())
    await storage.save(Session(uuid='s1'))
    await awaiting

    client = seen[0]
    assert helper.client is client
    assert client.token == 'fresh'
    assert client.calls == ['update_session', 'update_session']
    assert client.updated is not None


async def test_save_gives_up_when_no_fresh_token_arrives(helper_env, monkeypatch):
    def build(token):
        return ExpiredClient(token, failures=99)

    monkeypatch.setattr(helper_module, '_build_client', build)
    monkeypatch.setattr(helper_module, 'TOKEN_REFRESH_WAIT', 0.1)
    helper = helper_module.ChatHelper('s1', 'stale', 'u1')

    with pytest.raises(DataCloudError) as caught:
        await helper.storage().save(Session(uuid='s1'))

    assert caught.value.status_code == 401


async def test_save_does_not_retry_other_failures(helper_env, monkeypatch):
    calls: list[str] = []

    class BrokenClient(FakeClient):
        async def update_session(self, uuid, payload, source=None):
            calls.append('update_session')
            raise DataCloudError(500, 'INTERNAL', 'cloud down')

    monkeypatch.setattr(helper_module, '_build_client', BrokenClient)
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(DataCloudError) as caught:
        await helper.storage().save(Session(uuid='s1'))

    assert caught.value.status_code == 500
    assert calls == ['update_session']


async def test_agent_not_found_is_not_persisted(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'missing')
    assert helper.persisted is False


async def test_room_and_workspace_are_scoped_to_the_user(helper_env, monkeypatch):
    seen: list[str] = []

    async def capture(app, room):
        seen.append(room)
        return app.stream

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


async def test_revert_refuses_a_missing_session(helper_env, monkeypatch):
    def build(token):
        client = FakeClient(token)
        client.get_session_error = DataCloudError(404, 'NOT_FOUND', 'missing')
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    assert await helper.revert('m2') is None


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


async def test_revert_refuses_the_opening_user_message(helper_env, monkeypatch):
    seen: list[FakeClient] = []
    monkeypatch.setattr(helper_module, '_build_client', _revert_client(seen))
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    assert await helper.revert('m1') is None
    assert seen[0].calls == ['get_session', 'list_session_messages']


async def test_revert_session_closes_the_client(helper_env, monkeypatch):
    seen: list[FakeClient] = []
    monkeypatch.setattr(helper_module, '_build_client', _revert_client(seen))

    assert await helper_module.revert_session('s1', 'm2', 't', 'u1') is True
    assert seen[0].closed is True


async def test_a_sop_command_is_sent_as_the_rewritten_message(helper_env):
    FakeClient.sops = [{'slug': 'trip-itinerary', 'title': 'Trip itinerary', 'content': '# Trip itinerary\n\nPlan it.'}]
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    await helper.chat('/trip-itinerary 幫我安排大阪行程', 'p', 'a')

    assert FakeApp.last.chats == ['#trip-itinerary 幫我安排大阪行程\n\n# Trip itinerary\n\nPlan it.']
    assert FakeApp.last.chat_helper.titles == [('s1', '/trip-itinerary 幫我安排大阪行程')]


async def test_a_message_that_names_no_sop_is_sent_as_typed(helper_env):
    FakeClient.sops = []
    FakeCompactor.summary = 'a summary'
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    await helper.chat('/nope hello', 'p', 'a')

    assert FakeApp.last.chats == ['/nope hello']


async def test_the_compact_command_summarises_the_conversation_without_a_model_turn(helper_env, monkeypatch):
    def build(token):
        client = FakeClient(token)
        client.messages = [_user(0, 'm1', 'first'), _assistant(1, 'a1', 'answer'), _user(2, 'm2', 'second'), _assistant(3, 'a2', 'reply')]
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    await helper.chat('/compact', 'p', 'a')

    app = FakeApp.last
    assert app.chats == []
    assert len(app.compactor.summaries) == 1
    assert [message.content for message in app.session_mgr.sessions_map['s1'].history][-1] == 'a summary'
    assert helper.persisted is True


async def test_the_compact_command_skips_an_empty_conversation(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    await helper.chat('/compact', 'p', 'a')

    assert FakeApp.last.compactor.summaries == []
    assert helper.persisted is True


async def test_a_failed_compact_leaves_the_conversation_alone(helper_env, monkeypatch):
    FakeCompactor.summary = None

    def build(token):
        client = FakeClient(token)
        client.messages = [_user(0, 'm1', 'first'), _assistant(1, 'a1', 'answer'), _user(2, 'm2', 'second')]
        return client

    monkeypatch.setattr(helper_module, '_build_client', build)
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    await helper.chat('/compact', 'p', 'a')

    assert len(FakeApp.last.compactor.summaries) == 1
    assert [message.content for message in FakeApp.last.session_mgr.sessions_map['s1'].history] == ['first', 'answer', 'second']


async def test_images_reach_the_app_and_survive_a_cancelled_turn(helper_env):
    uri = 'data:image/png;base64,AAAA'

    async def behavior(app):
        app.stream.content.extend(['par', 'tial'])
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a', images=[uri])

    assert FakeApp.last.images == [uri]
    session = FakeApp.last.session_mgr.sessions_map['s1']
    assert session.history[0].images == [uri]


async def test_a_session_in_a_project_works_in_that_directory(helper_env):
    FakeClient.projects = {'p1': {'id': 'p1', 'project_name': 'acme', 'project_path': 'acme/api'}}
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    helper.client.session_info['project_id'] = 'p1'

    await helper.chat('hi', 'p', 'a')

    assert helper.workspace is not None
    assert helper.workspace.subdir == 'acme/api'
    assert helper.workspace.workdir == '/workspace/acme/api'


async def test_a_session_without_a_project_stays_at_the_root(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')

    await helper.chat('hi', 'p', 'a')

    assert helper.workspace is not None
    assert helper.workspace.subdir == ''
    assert helper.client.calls == ['list_agents', 'list_providers', 'get_session', 'list_session_messages', 'list_sops']


async def test_a_project_the_cloud_no_longer_has_falls_back_to_the_root(helper_env):
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    helper.client.session_info['project_id'] = 'gone'

    await helper.chat('hi', 'p', 'a')

    assert helper.workspace is not None
    assert helper.workspace.subdir == ''


async def test_the_workspace_follows_a_repointed_session(helper_env):
    FakeClient.projects = {'p1': {'id': 'p1', 'project_name': 'acme', 'project_path': 'acme'}}
    helper = helper_module.ChatHelper('s1', 't', 'u1')
    helper.client.session_info['project_id'] = 'p1'
    await helper.chat('hi', 'p', 'a')

    FakeClient.projects = {'p2': {'id': 'p2', 'project_name': 'other', 'project_path': 'other'}}
    helper.client.session_info['project_id'] = 'p2'
    await helper.chat('again', 'p', 'a')

    assert helper.workspace is not None
    assert helper.workspace.subdir == 'other'
