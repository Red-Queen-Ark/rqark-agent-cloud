import asyncio

import pytest
from rqark_app.cloud.client import DataCloudError

from src.apps.tasks import runner as runner_module
from src.apps.tasks.runner import ClaimedRun, InternalClient, RunnerError, execute

RUN_ID = 'run-1'
USER_ID = 'user-1'
TASK_ID = 'task-1'
SESSION_ID = 'session-1'

PAYLOAD = {
    'run_id': RUN_ID,
    'user_id': USER_ID,
    'task': {'id': TASK_ID, 'name': 'Morning brief', 'prompt': 'Summarise my inbox.', 'agent_name': 'assistant', 'provider_name': 'openai'},
    'agent_def': {'id': 'agent-1', 'name': 'assistant', 'description': '', 'prompts': [], 'tools': []},
    'provider_def': {'id': 'provider-1', 'name': 'openai', 'model_name': 'gpt-5', 'api_key': 'sk-test', 'api_base': 'https://example.com', 'priority': 1, 'combine': False},
    'session_id': SESSION_ID,
    'actor_token': 'run-token',
}


class FakeClient:
    def __init__(self):
        self.finalized: list[tuple[str, str, str, str, str | None]] = []
        self.claimed: list[int] = []

    async def claim(self, limit):
        self.claimed.append(limit)
        return []

    async def finalize(self, task_id, run_id, status, error='', session_id=None):
        self.finalized.append((task_id, run_id, status, error, session_id))


class FakeStorage:
    def __init__(self):
        self.saved = []

    async def save(self, session):
        self.saved.append(session)


class FakeWorkspace:
    instances: list['FakeWorkspace'] = []

    def __init__(self, user_id, image=''):
        self.user_id = user_id
        self.image = image
        self.ensured = False
        FakeWorkspace.instances.append(self)

    async def ensure(self):
        self.ensured = True


