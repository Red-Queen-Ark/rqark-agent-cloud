from fastapi import WebSocket
from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL
from rqark_app.types.cloud import CloudConfig
from rqark_app.config.manager import _build_provider
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.app import AgentApp
from rqark_app.session.storage import CloudStorage

class WebSocketEvent:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.token = websocket.query_params.get('token')
        self.client: RQArkDataCloudClient

    async def login(self) -> bool:
        if self.token is None:
            await self.close(code=4401)
            return False
        try:
            config = CloudConfig(url=DATA_CLOUD_URL, api_key=self.token)
            async with RQArkDataCloudClient(config, timeout=DATA_CLOUD_TIMEOUT) as client:
                self.user = await client.me()
                self.websocket.state.user = self.user
        except DataCloudError:
            await self.close(code=4401)
            return False
        return True
    
    async def close(self, code: int):
        await self.websocket.close(code=code)
        
    async def chat(self, content: str, provider_name: str, agent_name: str):
        providers = [_build_provider(provider) for provider in await self.client.list_providers()]
        storage = CloudStorage(self.client)
        app = AgentApp(providers, storage)
        app.chat(content)