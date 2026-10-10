from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from pydantic import BaseModel

from src.apps.chat.helper import authorize_session, revert_session
from src.apps.chat.replay import read
from src.apps.chat.runner import is_busy
from src.apps.chat.scope import room_key
from src.apps.chat.service import chat_websocket
from src.middleware.auth import AuthError, get_bearer, get_current_user

router = APIRouter(tags=['Chat'], prefix='/chat')


class RevertRequest(BaseModel):
    message_id: str


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

@router.post('/{session_id}/revert')
async def revert_message(session_id: str, body: RevertRequest, user: Annotated[dict, Depends(get_current_user)], token: Annotated[str, Depends(get_bearer)]):
    user_id = str(user.get('id') or '')
    if not user_id:
        raise AuthError(401, 'UNAUTHORIZED', 'Authenticated user has no id.')
    if not await authorize_session(token, session_id):
        raise AuthError(403, 'FORBIDDEN', 'This session does not belong to you.')
    if is_busy(session_id, user_id):
        raise AuthError(409, 'CONFLICT', 'This session is still answering.')
    if not await revert_session(session_id, body.message_id, token, user_id):
        raise AuthError(400, 'BAD_REQUEST', 'This message cannot be reverted.')
    return {'session_id': session_id, 'message_id': body.message_id}
