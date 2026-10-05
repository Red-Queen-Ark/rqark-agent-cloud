import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from src.apps.chat.event import error_event
from src.apps.chat.helper import ChatHelper
from src.apps.chat.lock import ChatLock
from src.apps.chat.replay import clear
from src.apps.chat.room import broadcast_stop, has_clients, listen_stop, publish

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
    def __init__(self, session_id: str, token: str | None):
        self.session_id = session_id
        self._helper = ChatHelper(session_id, token)
        self._commands: asyncio.Queue[Command] = asyncio.Queue()
        self._chat: asyncio.Task[None] | None = None
        self._worker = asyncio.create_task(self._run())
        self._control = asyncio.create_task(listen_stop(session_id, self._cancel))
        logger.info('runner created session=%s', session_id)

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

    def _start_chat(self, command: ChatCommand, lock: ChatLock) -> None:
        self._chat = asyncio.create_task(self._chat_once(command, lock))
        self._chat.add_done_callback(lambda _: self.request_retire())

    async def _run(self) -> None:
        while True:
            command = await self._commands.get()
            if isinstance(command, RetireCommand):
                if not self._busy() and not has_clients(self.session_id):
                    await self._retire()
                    return
                logger.debug('retire deferred session=%s busy=%s', self.session_id, self._busy())
            elif isinstance(command, StopCommand):
                logger.info('stop command session=%s', self.session_id)
                await self._stop()
            elif self._busy():
                logger.warning('chat rejected busy session=%s', self.session_id)
                await publish(self.session_id, error_event(_BUSY))
            else:
                lock = ChatLock(self.session_id)
                if await lock.acquire():
                    self._start_chat(command, lock)
                else:
                    logger.warning('chat rejected locked session=%s', self.session_id)
                    await publish(self.session_id, error_event(_BUSY))

    async def _chat_once(self, command: ChatCommand, lock: ChatLock) -> None:
        await clear(self.session_id)
        started = monotonic()
        logger.info('chat started session=%s provider=%s agent=%s', self.session_id, command.provider, command.agent)
        try:
            await self._helper.chat(command.content, command.provider, command.agent)
        except asyncio.CancelledError:
            logger.warning('chat cancelled session=%s elapsed=%.1fs', self.session_id, monotonic() - started)
            raise
        except Exception as error:
            logger.exception('chat failed session=%s elapsed=%.1fs error=%s', self.session_id, monotonic() - started, error)
            await publish(self.session_id, error_event(str(error)))
        else:
            logger.info('chat finished session=%s elapsed=%.1fs', self.session_id, monotonic() - started)
        finally:
            if lock.lost:
                logger.error('chat ran without lock session=%s', self.session_id)
            await lock.release()
            if self._helper.persisted:
                await clear(self.session_id)
            else:
                logger.warning('chat not persisted, replay kept session=%s', self.session_id)

    async def _stop(self) -> None:
        await broadcast_stop(self.session_id)
        await self._cancel()

    async def _cancel(self) -> None:
        chat = self._chat
        if chat is None or chat.done():
            return
        logger.warning('cancelling running chat session=%s', self.session_id)
        self._chat = None
        chat.cancel()
        try:
            await chat
        except asyncio.CancelledError:
            pass
        await publish(self.session_id, error_event(_STOPPED))

    async def _retire(self) -> None:
        if _runners.get(self.session_id) is self:
            _runners.pop(self.session_id, None)
        self._control.cancel()
        try:
            await self._control
        except asyncio.CancelledError:
            pass
        await self._helper.close()
        logger.info('runner retired session=%s', self.session_id)


_runners: dict[str, SessionRunner] = {}


async def acquire_runner(session_id: str, token: str | None) -> SessionRunner:
    runner = _runners.get(session_id)
    if runner is None:
        runner = SessionRunner(session_id, token)
        _runners[session_id] = runner
        return runner
    await runner.update_token(token)
    return runner


def request_retire(session_id: str) -> None:
    runner = _runners.get(session_id)
    if runner is not None:
        runner.request_retire()
