import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from rqark_app.app import AgentApp
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.config.manager import _build_agent, _build_provider
from rqark_app.session.storage import CloudStorage
from rqark_app.types.cloud import CloudConfig
from rqark_app.types.session import Session
from rqark_core.types.chat import AssistantMessage, Chunk, UserMessage

from src.apps.sandbox import swap_agent
from src.apps.sandbox.workspace import Workspace
from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL, INTERNAL_TOKEN, SANDBOX_IMAGE, TASK_RUN_TIMEOUT

logger = logging.getLogger(__name__)

TASK_SOURCE = 'TASK'

RUN_OK = 'ok'
RUN_ERROR = 'error'
RUN_TIMEOUT = 'timeout'
RUN_CANCELLED = 'cancelled'


class RunnerError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ClaimedRun:
    run_id: str
    user_id: str
    task_id: str
    task_name: str
    prompt: str
    session_id: str
    actor_token: str
    agent_def: dict[str, Any]
    provider_def: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> 'ClaimedRun':
        task = payload.get('task') or {}
        return cls(
            run_id=str(payload['run_id']),
            user_id=str(payload['user_id']),
            task_id=str(task['id']),
            task_name=str(task.get('name') or ''),
            prompt=str(task.get('prompt') or ''),
            session_id=str(payload['session_id']),
            actor_token=str(payload['actor_token']),
            agent_def=payload.get('agent_def') or {},
            provider_def=payload.get('provider_def') or {},
        )


class InternalClient:
    def __init__(self, url: str = DATA_CLOUD_URL, token: str = INTERNAL_TOKEN, timeout: float = DATA_CLOUD_TIMEOUT):
        self.url = url.rstrip('/')
        self.token = token
        self.timeout = timeout

    @property
    def api_url(self) -> str:
        return self.url if self.url.endswith('/api') else f'{self.url}/api'

    def _headers(self) -> dict[str, str]:
        return {'X-Internal-Token': self.token}

    async def claim(self, limit: int) -> list[ClaimedRun]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f'{self.api_url}/internal/tasks/claim', json={'limit': limit}, headers=self._headers())
            response.raise_for_status()
            body = response.json()
        return [ClaimedRun.from_payload(item) for item in body.get('runs') or []]

    async def finalize(self, task_id: str, run_id: str, status: str, error: str = '', session_id: str | None = None) -> None:
        payload: dict[str, Any] = {'status': status, 'error': error}
        if session_id is not None:
            payload['session_id'] = session_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.patch(f'{self.api_url}/internal/tasks/{task_id}/runs/{run_id}', json=payload, headers=self._headers())
            response.raise_for_status()


class LocalStream:
    def __init__(self) -> None:
        self._chunks: list[str] = []

    @property
    def text(self) -> str:
        return ''.join(self._chunks)

    def connect(self, app: AgentApp) -> None:
        @app.on_content()
        async def content(ev: Any) -> None:
            if ev.path:
                return
            chunk = ev.payload
            if isinstance(chunk, Chunk) and chunk.type == 'content':
                self._chunks.append(str(chunk.content))


def _build_app(run: ClaimedRun) -> tuple[Any, AgentApp, CloudStorage, Workspace]:
    try:
        agent = _build_agent(run.agent_def)
    except (KeyError, ValueError) as error:
        raise RunnerError('AGENT_NOT_FOUND') from error
    try:
        provider = _build_provider(run.provider_def)
    except (KeyError, TypeError, ValueError) as error:
        raise RunnerError('PROVIDER_NOT_FOUND') from error

    workspace = Workspace(run.user_id, image=SANDBOX_IMAGE)
    swap_agent(agent, workspace)

    client = RQArkDataCloudClient(CloudConfig(url=DATA_CLOUD_URL, api_key=run.actor_token), timeout=DATA_CLOUD_TIMEOUT)
    storage = CloudStorage(client, source=TASK_SOURCE)
    app = AgentApp([provider], storage)
    app.provider_mgr.switch_to(0)

    return agent, app, storage, workspace


async def _build_and_open(run: ClaimedRun) -> tuple[Any, AgentApp, CloudStorage, Workspace]:
    agent, app, storage, workspace = _build_app(run)
    await workspace.ensure()
    return agent, app, storage, workspace


async def _save_transcript(storage: CloudStorage, run: ClaimedRun, text: str) -> None:
    session = Session(uuid=run.session_id)
    session.title = run.task_name
    session.history = [UserMessage(run.prompt)]
    if text:
        session.history.append(AssistantMessage(text))
    await storage.save(session)


async def _run_to_completion(coro: Any) -> None:
    """Runs a cleanup step to the end even if the caller is cancelled again.

    A shutdown cancels the run task; the cleanup below it then has to reach the
    data cloud, which is an await of its own. A second cancellation arriving while
    it is in flight would leave the run unfinalized, and the dispatcher would only
    learn of it when the stale sweep fired — a run reported as lost that in fact
    completed.
    """
    task = asyncio.create_task(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            logger.error('cleanup aborted before it finished')
        raise


async def _finalize(client: InternalClient, run: ClaimedRun, status: str, error: str) -> None:
    try:
        await client.finalize(run.task_id, run.run_id, status, error, run.session_id)
    except DataCloudError:
        logger.exception('finalize refused run=%s', run.run_id)
    except Exception:
        logger.exception('finalize failed run=%s', run.run_id)


async def execute(client: InternalClient, run: ClaimedRun) -> None:
    status = RUN_OK
    error = ''
    cancelled = False

    stream = LocalStream()
    storage: CloudStorage | None = None
    app: AgentApp | None = None
    agent: Any = None

    try:
        agent, app, storage, _ = await _build_and_open(run)
        stream.connect(app)

        session = await app.session_mgr.get_or_create(run.session_id)
        session.title = run.task_name
        await asyncio.wait_for(app.chat(run.prompt, agent, session_id=run.session_id), TASK_RUN_TIMEOUT)
    except RunnerError as failure:
        status, error = RUN_ERROR, failure.reason
    except TimeoutError:
        logger.warning('run timed out run=%s', run.run_id)
        status, error = RUN_ERROR, RUN_TIMEOUT
    except asyncio.CancelledError:
        logger.warning('run cancelled run=%s', run.run_id)
        status, error = RUN_CANCELLED, ''
        cancelled = True
    except Exception as failure:
        logger.exception('run failed run=%s', run.run_id)
        status, error = RUN_ERROR, str(failure) or RUN_ERROR
    finally:
        async def close_out() -> None:
            if storage is not None and status != RUN_OK:
                try:
                    await _save_transcript(storage, run, stream.text)
                except Exception:
                    logger.exception('cannot save the partial transcript run=%s', run.run_id)
            await _finalize(client, run, status, error)

        await _run_to_completion(close_out())

    if cancelled:
        raise asyncio.CancelledError


async def run_once(limit: int = 1) -> int:
    client = InternalClient()
    runs = await client.claim(limit)
    for run in runs:
        await execute(client, run)
    return len(runs)
