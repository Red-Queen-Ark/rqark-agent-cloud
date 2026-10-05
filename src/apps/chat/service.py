import json

from fastapi import WebSocket, WebSocketDisconnect

from src.apps.chat.event import error_event
from src.apps.chat.helper import authenticate
from src.apps.chat.room import has_clients, join, leave
from src.apps.chat.runner import ChatCommand, acquire_runner, request_retire


async def chat_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    token = websocket.query_params.get('token')
    user = await authenticate(token)
    if user is None:
        await websocket.close(code=4401)
        return
    websocket.state.user = user
    await join(session_id, websocket)
    runner = acquire_runner(session_id, token)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                info: dict[str, str] = json.loads(data)
                if info.get('kind') == 'stop':
                    runner.stop()
                else:
                    runner.submit(ChatCommand(info['content'], info['provider'], info['agent']))
            except (ValueError, KeyError, TypeError) as error:
                await websocket.send_json(error_event(str(error)))
    except WebSocketDisconnect:
        pass
    finally:
        await leave(session_id, websocket)
        if not has_clients(session_id):
            request_retire(session_id)
