import asyncio
import logging

from rqark_app.app import AgentApp, Session
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.config.manager import _build_agent, _build_provider
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
        if len(session.history) > saved:
            logger.warning('session already saved session=%s streamed=%d', self.session_id, len(assistant))
            self.persisted = True
            return
        logger.warning('saving partial session=%s streamed=%d', self.session_id, len(assistant))
        session.history.append(UserMessage(content))
        if assistant:
            session.history.append(AssistantMessage(assistant))
        await app.session_mgr.save(session.uuid)
        self.persisted = True

    async def _load_session(self, app: AgentApp) -> None:
        try:
            info = await self.client.get_session(self.session_id)
        except DataCloudError as error:
            if error.status_code != 404:
                raise
            return
        messages = await self.client.list_session_messages(self.session_id)
        app.session_mgr.sessions_map[self.session_id] = Session(
            uuid=info['id'],
            created_at=parse_dt(info['created_at']),
            updated_at=parse_dt(info['updated_at']),
            title=info.get('title') or '',
            plan_items=[PlanItem(**item) for item in info.get('plan_items') or []],
            history=[message_from_dict(message['data']) for message in messages],
        )

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

        await self._load_session(app)
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

    async def close(self) -> None:
        await self.client.aclose()
