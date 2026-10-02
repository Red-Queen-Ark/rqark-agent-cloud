from fastapi import WebSocket
from rqark_app.app import AgentApp
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.config.manager import _build_provider, _build_agent
from rqark_app.session.storage import CloudStorage
from rqark_app.types.cloud import CloudConfig
from rqark_app.types.session import Session

from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL

class WebSocketEvent:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.token = websocket.query_params.get('token')
        self.client = RQArkDataCloudClient(CloudConfig(url=DATA_CLOUD_URL, api_key=self.token or ''), timeout=DATA_CLOUD_TIMEOUT)

    async def login(self) -> bool:
        if self.token is None:
            await self.close(code=4401)
            return False
        try:
            self.user = await self.client.me()
            self.websocket.state.user = self.user
        except DataCloudError:
            await self.close(code=4401)
            return False
        return True

    async def aclose(self):
        await self.client.aclose()

    async def close(self, code: int):
        await self.aclose()
        await self.websocket.close(code=code)

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
        return await app.chat(content, agent)
