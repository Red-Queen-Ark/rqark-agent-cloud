def room_key(user_id: str, session_id: str) -> str:
    return f'{user_id}:{session_id}'
