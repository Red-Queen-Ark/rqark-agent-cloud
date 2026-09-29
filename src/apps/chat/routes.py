from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=['Chat'], prefix='/chat')

@router.websocket("/ws/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await websocket.send_text(f"Message text was: {data}")

rooms: dict[str, set[WebSocket]] = defaultdict(set)

@router.websocket("/ws/{session_id}")
async def websocket_endpoint_session(websocket: WebSocket, session_id: str):
    await websocket.accept()
    rooms[session_id].add(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            dead = []
            for conn in rooms[session_id]:
                try:
                    await conn.send_text(f"[{session_id}] {data}")
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
        print(f"Session {session_id} closed")
