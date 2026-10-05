import json

from src.apps.chat import replay


async def test_record_then_read(room_env):
    await replay.record('s1', {'n': 1})
    await replay.record('s1', {'n': 2})
    assert await replay.read('s1') == [{'n': 1}, {'n': 2}]


async def test_clear_removes_events(room_env):
    await replay.record('s2', {'n': 1})
    await replay.clear('s2')
    assert await replay.read('s2') == []


async def test_record_is_isolated_per_session(room_env):
    await replay.record('s3', {'n': 1})
    await replay.record('s4', {'n': 2})
    assert await replay.read('s3') == [{'n': 1}]
    assert await replay.read('s4') == [{'n': 2}]


async def test_read_skips_corrupt_entries(room_env):
    await room_env.rpush('chat:replay:s5', 'not json', json.dumps({'n': 1}))
    assert await replay.read('s5') == [{'n': 1}]


async def test_replay_survives_redis_outage(room_env, monkeypatch):
    import src.apps.chat.replay as replay_module

    await replay.record('s6', {'n': 1})

    async def no_redis():
        return None

    monkeypatch.setattr(replay_module, 'get_redis', no_redis)
    assert await replay.read('s6') == []
    await replay.record('s6', {'n': 2})
