from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from src.apps.chat.replay import read
from src.apps.chat.service import chat_websocket
from src.middleware.auth import get_current_user

router = APIRouter(tags=['Chat'], prefix='/chat')

@router.websocket('/ws/{session_id}')
async def websocket_endpoint_session(websocket: WebSocket, session_id: str):
    await chat_websocket(websocket, session_id)

@router.get('/{session_id}/replay')
async def replay_session(session_id: str, user: Annotated[dict, Depends(get_current_user)]):
    return await read(session_id)
