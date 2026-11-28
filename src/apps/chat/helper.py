import asyncio
import logging
from pathlib import Path
from time import monotonic

from rqark_app.app import AgentApp, Session
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.compact import compact_messages
from rqark_app.config.manager import _build_agent, _build_provider
from rqark_app.session.manager import SessionManager
from rqark_app.session.storage import CloudStorage
from rqark_app.sop.expand import expand
from rqark_app.sop.manager import SopManager
from rqark_app.tools.context import AGENT_KEY, CLIENT_KEY, COMPACT_KEY, SESSION_KEY, SOURCE_KEY
from rqark_app.types.cloud import CloudConfig
from rqark_app.types.session import message_from_dict, parse_dt
from rqark_core.types.chat import Message, UserMessage
from rqark_core.types.tool import PlanItem
from rqark_sandbox import swap_agent
from rqark_sandbox.project import load_project, project_subdir
from rqark_sandbox.workspace import Workspace

from src.apps.chat.event import RoomStream, connect_agent_event_to_room, token_required_event
from src.apps.chat.recover import turn_from_frames
from src.apps.chat.room import publish
from src.apps.chat.scope import room_key
from src.config import CHAT_IDLE_CHECK, CHAT_IDLE_TIMEOUT, CHAT_RETRY_LIMIT, DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL, SANDBOX_ENABLED, SANDBOX_IMAGE

logger = logging.getLogger(__name__)

SOURCE = 'AGENT_CLOUD'
COMPACT_COMMAND = '/compact'
TOKEN_REFRESH_WAIT = 90.0
TOKEN_REFRESH_POLL = 0.05
TOKEN_REQUEST_INTERVAL = 10.0


async def _watch_progress(stream: RoomStream, idle: asyncio.Timeout) -> None:
    """Extend the deadline only while the turn is really moving; a silent retry loop still trips it.

    Two bounds end a turn. The idle timeout catches a turn that has gone quiet for
    long enough that nothing is coming. The retry limit catches the other shape of
    a dead turn: a provider that keeps failing, which is not silence (every
    attempt reports an error) but is not progress either, and would otherwise run
    for the whole idle timeout before anyone said so.
    """
    loop = asyncio.get_running_loop()
    seen = stream.last_activity
    while True:
        await asyncio.sleep(CHAT_IDLE_CHECK)
        if stream.failures >= CHAT_RETRY_LIMIT:
            stream.stall_reason = f'the provider failed {stream.failures} times without producing anything'
            idle.reschedule(loop.time())
            return
        if stream.last_activity != seen:
            seen = stream.last_activity
            idle.reschedule(loop.time() + CHAT_IDLE_TIMEOUT)


def _build_client(token: str | None) -> RQArkDataCloudClient:
    return RQArkDataCloudClient(CloudConfig(url=DATA_CLOUD_URL, api_key=token or ''), timeout=DATA_CLOUD_TIMEOUT)


async def _owns_session(client: RQArkDataCloudClient, session_id: str) -> bool:
    try:
        await client.get_session(session_id)
        return True
    except DataCloudError as error:
        if error.status_code != 404:
            logger.warning('session lookup failed session=%s status=%s', session_id, error.status_code)
        return False


async def authorize_session(token: str | None, session_id: str, claim: bool = False) -> bool:
    if token is None:
        return False
    client = _build_client(token)
    try:
        if await _owns_session(client, session_id):
            return True
        if not claim:
            return False
        try:
            await client.create_session({'uuid': session_id}, source=SOURCE)
            return True
        except DataCloudError as error:
            if error.status_code != 409:
                logger.warning('session claim failed session=%s status=%s', session_id, error.status_code)
                return False
        return await _owns_session(client, session_id)
    finally:
        await client.aclose()


async def authenticate(token: str | None) -> dict | None:
    if token is None:
        return None
    client = _build_client(token)
    try:
        return await client.me()
    except DataCloudError as error:
        logger.warning('authenticate failed error=%s', error)
        return None
    finally:
        await client.aclose()


