from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.types.cloud import CloudConfig

from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL

router = APIRouter(tags=['Chat'], prefix='/chat')

rooms: dict[str, set[WebSocket]] = defaultdict(set)

@router.websocket('/ws/{session_id}')
async def websocket_endpoint_session(websocket: WebSocket, session_id: str):
    await websocket.accept()
    token = websocket.query_params.get('token')
    if token is None:
        await websocket.close(code=4401)
        return
    config = CloudConfig(url=DATA_CLOUD_URL, api_key=token)
    try:
        async with RQArkDataCloudClient(config, timeout=DATA_CLOUD_TIMEOUT) as client:
            user = await client.me()
    except DataCloudError:
        await websocket.close(code=4401)
        return
    websocket.state.user = user
    rooms[session_id].add(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            dead = []
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
