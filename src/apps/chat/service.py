import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from src.apps.chat.event import error_event
from src.apps.chat.helper import authenticate, authorize_session
from src.apps.chat.room import has_clients, join, leave
from src.apps.chat.runner import ChatCommand, acquire_runner, request_retire
from src.apps.chat.scope import room_key

logger = logging.getLogger(__name__)


async def chat_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    token = websocket.query_params.get('token')
    user = await authenticate(token)
    if user is None:
        logger.warning('websocket auth failed session=%s', session_id)
        await websocket.close(code=4401)
        return
    user_id = str(user.get('id') or '')
    if not user_id:
        logger.warning('websocket auth missing user id session=%s', session_id)
        await websocket.close(code=4401)
        return
    if not await authorize_session(token, session_id, claim=True):
        logger.warning('websocket session forbidden session=%s user=%s', session_id, user_id)
        await websocket.close(code=4403)
        return
    room = room_key(user_id, session_id)
    websocket.state.user = user
    await join(room, websocket)
    runner = await acquire_runner(session_id, token, user_id)
    logger.info('websocket connected room=%s', room)
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
        try:
            await leave(room, websocket)
        except Exception as error:
            logger.warning('leave failed room=%s error=%s', room, error)
        if not has_clients(room):
            request_retire(session_id, user_id)
