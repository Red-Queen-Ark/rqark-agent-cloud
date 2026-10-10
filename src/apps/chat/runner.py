import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from src.apps.chat.event import error_event
from src.apps.chat.helper import ChatHelper
from src.apps.chat.lock import ChatLock
from src.apps.chat.replay import clear
from src.apps.chat.room import broadcast_stop, has_clients, listen_stop, publish
from src.apps.chat.scope import room_key

logger = logging.getLogger(__name__)

_BUSY = 'this session already has a running chat, stop it before sending another'
_STOPPED = 'chat stopped'


@dataclass
class ChatCommand:
    content: str
    provider: str
    agent: str


@dataclass
class StopCommand:
    pass


@dataclass
class RetireCommand:
    pass


type Command = ChatCommand | StopCommand | RetireCommand


class SessionRunner:
    def __init__(self, session_id: str, token: str | None, user_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self.room = room_key(user_id, session_id)
        self._helper = ChatHelper(session_id, token, user_id)
        self._commands: asyncio.Queue[Command] = asyncio.Queue()
        self._chat: asyncio.Task[None] | None = None
        self._worker = asyncio.create_task(self._run())
        self._control = asyncio.create_task(listen_stop(self.room, self._cancel))
        logger.info('runner created room=%s', self.room)

    def submit(self, command: ChatCommand) -> None:
        self._commands.put_nowait(command)

    def stop(self) -> None:
        self._commands.put_nowait(StopCommand())

    def request_retire(self) -> None:
        self._commands.put_nowait(RetireCommand())

    async def update_token(self, token: str | None) -> None:
        await self._helper.update_token(token)

    def _busy(self) -> bool:
        return self._chat is not None and not self._chat.done()

    def busy(self) -> bool:
        return self._busy()

    def _start_chat(self, command: ChatCommand, lock: ChatLock) -> None:
        self._chat = asyncio.create_task(self._chat_once(command, lock))
        self._chat.add_done_callback(lambda _: self.request_retire())

    async def _run(self) -> None:
        while True:
            command = await self._commands.get()
            if isinstance(command, RetireCommand):
                if not self._busy() and not has_clients(self.room):
                    await self._retire()
                    return
                logger.debug('retire deferred room=%s busy=%s', self.room, self._busy())
            elif isinstance(command, StopCommand):
                logger.info('stop command room=%s', self.room)
                await self._stop()
            elif self._busy():
                logger.warning('chat rejected busy room=%s', self.room)
                await publish(self.room, error_event(_BUSY))
            else:
                lock = ChatLock(self.room)
                if await lock.acquire():
                    self._start_chat(command, lock)
                else:
                    logger.warning('chat rejected locked room=%s', self.room)
                    await publish(self.room, error_event(_BUSY))

    async def _chat_once(self, command: ChatCommand, lock: ChatLock) -> None:
        await clear(self.room)
        started = monotonic()
        logger.info('chat started room=%s provider=%s agent=%s', self.room, command.provider, command.agent)
        try:
            await self._helper.chat(command.content, command.provider, command.agent)
        except asyncio.CancelledError:
            logger.warning('chat cancelled room=%s elapsed=%.1fs', self.room, monotonic() - started)
            raise
        except Exception as error:
            logger.exception('chat failed room=%s elapsed=%.1fs error=%s', self.room, monotonic() - started, error)
            await publish(self.room, error_event(str(error)))
        else:
            logger.info('chat finished room=%s elapsed=%.1fs', self.room, monotonic() - started)
        finally:
            if lock.lost:
                logger.error('chat ran without lock room=%s', self.room)
            await lock.release()
            if self._helper.persisted:
                await clear(self.room)
            else:
                logger.warning('chat not persisted, replay kept room=%s', self.room)

    async def _stop(self) -> None:
        await broadcast_stop(self.room)
        await self._cancel()

    async def _cancel(self) -> None:
        chat = self._chat
        if chat is None or chat.done():
            return
        logger.warning('cancelling running chat room=%s', self.room)
        self._chat = None
        chat.cancel()
        try:
            await chat
        except asyncio.CancelledError:
            pass
        await publish(self.room, error_event(_STOPPED))

    async def _retire(self) -> None:
        if _runners.get(self.room) is self:
            _runners.pop(self.room, None)
        self._control.cancel()
        try:
            await self._control
        except asyncio.CancelledError:
            pass
        await self._helper.close()
        logger.info('runner retired room=%s', self.room)


_runners: dict[str, SessionRunner] = {}


async def acquire_runner(session_id: str, token: str | None, user_id: str) -> SessionRunner:
    room = room_key(user_id, session_id)
    runner = _runners.get(room)
    if runner is None:
        runner = SessionRunner(session_id, token, user_id)
        _runners[room] = runner
        return runner
    await runner.update_token(token)
    return runner


def request_retire(session_id: str, user_id: str) -> None:
    runner = _runners.get(room_key(user_id, session_id))
    if runner is not None:
        runner.request_retire()


def is_busy(session_id: str, user_id: str) -> bool:
    runner = _runners.get(room_key(user_id, session_id))
    return runner is not None and runner.busy()
