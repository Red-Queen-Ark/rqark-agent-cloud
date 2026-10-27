import asyncio

import pytest

from src.apps.chat import helper as chat_helper
from src.apps.chat.event import RoomStream


def test_watch_progress_extends_while_events_keep_coming(monkeypatch):
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_CHECK', 0.01)
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_TIMEOUT', 0.2)

    async def main():
        stream = RoomStream()
        async with asyncio.timeout(0.2) as idle:
            task = asyncio.create_task(chat_helper._watch_progress(stream, idle))
            for _ in range(20):
                await asyncio.sleep(0.03)
                stream.touch()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_watch_progress_trips_on_silence(monkeypatch):
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_CHECK', 0.01)
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_TIMEOUT', 0.1)

    async def main():
        stream = RoomStream()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1) as idle:
                await chat_helper._watch_progress(stream, idle)

    asyncio.run(main())
