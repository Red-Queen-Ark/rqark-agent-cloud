import asyncio

from src.apps.chat import lock as lock_module
from src.apps.chat.lock import ChatLock


async def test_lock_blocks_second_holder(room_env):
    first = ChatLock('s1')
    second = ChatLock('s1')
    assert await first.acquire() is True
    assert await second.acquire() is False
    await first.release()


async def test_release_allows_reacquire(room_env):
    first = ChatLock('s2')
    assert await first.acquire() is True
    await first.release()
    second = ChatLock('s2')
    assert await second.acquire() is True
    await second.release()


async def test_release_ignores_foreign_owner(room_env):
    first = ChatLock('s3')
    assert await first.acquire() is True
    second = ChatLock('s3')
    assert await second.acquire() is False
    await second.release()
    third = ChatLock('s3')
    assert await third.acquire() is False
    await first.release()


async def test_lock_degrades_without_redis(monkeypatch):
    async def no_redis():
        return None

    monkeypatch.setattr(lock_module, 'get_redis', no_redis)
    lock = ChatLock('s4')
    assert await lock.acquire() is True
    await lock.release()


async def test_lock_keeps_renewing_on_transient_error(room_env, monkeypatch):
    monkeypatch.setattr(lock_module, '_TTL', 0.3)
    lock = ChatLock('s5')
    assert await lock.acquire() is True
    room_env.renew_fail = True
    await asyncio.sleep(0.25)
    room_env.renew_fail = False
    assert lock.lost is False
    assert lock._heartbeat is not None and not lock._heartbeat.done()
    await lock.release()


async def test_lock_recovers_from_transient_errors(room_env, monkeypatch):
    monkeypatch.setattr(lock_module, '_TTL', 0.3)
    lock = ChatLock('s6')
    assert await lock.acquire() is True
    room_env.renew_fail = True
    await asyncio.sleep(0.15)
    room_env.renew_fail = False
    await asyncio.sleep(0.1)
    assert lock.lost is False
    other = ChatLock('s6')
    assert await other.acquire() is False
    await lock.release()


async def test_lock_reports_loss_when_owner_is_replaced(room_env, monkeypatch):
    monkeypatch.setattr(lock_module, '_TTL', 0.3)
    lock = ChatLock('s7')
    assert await lock.acquire() is True
    room_env.store['chat:busy:s7'] = 'stolen'
    await asyncio.sleep(0.15)
    assert lock.lost is True
    assert lock._heartbeat is not None and lock._heartbeat.done()
    await lock.release()


async def test_release_keeps_key_owned_by_someone_else(room_env):
    lock = ChatLock('s8')
    assert await lock.acquire() is True
    room_env.store['chat:busy:s8'] = 'stolen'
    await lock.release()
    assert room_env.store['chat:busy:s8'] == 'stolen'


async def test_lock_survives_for_whole_chat(room_env, monkeypatch):
    monkeypatch.setattr(lock_module, '_TTL', 0.2)
    lock = ChatLock('s6')
    assert await lock.acquire() is True
    await asyncio.sleep(0.5)
    other = ChatLock('s6')
    assert await other.acquire() is False
    await lock.release()
