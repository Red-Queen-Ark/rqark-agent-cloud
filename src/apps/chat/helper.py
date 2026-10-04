from fastapi import WebSocket
from rqark_app.app import AgentApp, Session
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.config.manager import _build_agent, _build_provider
from rqark_app.session.storage import CloudStorage
from rqark_app.types.cloud import CloudConfig

from src.apps.chat.event import connect_agent_event_to_room
from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL


class ChatSocketHelper:
    def __init__(self, websocket: WebSocket, session_id: str):
        self.websocket = websocket
        self.session_id = session_id
        self.token = websocket.query_params.get('token')
        self.client = RQArkDataCloudClient(CloudConfig(url=DATA_CLOUD_URL, api_key=self.token or ''), timeout=DATA_CLOUD_TIMEOUT)

    async def is_valid_token(self):
        if self.token is None:
            return False
        try:
            self.user = await self.client.me()
            self.websocket.state.user = self.user
        except DataCloudError:
            return False
        return True

    async def _stored_title(self) -> str:
        try:
            info = await self.client.get_session(self.session_id)
        except DataCloudError as error:
            if error.status_code == 404:
                return ''
            raise
        return info.get('title') or ''

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
        title = await self._stored_title()
        session = await app.session_mgr.create(self.session_id)
        session.title = title
        needs_title = bool(content) and not title
        session = await app.chat(content, agent, session_id=self.session_id)
        if needs_title:
            await app.chat_helper.gen_title(session.uuid, content)
        return session

    async def close(self, code: int):
        try:
            await self.client.aclose()
            await self.websocket.close(code=code)
        except Exception:
            pass
