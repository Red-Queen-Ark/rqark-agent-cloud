import asyncio

import pytest
from rqark_app.cloud.client import DataCloudError
from rqark_app.types.session import Session
from rqark_core.types.chat import Chunk, UserMessage
from rqark_core.types.event import AgentPayload

from src.apps.chat import helper as helper_module


class FakeClient:
    def __init__(self, token):
        self.token = token

    async def list_agents(self):
        return [{'name': 'a'}]

    async def list_providers(self):
        return [{'name': 'p'}]

    async def aclose(self):
        return None


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
    def __init__(self, session, fail_save=False):
        self.session = session
        self.loads = 0
        self.saved: list[str] = []
        self.fail_save = fail_save

    async def load(self):
        self.loads += 1

    async def get_or_create(self, session_id):
        return self.session

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
        self.session_mgr = FakeSessionManager(Session(uuid='s1'), FakeApp.fail_save)
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
    monkeypatch.setattr(helper_module, '_build_agent', lambda info: object())
    monkeypatch.setattr(helper_module, 'AgentApp', FakeApp)

    async def noop(app, session_id):
        return None

    monkeypatch.setattr(helper_module, 'connect_agent_event_to_room', noop)
    yield
    FakeApp.behavior = None
    FakeApp.fail_save = False


async def test_successful_chat_is_persisted(helper_env):
    helper = helper_module.ChatHelper('s1', 't')
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
    helper = helper_module.ChatHelper('s1', 't')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    session = FakeApp.last.session_mgr.session
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
    helper = helper_module.ChatHelper('s1', 't')

    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is False


async def test_already_saved_history_is_persisted_without_saving(helper_env):
    async def behavior(app):
        app.session_mgr.session.history.append(UserMessage('already in cloud'))
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't')

    with pytest.raises(asyncio.CancelledError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is True
    assert FakeApp.last.session_mgr.saved == []


async def test_title_failure_after_saved_chat_still_counts_as_persisted(helper_env):
    async def behavior(app):
        app.chat_helper.fail_title = True

    FakeApp.behavior = behavior
    helper = helper_module.ChatHelper('s1', 't')

    with pytest.raises(RuntimeError):
        await helper.chat('hi', 'p', 'a')

    assert helper.persisted is True


async def test_persisted_flag_is_reset_each_turn(helper_env):
    helper = helper_module.ChatHelper('s1', 't')
    await helper.chat('hi', 'p', 'a')
    assert helper.persisted is True

    async def behavior(app):
        raise asyncio.CancelledError()

    FakeApp.behavior = behavior
    FakeApp.fail_save = True

    with pytest.raises(DataCloudError):
        await helper.chat('bye', 'p', 'a')

    assert helper.persisted is False


async def test_agent_not_found_is_not_persisted(helper_env):
    helper = helper_module.ChatHelper('s1', 't')
    with pytest.raises(DataCloudError):
        await helper.chat('hi', 'p', 'missing')
    assert helper.persisted is False
