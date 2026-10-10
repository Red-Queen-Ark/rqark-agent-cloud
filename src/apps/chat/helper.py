import asyncio
import logging

from rqark_app.app import AgentApp, Session
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.config.manager import _build_agent, _build_provider
from rqark_app.session.manager import SessionManager
from rqark_app.session.storage import CloudStorage
from rqark_app.types.cloud import CloudConfig
from rqark_app.types.session import message_from_dict, parse_dt
from rqark_core.types.chat import AssistantMessage, Chunk, UserMessage
from rqark_core.types.event import AgentPayload
from rqark_core.types.tool import PlanItem

from src.apps.chat.event import connect_agent_event_to_room
from src.apps.chat.scope import room_key
from src.apps.sandbox import swap_agent
from src.apps.sandbox.workspace import Workspace
from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL, SANDBOX_IMAGE

logger = logging.getLogger(__name__)

SOURCE = 'AGENT_CLOUD'


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
        self.workspace = Workspace(user_id, image=SANDBOX_IMAGE)
        self.client = _build_client(token)
        self.persisted = False

    async def update_token(self, token: str | None) -> None:
        if (token or '') == self.token:
            return
        previous, self.client = self.client, _build_client(token)
        self.token = token or ''
        logger.info('chat token updated session=%s', self.session_id)
        await previous.aclose()

    async def _save_partial(self, app: AgentApp, session: Session, saved: int, content: str, assistant: str) -> None:
        if len(session.history) <= saved:
            session.history.append(UserMessage(content))
            if assistant:
                session.history.append(AssistantMessage(assistant))
        logger.warning('saving partial session=%s loaded=%d streamed=%d', self.session_id, saved, len(assistant))
        await app.session_mgr.save(session.uuid)
        self.persisted = True

    async def _load_session(self, manager: SessionManager) -> bool:
        try:
            info = await self.client.get_session(self.session_id)
        except DataCloudError as error:
            if error.status_code != 404:
                raise
            logger.warning('session not found session=%s', self.session_id)
            return False
        messages = await self.client.list_session_messages(self.session_id)
        manager.sessions_map[self.session_id] = Session(
            uuid=info['id'],
            created_at=parse_dt(info['created_at']),
            updated_at=parse_dt(info['updated_at']),
            title=info.get('title') or '',
            plan_items=[PlanItem(**item) for item in info.get('plan_items') or []],
            history=[message_from_dict(message['data']) for message in messages],
        )
        return True

    async def chat(self, content: str, provider_name: str, agent_name: str) -> Session:
        self.persisted = False
        agents = await self.client.list_agents()
        agent_info = next((info for info in agents if info.get('name') == agent_name), None)
        if agent_info is None:
            logger.error('agent not found session=%s agent=%s', self.session_id, agent_name)
            raise DataCloudError(404, 'AGENT_NOT_FOUND', f'agent not found: {agent_name}')
        providers = [_build_provider(info) for info in await self.client.list_providers()]
        provider = next((p for p in providers if p.name == provider_name), None)
        if provider is None:
            logger.error('provider not found session=%s provider=%s', self.session_id, provider_name)
            raise DataCloudError(404, 'PROVIDER_NOT_FOUND', f'provider not found: {provider_name}')
        await self.workspace.ensure()
        agent = _build_agent(agent_info)
        swap_agent(agent, self.workspace)
        app = AgentApp(providers, CloudStorage(self.client, source=SOURCE, list_source=SOURCE))
        app.provider_mgr.switch_to(app.provider_mgr.providers.index(provider))
        await connect_agent_event_to_room(app, self.room)
        streamed: list[str] = []

        @app.on_content()
        async def collect(ev: AgentPayload[Chunk]) -> None:
            streamed.append(str(ev.payload.content))

        if not await self._load_session(app.session_mgr):
            logger.error('chat refused for a missing session session=%s', self.session_id)
            raise DataCloudError(404, 'SESSION_NOT_FOUND', f'session not found: {self.session_id}')
        session = await app.session_mgr.get_or_create(self.session_id)
        needs_title = bool(content) and not session.title
        saved = len(session.history)
        logger.info('chat request session=%s provider=%s agent=%s context=%d', self.session_id, provider_name, agent_name, saved)
        try:
            await app.chat(content, agent, session_id=self.session_id)
        except asyncio.CancelledError:
            await self._save_partial(app, session, saved, content, ''.join(streamed))
            raise
        self.persisted = True
        if needs_title:
            await app.chat_helper.gen_title(session.uuid, content)
        return session

    async def revert(self, message_id: str) -> Session | None:
        manager = SessionManager(CloudStorage(self.client, source=SOURCE, list_source=SOURCE))
        if not await self._load_session(manager):
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


async def revert_session(session_id: str, message_id: str, token: str, user_id: str) -> bool:
    helper = ChatHelper(session_id, token, user_id)
    try:
        return await helper.revert(message_id) is not None
    finally:
        await helper.close()