class ChatHelper:
    def __init__(self, session_id: str, token: str | None, user_id: str):
        self.session_id = session_id
        self.token = token or ''
        self.user_id = user_id
        self.room = room_key(user_id, session_id)
        self.workspace: Workspace | None = Workspace(user_id, image=SANDBOX_IMAGE) if SANDBOX_ENABLED else None
        self._token_generation = 0
        self.client = _build_client(token)
        self.client.on_unauthorized = self._wait_for_fresh_token
        self.persisted = False

    @property
    def token_generation(self) -> int:
        return self._token_generation

    async def update_token(self, token: str | None) -> None:
        fresh = token or ''
        if fresh == self.token:
            return
        self.token = fresh
        self.client.config.api_key = fresh
        self._token_generation += 1
        logger.info('chat token updated session=%s', self.session_id)

    async def wait_for_token(self, generation: int, timeout: float, ask: bool = False) -> bool:
        deadline = monotonic() + timeout
        next_ask = monotonic()
        while self._token_generation == generation:
            if monotonic() >= deadline:
                return False
            if ask and monotonic() >= next_ask:
                await self._request_token()
                next_ask = monotonic() + TOKEN_REQUEST_INTERVAL
            await asyncio.sleep(TOKEN_REFRESH_POLL)
        return True

    async def _request_token(self) -> None:
        logger.warning('asking for a fresh token session=%s', self.session_id)
        try:
            await publish(self.room, token_required_event())
        except Exception as error:
            logger.warning('token request not delivered session=%s error=%s', self.session_id, error)

    async def _wait_for_fresh_token(self) -> None:
        generation = self._token_generation
        if not await self.wait_for_token(generation, TOKEN_REFRESH_WAIT, ask=True):
            raise DataCloudError(401, 'UNAUTHORIZED', 'the data cloud token expired and no fresh one arrived')

    def storage(self) -> CloudStorage:
        return RefreshingStorage(self, source=SOURCE, list_source=SOURCE)

    def extras(self, agent_id: str | None) -> dict[str, object] | None:
        if not agent_id:
            return None
        return {CLIENT_KEY: self.client, AGENT_KEY: agent_id, SOURCE_KEY: SOURCE}

    async def _save_partial(self, app: AgentApp, session: Session, saved: int, content: str, turn: list[Message], images: list[str] | None = None) -> None:
        """Store a stopped turn as far as it got.

        `turn` is the turn's own history, handed back by the core when the cancel
        landed: the user message, every tool iteration it finished and the tool
        calls it never got answers for. Keeping only the streamed text dropped the
        entire tool record of a turn stopped inside a tool loop.
        """
        if turn:
            session.update_history_to_last_chunk(turn)
        elif len(session.history) <= saved:
            session.history.append(UserMessage(content, images=list(images) if images else []))
        logger.warning('saving partial session=%s loaded=%d turn=%d', self.session_id, saved, len(turn))
        # Shielded because this runs from the cancellation path: the write is
        # the only copy of a turn the user already watched, so it has to be
        # allowed to finish and report whether it did.
        await asyncio.shield(app.session_mgr.save(session.uuid))
        self.persisted = True

    async def _adopt_replay(self, app: AgentApp, session: Session, frames: list[dict]) -> None:
        """Store a turn that no shutdown path got to write.

        A worker that was killed rather than stopped leaves the answer in the
        replay log and nowhere else: the next turn would be assembled from a
        history that stops before it. The frames are folded back in first, so
        the conversation continues from what the user actually saw.
        """
        turn = turn_from_frames(frames)
        if not turn:
            return
        known = {message.id for message in session.history}
        if turn[0].id in known:
            if not session.history or session.history[-1].id != turn[0].id:
                return
            session.update_history_to_last_chunk(turn)
        else:
            session.history.extend(turn)
        logger.warning('adopted a replayed turn session=%s frames=%d messages=%d', self.session_id, len(frames), len(turn))
        try:
            await app.session_mgr.save(session.uuid)
        except Exception as error:
            logger.warning('adopted turn not stored session=%s error=%s', self.session_id, error)

    async def _load_session(self, manager: SessionManager, info: dict | None) -> bool:
        if info is None:
            logger.warning('session not found session=%s', self.session_id)
            return False
        messages = await self.client.list_session_messages(self.session_id)
        manager.sessions_map[self.session_id] = Session(
            uuid=info['id'],
            created_at=parse_dt(info['created_at']),
            updated_at=parse_dt(info['updated_at']),
            title=info.get('title') or '',
            agent_id=info.get('agent_id') or '',
            plan_items=[PlanItem(**item) for item in info.get('plan_items') or []],
            history=[message_from_dict(message['data']) for message in messages],
        )
        return True

    async def _session_info(self) -> dict | None:
        """The stored session metadata, or None when the cloud does not have it."""
        try:
            return await self.client.get_session(self.session_id)
        except DataCloudError as error:
            if error.status_code != 404:
                raise
            return None

    async def _apply_project(self, info: dict | None) -> None:
        """Points the workspace at the directory the session's project names.

        Done before the agent is built so that the prompts, which read the
        workspace, describe the directory the agent actually works in.
        """
        if self.workspace is None:
            return
        project = await load_project(self.client, (info or {}).get('project_id'), session_id=self.session_id)
        self.workspace.subdir = project_subdir(project)

    async def chat(self, content: str, provider_name: str, agent_name: str, images: list[str] | None = None, replay: list[dict] | None = None) -> Session:
        self.persisted = False
        images = list(images) if images else []
        agents = await self.client.list_agents()
        agent_info = next((agent for agent in agents if agent.get('name') == agent_name), None)
        if agent_info is None:
            logger.error('agent not found session=%s agent=%s', self.session_id, agent_name)
            raise DataCloudError(404, 'AGENT_NOT_FOUND', f'agent not found: {agent_name}')
        providers = [_build_provider(provider_info) for provider_info in await self.client.list_providers()]
        provider = next((p for p in providers if p.name == provider_name), None)
        if provider is None:
            logger.error('provider not found session=%s provider=%s', self.session_id, provider_name)
            raise DataCloudError(404, 'PROVIDER_NOT_FOUND', f'provider not found: {provider_name}')
        info = await self._session_info()
        await self._apply_project(info)
        agent = _build_agent(agent_info)
        if self.workspace is not None:
            await self.workspace.ensure()
            swap_agent(agent, self.workspace)
        app = AgentApp(providers, self.storage())
        app.provider_mgr.switch_to(app.provider_mgr.providers.index(provider))
        stream = await connect_agent_event_to_room(app, self.room)

        if not await self._load_session(app.session_mgr, info):
            logger.error('chat refused for a missing session session=%s', self.session_id)
            raise DataCloudError(404, 'SESSION_NOT_FOUND', f'session not found: {self.session_id}')
        session = await app.session_mgr.get_or_create(self.session_id)
        await self._adopt_replay(app, session, replay or [])
        if content.strip() == COMPACT_COMMAND:
            await self._compact(app, session, agent_info.get('id'))
            self.persisted = True
            return session
        needs_title = bool(content) and not session.title
        saved = len(session.history)
        # Filled by the core if this turn is cancelled mid-flight: the stop and the
        # idle timeout both arrive as a cancellation, so both paths read it.
        partial: list[Message] = []
        expanded = await self._expand_sop(content)
        logger.info('chat request session=%s provider=%s agent=%s context=%d images=%d', self.session_id, provider_name, agent_name, saved, len(images))
        extras = self.extras(agent_info.get('id'))
        try:
            async with asyncio.timeout(CHAT_IDLE_TIMEOUT) as idle:
                watchdog = asyncio.create_task(_watch_progress(stream, idle))
                try:
                    await app.chat(expanded, agent, session_id=self.session_id, images=images, extras=extras, partial=partial)
                finally:
                    watchdog.cancel()
                    await asyncio.gather(watchdog, return_exceptions=True)
        except TimeoutError as error:
            reason = stream.stall_reason or 'no progress'
            logger.error('chat stalled session=%s reason=%s idle=%.0fs failures=%d', self.session_id, reason, CHAT_IDLE_TIMEOUT, stream.failures)
            await self._save_partial(app, session, saved, expanded, partial, images)
            raise RuntimeError(f'chat stalled: {reason}') from error
        except asyncio.CancelledError:
            await self._save_partial(app, session, saved, expanded, partial, images)
            raise
        self.persisted = True
        if needs_title:
            await app.chat_helper.gen_title(session.uuid, content)
        _record_turn(app, session, extras)
        return session

    async def _expand_sop(self, content: str) -> str:
        """Rewrite a '/slug args' turn with the SOP body; the cloud is the only source here."""
        sops = await SopManager(Path.home() / '.rqark', self.client).list()
        expanded = expand(content, sops)
        if expanded is None:
            return content
        logger.info('sop expanded session=%s slug=%s', self.session_id, expanded[1])
        return expanded[0]

    async def _compact(self, app: AgentApp, session: Session, agent_id: str | None) -> None:
        """Replace the conversation behind the next turn with a summary of it."""
        messages = session.get_last_chunk_for_chat()
        if not compact_messages(messages):
            logger.info('compact skipped on a conversation too short session=%s', self.session_id)
            return
        extras = {**(self.extras(agent_id) or {}), SESSION_KEY: session.uuid, COMPACT_KEY: True}
        summary = await app.compactor.get_summary(messages, self.client, extras)
        if summary is None:
            logger.warning('compact failed session=%s', self.session_id)
            return
        session.history.append(summary)
        await app.session_mgr.save(session.uuid)
        logger.info('compact done session=%s', self.session_id)

    async def revert(self, message_id: str) -> Session | None:
        manager = SessionManager(CloudStorage(self.client, source=SOURCE, list_source=SOURCE))
        if not await self._load_session(manager, await self._session_info()):
            logger.warning('refusing to revert a missing session session=%s', self.session_id)
            return None
        session = manager.sessions_map.get(self.session_id)
        if session is None:
            return None
        first = next((message for message in session.history if isinstance(message, UserMessage)), None)
        if first is None or first.id == message_id:
            logger.warning('refusing to revert the opening user message session=%s', self.session_id)
            return None
        reverted = await manager.revert(self.session_id, message_id)
        if reverted is not None:
            logger.info('session reverted session=%s message=%s remaining=%d', self.session_id, message_id, len(reverted.history))
        return reverted

    async def close(self) -> None:
        await self.client.aclose()


