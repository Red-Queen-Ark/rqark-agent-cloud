import json
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from rqark_app.cloud.client import DataCloudError

from src.apps.chat.event import WebSocketEvent

router = APIRouter(tags=['Chat'], prefix='/chat')

rooms: dict[str, set[WebSocket]] = defaultdict(set)

@router.websocket('/ws/{session_id}')
async def websocket_endpoint_session(websocket: WebSocket, session_id: str):
    await websocket.accept()
    event = WebSocketEvent(websocket)
    s = await event.login()
    if not s:
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
            except (ValueError, KeyError, TypeError) as error:
                await websocket.send_text(f'[error] INVALID_MESSAGE: {error}')
                continue
            try:
                await event.chat(content, provider_name, agent_name)
            except DataCloudError as error:
                await websocket.send_text(f'[error] {error.code}: {error.message}')
                continue
            dead = []
            for conn in rooms[session_id]:
                try:
                    await conn.send_text(f'[{session_id}] {content}')
                except Exception:
                    dead.append(conn)
            for conn in dead:
                rooms[session_id].discard(conn)
    except WebSocketDisconnect:
        pass
    finally:
        await event.aclose()
        rooms[session_id].discard(websocket)
        if not rooms[session_id]:
            del rooms[session_id]
        print(f'Session {session_id} closed')
