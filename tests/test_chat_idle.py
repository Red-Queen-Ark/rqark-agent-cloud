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


def test_watch_progress_trips_on_a_provider_that_keeps_failing(monkeypatch):
    """A failing provider is not silence: every attempt moves the clock. Without a
    separate bound the turn would run for the whole idle timeout first."""
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_CHECK', 0.01)
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_TIMEOUT', 30)
    monkeypatch.setattr(chat_helper, 'CHAT_RETRY_LIMIT', 3)

    async def main():
        stream = RoomStream()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(30) as idle:
                task = asyncio.create_task(chat_helper._watch_progress(stream, idle))
                try:
                    # Each failure reports an error, so the idle clock keeps moving.
                    for _ in range(3):
                        await asyncio.sleep(0.02)
                        stream.failed()
                    await asyncio.wait_for(task, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        assert 'failed 3 times' in stream.stall_reason

    asyncio.run(main())


def test_a_recovered_provider_is_not_cut_off(monkeypatch):
    """Two failures then real content is a flaky model, not a dead turn."""
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_CHECK', 0.01)
    monkeypatch.setattr(chat_helper, 'CHAT_IDLE_TIMEOUT', 30)
    monkeypatch.setattr(chat_helper, 'CHAT_RETRY_LIMIT', 3)

    async def main():
        stream = RoomStream()
        async with asyncio.timeout(30) as idle:
            task = asyncio.create_task(chat_helper._watch_progress(stream, idle))
            for _ in range(5):
                await asyncio.sleep(0.02)
                stream.failed()
                stream.failed()
                stream.touch()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert stream.stall_reason == ''

    asyncio.run(main())


def test_a_failure_alone_does_not_count_as_progress():
    stream = RoomStream()
    stream.failed()
    assert stream.failures == 1
    stream.touch()
    assert stream.failures == 0
