import json
from collections import defaultdict

from fastapi import WebSocket, WebSocketDisconnect
from rqark_app.cloud.client import DataCloudError
from rqark_core.types.chat import Chunk
from rqark_core.types.event import AgentPayload

from src.apps.chat.event import agent_payload_to_send_json
from src.apps.chat.helper import ChatSocketHelper

rooms: dict[str, set[WebSocket]] = defaultdict(set)

async def chat_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    chat_helper = ChatSocketHelper(websocket, session_id)
    valid_token = await chat_helper.is_valid_token()
    if not valid_token:
        await chat_helper.close(4401)
        return
    rooms[session_id].add(websocket)
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
                agent_payload = AgentPayload('', (),  Chunk('error', str(error)))
                _error = agent_payload_to_send_json(agent_payload)
                await websocket.send_json(_error)
                continue
    except WebSocketDisconnect:
        pass
    finally:
        await chat_helper.close(-1)
        rooms[session_id].discard(websocket)
        if not rooms[session_id]:
            del rooms[session_id]
        print(f'Session {session_id} closed')
