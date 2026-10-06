from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from src.apps.chat.helper import authorize_session
from src.apps.chat.replay import read
from src.apps.chat.scope import room_key
from src.apps.chat.service import chat_websocket
from src.middleware.auth import AuthError, get_bearer, get_current_user

router = APIRouter(tags=['Chat'], prefix='/chat')

@router.websocket('/ws/{session_id}')
async def websocket_endpoint_session(websocket: WebSocket, session_id: str):
    await chat_websocket(websocket, session_id)

@router.get('/{session_id}/replay')
async def replay_session(session_id: str, user: Annotated[dict, Depends(get_current_user)], token: Annotated[str, Depends(get_bearer)]):
    user_id = str(user.get('id') or '')
    if not user_id:
        raise AuthError(401, 'UNAUTHORIZED', 'Authenticated user has no id.')
    if not await authorize_session(token, session_id):
        raise AuthError(403, 'FORBIDDEN', 'This session does not belong to you.')
    return await read(room_key(user_id, session_id))