_background_writes: set[asyncio.Task] = set()


async def drain_writes(timeout: float) -> None:
    """Wait for the turn records still in flight, up to timeout.

    A task nobody awaits dies with the loop, which drops the memory of the last
    turn of every session that was being answered when the deploy started.
    """
    pending = [task for task in _background_writes if not task.done()]
    if not pending:
        return
    logger.warning('waiting for %d pending turn records', len(pending))
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(*pending, return_exceptions=True)
    except TimeoutError:
        logger.error('gave up waiting for turn records after %.1fs', timeout)


def _record_turn(app: AgentApp, session: Session, extras: dict[str, object] | None) -> None:
    """Record the finished turn without making the answer wait for it.

    The write is its own task: it costs up to a model call and a few cloud
    requests, and the answer has already been streamed. The app logs a failure;
    nothing here can reach the socket.
    """
    try:
        task = asyncio.create_task(app.write_turn_memory(session_id=session.uuid, source=SOURCE, session=session, extras=extras))
    except RuntimeError:
        logger.warning('no running loop, the turn was not recorded session=%s', session.uuid)
        return
    _background_writes.add(task)
    task.add_done_callback(_background_writes.discard)


class RefreshingStorage(CloudStorage):
    def __init__(self, helper: ChatHelper, source: str = '', list_source: str | None = None):
        super().__init__(helper.client, source=source, list_source=list_source)
        self._helper = helper

    async def save(self, session: Session) -> None:
        generation = self._helper.token_generation
        try:
            await super().save(session)
        except DataCloudError as error:
            if error.status_code != 401:
                raise
            logger.warning('session save refused, waiting for a fresh token session=%s', session.uuid)
            if not await self._helper.wait_for_token(generation, TOKEN_REFRESH_WAIT, ask=True):
                raise
            await super().save(session)


async def revert_session(session_id: str, message_id: str, token: str, user_id: str) -> bool:
    helper = ChatHelper(session_id, token, user_id)
    try:
        return await helper.revert(message_id) is not None
    finally:
        await helper.close()