class FakeSessionManager:
    def __init__(self):
        self.sessions = {}

    async def get_or_create(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            session = runner_module.Session(uuid=session_id)
            self.sessions[session_id] = session
        return session


class FakeApp:
    behavior = None

    def __init__(self, storage):
        self.storage = storage
        self.session_mgr = FakeSessionManager()
        self.handlers = {}

    def on_content(self):
        def register(handler):
            self.handlers['content'] = handler
            return handler

        return register

    async def emit_content(self, text, path=()):
        handler = self.handlers.get('content')
        if handler:
            await handler(type('Ev', (), {'path': path, 'payload': runner_module.Chunk('content', text)})())

    async def chat(self, content, agent, session_id=None, **kwargs):
        await self.emit_content('half an ')
        await self.emit_content('answer')
        if FakeApp.behavior == 'timeout':
            await asyncio.sleep(30)
        if FakeApp.behavior == 'fail':
            raise RuntimeError('provider exploded')
        if FakeApp.behavior == 'hang':
            await asyncio.sleep(30)


class SpyDataCloudClient:
    instances: list['SpyDataCloudClient'] = []

    def __init__(self, config, timeout=30.0, transport=None):
        self.config = config
        self.timeout = timeout
        self.calls: list[str] = []
        SpyDataCloudClient.instances.append(self)

    async def list_agents(self, *args, **kwargs):
        self.calls.append('list_agents')
        raise AssertionError('the runner must not list agents')

    async def list_providers(self, *args, **kwargs):
        self.calls.append('list_providers')
        raise AssertionError('the runner must not list providers')

    async def create_session(self, *args, **kwargs):
        self.calls.append('create_session')
        raise AssertionError('the runner must not create a session')

    async def update_session(self, *args, **kwargs):
        self.calls.append('update_session')


@pytest.fixture
def wired(monkeypatch):
    FakeApp.behavior = None
    FakeWorkspace.instances = []
    SpyDataCloudClient.instances = []
    storages: list[FakeStorage] = []

    async def build_and_open(run):
        storage = FakeStorage()
        storages.append(storage)
        app = FakeApp(storage)
        workspace = FakeWorkspace(run.user_id)
        agent = type('Agent', (), {'name': 'assistant'})()
        await workspace.ensure()
        return agent, app, storage, workspace

    monkeypatch.setattr(runner_module, '_build_and_open', build_and_open)
    monkeypatch.setattr(runner_module, 'Workspace', FakeWorkspace)
    monkeypatch.setattr(runner_module, 'RQArkDataCloudClient', SpyDataCloudClient)

    return storages


async def test_a_successful_run_finalizes_ok(wired):
    client = FakeClient()

    await execute(client, ClaimedRun.from_payload(PAYLOAD))

    assert client.finalized == [(TASK_ID, RUN_ID, 'ok', '', SESSION_ID)]
    assert wired[0].saved == []
    assert FakeWorkspace.instances[0].ensured is True


async def test_a_timeout_is_reported_as_an_error_with_the_partial_text(wired, monkeypatch):
    monkeypatch.setattr(runner_module, 'TASK_RUN_TIMEOUT', 0.05)
    FakeApp.behavior = 'timeout'
    client = FakeClient()

    await execute(client, ClaimedRun.from_payload(PAYLOAD))

    assert client.finalized == [(TASK_ID, RUN_ID, 'error', 'timeout', SESSION_ID)]

    saved = wired[0].saved
    assert len(saved) == 1
    assert saved[0].history[0].content == 'Summarise my inbox.'
    assert saved[0].history[1].content == 'half an answer'
    assert saved[0].title == 'Morning brief'


async def test_a_failed_run_is_reported_with_its_reason(wired):
    FakeApp.behavior = 'fail'
    client = FakeClient()

    await execute(client, ClaimedRun.from_payload(PAYLOAD))

    status, error = client.finalized[0][2], client.finalized[0][3]
    assert status == 'error'
    assert 'provider exploded' in error


async def test_a_run_whose_agent_is_missing_never_starts(wired, monkeypatch):
    async def missing_agent(run):
        raise RunnerError('AGENT_NOT_FOUND')

    monkeypatch.setattr(runner_module, '_build_and_open', missing_agent)
    client = FakeClient()

    await execute(client, ClaimedRun.from_payload(PAYLOAD))

    assert client.finalized == [(TASK_ID, RUN_ID, 'error', 'AGENT_NOT_FOUND', SESSION_ID)]


async def test_a_run_whose_workspace_fails_is_reported_rather_than_raised(wired, monkeypatch):
    async def broken(run):
        raise RuntimeError('docker is down')

    monkeypatch.setattr(runner_module, '_build_and_open', broken)
    client = FakeClient()

    await execute(client, ClaimedRun.from_payload(PAYLOAD))

    assert client.finalized[0][:3] == (TASK_ID, RUN_ID, 'error')
    assert 'docker is down' in client.finalized[0][3]


async def test_a_cancelled_run_is_reported_as_cancelled_not_failed(wired):
    FakeApp.behavior = 'hang'
    client = FakeClient()

    task = asyncio.create_task(execute(client, ClaimedRun.from_payload(PAYLOAD)))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.finalized == [(TASK_ID, RUN_ID, 'cancelled', '', SESSION_ID)]


async def test_a_cancel_during_preparation_still_finalizes(wired, monkeypatch):
    gate = asyncio.Event()

    async def slow_open(run):
        await gate.wait()
        raise RunnerError('never reached')

    monkeypatch.setattr(runner_module, '_build_and_open', slow_open)
    client = FakeClient()

    task = asyncio.create_task(execute(client, ClaimedRun.from_payload(PAYLOAD)))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.finalized == [(TASK_ID, RUN_ID, 'cancelled', '', SESSION_ID)]


async def test_the_runner_never_stops_the_workspace(wired):
    """Two runs of one user share a sandbox, so a finishing run must not stop it."""
    client = FakeClient()

    await execute(client, ClaimedRun.from_payload(PAYLOAD))

    assert not hasattr(FakeWorkspace.instances[0], 'stopped')


async def test_the_runner_builds_its_client_from_the_run_token(monkeypatch):
    monkeypatch.setattr(runner_module, 'Workspace', FakeWorkspace)
    monkeypatch.setattr(runner_module, 'RQArkDataCloudClient', SpyDataCloudClient)
    FakeWorkspace.instances = []
    SpyDataCloudClient.instances = []

    run = ClaimedRun.from_payload(PAYLOAD)
    _, app, storage, _ = runner_module._build_app(run)

    assert SpyDataCloudClient.instances[0].config.api_key == 'run-token'
    assert SpyDataCloudClient.instances[0].calls == []
    assert storage.source == 'TASK'
    assert app.provider_mgr.provider.name == 'openai'


async def test_the_sandbox_is_named_after_the_claiming_user(monkeypatch):
    monkeypatch.setattr(runner_module, 'Workspace', FakeWorkspace)
    monkeypatch.setattr(runner_module, 'RQArkDataCloudClient', SpyDataCloudClient)
    FakeWorkspace.instances = []

    runner_module._build_app(ClaimedRun.from_payload(PAYLOAD))

    assert FakeWorkspace.instances[0].user_id == USER_ID


async def test_a_finalize_that_is_refused_does_not_escape(wired):
    class RefusingClient(FakeClient):
        async def finalize(self, *args, **kwargs):
            raise DataCloudError(409, 'CONFLICT', 'This run is no longer running.')

    await execute(RefusingClient(), ClaimedRun.from_payload(PAYLOAD))


async def test_a_finalize_that_cannot_reach_the_cloud_does_not_escape(wired):
    class BrokenClient(FakeClient):
        async def finalize(self, *args, **kwargs):
            raise DataCloudError(0, 'REQUEST_FAILED', 'cannot reach data cloud')

    await execute(BrokenClient(), ClaimedRun.from_payload(PAYLOAD))


async def test_the_claim_payload_is_read_into_its_parts():
    run = ClaimedRun.from_payload(PAYLOAD)

    assert run.run_id == RUN_ID
    assert run.user_id == USER_ID
    assert run.task_id == TASK_ID
    assert run.task_name == 'Morning brief'
    assert run.prompt == 'Summarise my inbox.'
    assert run.session_id == SESSION_ID
    assert run.actor_token == 'run-token'
    assert run.agent_def['name'] == 'assistant'
    assert run.provider_def['model_name'] == 'gpt-5'


def test_the_internal_client_sends_the_header_and_never_a_bearer_token():
    client = InternalClient(url='http://data-cloud:7302', token='shared-secret')

    assert client.api_url == 'http://data-cloud:7302/api'
    assert client._headers() == {'X-Internal-Token': 'shared-secret'}


async def test_run_once_claims_then_executes(monkeypatch):
    executed: list[str] = []

    class OneRunClient(FakeClient):
        async def claim(self, limit):
            self.claimed.append(limit)
            return [ClaimedRun.from_payload(PAYLOAD)]

    client = OneRunClient()

    async def fake_execute(inner, run):
        executed.append(run.run_id)

    monkeypatch.setattr(runner_module, 'InternalClient', lambda: client)
    monkeypatch.setattr(runner_module, 'execute', fake_execute)

    assert await runner_module.run_once(2) == 1
    assert client.claimed == [2]
    assert executed == [RUN_ID]
