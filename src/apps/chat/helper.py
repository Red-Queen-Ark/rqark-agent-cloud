import asyncio

from rqark_app.app import AgentApp, Session
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.config.manager import _build_agent, _build_provider
from rqark_app.session.storage import CloudStorage
from rqark_app.types.cloud import CloudConfig
from rqark_core.types.chat import AssistantMessage, Chunk, UserMessage
from rqark_core.types.event import AgentPayload

from src.apps.chat.event import connect_agent_event_to_room
from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL


def _build_client(token: str | None) -> RQArkDataCloudClient:
    return RQArkDataCloudClient(CloudConfig(url=DATA_CLOUD_URL, api_key=token or ''), timeout=DATA_CLOUD_TIMEOUT)


async def authenticate(token: str | None) -> dict | None:
    if token is None:
        return None
    client = _build_client(token)
    try:
        return await client.me()
    except DataCloudError:
        return None
    finally:
        await client.aclose()


class ChatHelper:
    def __init__(self, session_id: str, token: str | None):
        self.session_id = session_id
        self.client = _build_client(token)

    async def _stored_title(self) -> str:
        try:
            info = await self.client.get_session(self.session_id)
        except DataCloudError as error:
            if error.status_code == 404:
                return ''
            raise
        return info.get('title') or ''

    async def _save_partial(self, app: AgentApp, session: Session, saved: int, content: str, assistant: str) -> None:
        if len(session.history) > saved:
            return
        session.history.append(UserMessage(content))
        if assistant:
            session.history.append(AssistantMessage(assistant))
        await app.session_mgr.save(session.uuid)

    async def chat(self, content: str, provider_name: str, agent_name: str) -> Session:
        agent_info = next((info for info in await self.client.list_agents() if info.get('name') == agent_name), None)
        if agent_info is None:
            raise DataCloudError(404, 'AGENT_NOT_FOUND', f'agent not found: {agent_name}')
        providers = [_build_provider(info) for info in await self.client.list_providers()]
        provider = next((p for p in providers if p.name == provider_name), None)
        if provider is None:
            raise DataCloudError(404, 'PROVIDER_NOT_FOUND', f'provider not found: {provider_name}')
        agent = _build_agent(agent_info)
        app = AgentApp(providers, CloudStorage(self.client))
        app.provider_mgr.switch_to(app.provider_mgr.providers.index(provider))
        await connect_agent_event_to_room(app, self.session_id)
        streamed: list[str] = []

        @app.on_content()
        async def collect(ev: AgentPayload[Chunk]) -> None:
            streamed.append(str(ev.payload.content))

        title = await self._stored_title()
        session = await app.session_mgr.create(self.session_id)
        session.title = title
        needs_title = bool(content) and not title
        saved = len(session.history)
        try:
            await app.chat(content, agent, session_id=self.session_id)
        except asyncio.CancelledError:
            await self._save_partial(app, session, saved, content, ''.join(streamed))
            raise
        if needs_title:
            await app.chat_helper.gen_title(session.uuid, content)
        return session

    async def close(self) -> None:
        await self.client.aclose()
