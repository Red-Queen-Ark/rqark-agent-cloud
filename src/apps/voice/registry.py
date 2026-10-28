from collections import defaultdict
from typing import Any

from src.config import VOICE_MAX_CONNECTIONS, VOICE_MAX_PER_USER


class Registry:
    """Counts live voice sockets so one user cannot pin every model slot."""

    def __init__(self, max_connections: int = VOICE_MAX_CONNECTIONS, max_per_user: int = VOICE_MAX_PER_USER):
        self.max_connections = max(1, max_connections)
        self.max_per_user = max(1, max_per_user)
        self._sockets: dict[str, set[Any]] = defaultdict(set)

    @property
    def total(self) -> int:
        return sum(len(sockets) for sockets in self._sockets.values())

    def reason_to_refuse(self, user_id: str) -> str | None:
        if self.total >= self.max_connections:
            return 'the voice service is at its connection limit'
        if len(self._sockets.get(user_id, ())) >= self.max_per_user:
            return 'this account already has a voice connection'
        return None

    def add(self, user_id: str, websocket: Any) -> None:
        self._sockets[user_id].add(websocket)

    def remove(self, user_id: str, websocket: Any) -> None:
        sockets = self._sockets.get(user_id)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self._sockets.pop(user_id, None)

    def state(self) -> dict[str, int]:
        return {'connections': self.total, 'users': len(self._sockets), 'max_connections': self.max_connections, 'max_per_user': self.max_per_user}


registry = Registry()
