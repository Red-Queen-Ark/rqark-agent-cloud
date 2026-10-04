import json

from fastapi import WebSocket, WebSocketDisconnect
from rqark_app.cloud.client import DataCloudError
from rqark_core.types.chat import Chunk
from rqark_core.types.event import AgentPayload

from src.apps.chat.event import agent_payload_to_send_json
from src.apps.chat.helper import ChatSocketHelper
from src.apps.chat.room import join, leave


async def chat_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    chat_helper = ChatSocketHelper(websocket, session_id)
    valid_token = await chat_helper.is_valid_token()
    if not valid_token:
        await chat_helper.close(4401)
        return
    await join(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                info: dict[str, str] = json.loads(data)
                content: str = info['content']
                provider_name: str = info['provider']
                agent_name: str = info['agent']
                await chat_helper.chat(content, provider_name, agent_name)
            except (ValueError, KeyError, TypeError, DataCloudError) as error:
                agent_payload = AgentPayload('', (), Chunk('error', str(error)))
                await websocket.send_json(agent_payload_to_send_json(agent_payload))
                continue
    except WebSocketDisconnect:
        pass
    finally:
        await chat_helper.close(-1)
        await leave(session_id, websocket)
