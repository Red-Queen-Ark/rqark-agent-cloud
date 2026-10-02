from fastapi import APIRouter, WebSocket
from src.apps.chat.service import chat_websocket

router = APIRouter(tags=['Chat'], prefix='/chat')

@router.websocket('/ws/{session_id}')
async def websocket_endpoint_session(websocket: WebSocket, session_id: str):
    await chat_websocket(websocket, session_id)
