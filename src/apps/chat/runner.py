import asyncio
from dataclasses import dataclass

from src.apps.chat.event import error_event
from src.apps.chat.helper import ChatHelper
from src.apps.chat.lock import ChatLock
from src.apps.chat.replay import clear
from src.apps.chat.room import broadcast_stop, has_clients, listen_stop, publish

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

    def submit(self, command: ChatCommand) -> None:
        self._commands.put_nowait(command)

    def stop(self) -> None:
        self._commands.put_nowait(StopCommand())

    def request_retire(self) -> None:
        self._commands.put_nowait(RetireCommand())

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
            elif isinstance(command, StopCommand):
                await self._stop()
            elif self._busy():
                await publish(self.session_id, error_event(_BUSY))
            else:
                lock = ChatLock(self.session_id)
                if await lock.acquire():
                    self._start_chat(command, lock)
                else:
                    await publish(self.session_id, error_event(_BUSY))

    async def _chat_once(self, command: ChatCommand, lock: ChatLock) -> None:
        await clear(self.session_id)
        try:
            await self._helper.chat(command.content, command.provider, command.agent)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await publish(self.session_id, error_event(str(error)))
        finally:
            await lock.release()
            await clear(self.session_id)

    async def _stop(self) -> None:
        await broadcast_stop(self.session_id)
        await self._cancel()

    async def _cancel(self) -> None:
        chat = self._chat
        if chat is None or chat.done():
            return
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


_runners: dict[str, SessionRunner] = {}


def acquire_runner(session_id: str, token: str | None) -> SessionRunner:
    runner = _runners.get(session_id)
    if runner is None:
        runner = SessionRunner(session_id, token)
        _runners[session_id] = runner
    return runner


def request_retire(session_id: str) -> None:
    runner = _runners.get(session_id)
    if runner is not None:
        runner.request_retire()
