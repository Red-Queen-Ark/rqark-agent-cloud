import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from src.apps.chat.event import error_event
from src.apps.chat.helper import authenticate
from src.apps.chat.room import has_clients, join, leave
from src.apps.chat.runner import ChatCommand, acquire_runner, request_retire

logger = logging.getLogger(__name__)


async def chat_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    token = websocket.query_params.get('token')
    user = await authenticate(token)
    if user is None:
        logger.warning('websocket auth failed session=%s', session_id)
        await websocket.close(code=4401)
        return
    websocket.state.user = user
    await join(session_id, websocket)
    runner = acquire_runner(session_id, token)
    logger.info('websocket connected session=%s', session_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                info: dict[str, str] = json.loads(data)
                if info.get('kind') == 'stop':
                    logger.info('websocket stop requested session=%s', session_id)
                    runner.stop()
                else:
                    logger.info('websocket chat submitted session=%s provider=%s agent=%s', session_id, info.get('provider'), info.get('agent'))
                    runner.submit(ChatCommand(info['content'], info['provider'], info['agent']))
            except (ValueError, KeyError, TypeError) as error:
                logger.warning('websocket invalid frame session=%s error=%s', session_id, error)
                await websocket.send_json(error_event(str(error)))
    except WebSocketDisconnect:
        logger.info('websocket disconnected session=%s', session_id)
    finally:
        await leave(session_id, websocket)
        if not has_clients(session_id):
            request_retire(session_id)
