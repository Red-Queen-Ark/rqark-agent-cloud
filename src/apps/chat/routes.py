from collections import defaultdict
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
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
            dead = []
            await event.chat(data)
            for conn in rooms[session_id]:
                try:
                    await conn.send_text(f'[{session_id}] {data}')
                except Exception:
                    dead.append(conn)
            for conn in dead:
                rooms[session_id].discard(conn)
    except WebSocketDisconnect:
        pass
    finally:
        rooms[session_id].discard(websocket)
        if not rooms[session_id]:
            del rooms[session_id]
        print(f'Session {session_id} closed')
