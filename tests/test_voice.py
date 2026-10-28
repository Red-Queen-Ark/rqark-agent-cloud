import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from src.apps.asr.errors import AsrTimeoutError
from src.apps.voice import service as service_module
from src.apps.voice import vad, wake
from src.apps.voice.protocol import wake_event
from src.apps.voice.registry import Registry, registry
from src.apps.voice.session import duration_ms, int16_from_bytes, samples_from_bytes


class FakeConn:
    def __init__(self, actions, token: str | None = 't'):
        self.query_params = {} if token is None else {'token': token}
        self.state = SimpleNamespace()
        self.actions = list(actions)
        self.sent = []
        self.close_code = None

    async def accept(self):
        return None

    async def close(self, code=1000):
        self.close_code = code

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self):
        if not self.actions:
            return {'type': 'websocket.disconnect'}
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if isinstance(action, bytes):
            return {'type': 'websocket.receive', 'bytes': action}
        if isinstance(action, str):
            return {'type': 'websocket.receive', 'text': action}
        return action


def pcm(samples: int) -> bytes:
    return b'\x00\x40' * samples


class FakeWakeModel:
    def __init__(self, scores, name='wednesday'):
        self.scores = list(scores)
        self.name = name
        self.resets = 0

    def predict(self, frame, frame_size=None):
        return {self.name: self.scores.pop(0) if self.scores else 0.0}

    def reset(self):
        self.resets += 1


class FakeVadModel:
    def __init__(self, voiced_frames):
        self.voiced_frames = voiced_frames
        self.frames = 0

    def predict(self, frame, frame_size=480):
        self.frames += 1
        return np.array([1.0 if self.frames <= self.voiced_frames else 0.0], dtype=np.float32)

    def reset_states(self):
        return None


def fake_segmenter(voiced_frames, silence_ms=90, min_ms=30):
    return vad.Segmenter(threshold=0.5, silence_ms=silence_ms, lead_ms=90, min_ms=min_ms, load=lambda: FakeVadModel(voiced_frames))


def make_env(monkeypatch, user: dict | None = {'id': 'u1'}, segmenter=None, detector=None, transcript='你好'):
    async def authenticate(token):
        return None if token is None else user

    async def transcribe(samples):
        return transcript

    monkeypatch.setattr(service_module, 'authenticate', authenticate)
    monkeypatch.setattr(service_module.wake, 'for_connection', lambda: detector)
    monkeypatch.setattr(service_module, 'build_segmenter', lambda: segmenter)
    monkeypatch.setattr(service_module.asr_engine, 'transcribe', transcribe)


async def run(actions, token='t'):
    conn = FakeConn(actions, token)
    await service_module.voice_websocket(conn)
    return conn


def run_sync(actions, token='t'):
    return asyncio.run(run(actions, token))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    registry._sockets.clear()
    make_env(monkeypatch)


def test_audio_bytes_become_float_samples():
    samples = samples_from_bytes(b'\x00\x40')

    assert samples.dtype == np.float32
    assert samples[0] == pytest.approx(0.5)


def test_int16_samples_keep_the_raw_values():
    samples = int16_from_bytes(b'\x00\x40')

    assert samples.dtype == np.int16
    assert samples[0] == 16384


def test_odd_byte_count_keeps_whole_samples():
    assert samples_from_bytes(b'\x01\x02\x03').size == 1


def test_duration_of_one_second():
    assert duration_ms(16000 * 2) == 1000


def test_a_short_chunk_is_carried_into_the_next_wake_frame():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.0]))
    half = np.zeros(wake.FRAME_SIZE // 2, dtype=np.int16)

    assert detector.push(half).score is None
    assert detector.push(half).score == 0.0


def test_the_highest_wake_score_in_a_chunk_wins():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.1, 0.9]))

    assert detector.push(np.zeros(wake.FRAME_SIZE * 2, dtype=np.int16)).score == pytest.approx(0.9)


def test_a_wake_score_under_the_threshold_does_not_fire():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.49]))

    assert detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).woke is False


def test_the_wake_score_is_a_plain_float():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([np.float32(0.87)]))

    score = detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).score

    assert type(score) is float
    assert json.loads(json.dumps(wake_event(score)))['score'] == 0.87


def test_firing_resets_the_wake_stream():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.9]))

    assert detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).woke is True
    assert detector.model.resets == 1


def test_the_audio_after_the_wake_word_is_handed_back():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.9]))

    detection = detector.push(np.zeros(wake.FRAME_SIZE * 3, dtype=np.int16))

    assert detection.woke is True
    assert detection.rest.size == wake.FRAME_SIZE * 2


def test_a_second_wake_within_the_cooldown_is_ignored():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, cooldown=0.08, load=lambda paths: FakeWakeModel([0.9] * 3))

    assert detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).woke is True
    assert detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).woke is False


def test_the_wake_word_is_heard_again_once_the_cooldown_passes():
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, cooldown=0.08, load=lambda paths: FakeWakeModel([0.9] * 4))

    detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16))

    assert detector.push(np.zeros(wake.FRAME_SIZE * 2, dtype=np.int16)).woke is True


def test_a_loaded_model_is_handed_to_the_next_connection(monkeypatch):
    monkeypatch.setattr(wake, '_spare', {})
    monkeypatch.setattr(wake, '_load', lambda paths: FakeWakeModel([0.0]))
    first = wake.Detector(paths=['wednesday.onnx'])
    first.close()

    assert wake.Detector(paths=['wednesday.onnx']).model is first.model


class MultiWakeModel:
    def __init__(self, scores):
        self.scores = list(scores)
        self.resets = 0

    def predict(self, frame, frame_size=None):
        return self.scores.pop(0) if self.scores else {}

    def reset(self):
        self.resets += 1


def test_several_models_are_scored_on_the_same_frames():
    detector = wake.Detector(paths=['wednesday.onnx', 'diana.onnx'], threshold=0.5, load=lambda paths: MultiWakeModel([{'wednesday': 0.1, 'diana': 0.8}]))

    detection = detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16))

    assert detection.woke is True
    assert detection.name == 'diana'


def test_the_firing_model_is_the_one_that_is_named():
    detector = wake.Detector(paths=['wednesday.onnx', 'diana.onnx'], threshold=0.5, load=lambda paths: MultiWakeModel([{'wednesday': 0.7, 'diana': 0.2}]))

    assert detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).name == 'wednesday'


def test_a_cooldown_only_silences_the_model_that_fired():
    detector = wake.Detector(
        paths=['wednesday.onnx', 'diana.onnx'],
        threshold=0.5,
        cooldown=1.0,
        load=lambda paths: MultiWakeModel([{'wednesday': 0.9, 'diana': 0.0}, {'wednesday': 0.9, 'diana': 0.9}]),
    )

    assert detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16)).name == 'wednesday'

    second = detector.push(np.zeros(wake.FRAME_SIZE, dtype=np.int16))

    assert second.woke is True
    assert second.name == 'diana'


def test_a_comma_list_in_the_setting_loads_every_model(monkeypatch):
    monkeypatch.setattr(wake, 'VOICE_WAKE_MODEL', '/models/a.onnx, /models/b.onnx')

    assert wake.model_names() == ['a', 'b']


def test_a_bundled_name_resolves_without_a_path(monkeypatch):
    monkeypatch.setattr(wake, 'VOICE_WAKE_MODEL', 'wednesday, diana.onnx')

    assert wake.models() == [str(wake.bundled('wednesday')), str(wake.bundled('diana'))]


def test_an_unknown_name_is_left_alone(monkeypatch):
    monkeypatch.setattr(wake, 'VOICE_WAKE_MODEL', 'nothere')

    assert wake.models() == ['nothere']


def test_each_model_configuration_is_pooled_on_its_own(monkeypatch):
    monkeypatch.setattr(wake, '_spare', {})
    monkeypatch.setattr(wake, '_load', lambda paths: MultiWakeModel([]))
    both = wake.Detector(paths=['wednesday.onnx', 'diana.onnx'])
    both.close()

    assert wake.Detector(paths=['wednesday.onnx', 'diana.onnx']).model is both.model
    assert wake.Detector(paths=['wednesday.onnx']).model is not both.model


def test_the_ready_frame_lists_the_models(monkeypatch):
    detector = wake.Detector(paths=['wednesday.onnx', 'diana.onnx'], threshold=0.5, load=lambda paths: MultiWakeModel([]))
    make_env(monkeypatch, detector=detector)

    conn = run_sync([])

    assert conn.sent[0]['wake_models'] == ['wednesday', 'diana']


def test_the_bundled_model_is_used_when_the_setting_is_empty(monkeypatch):
    monkeypatch.setattr(wake, 'VOICE_WAKE_MODEL', '')

    assert wake.model_names() == ['wednesday']


def test_no_wake_detector_without_any_model(monkeypatch):
    monkeypatch.setattr(wake, 'VOICE_WAKE_MODEL', '')
    monkeypatch.setattr(wake, 'bundled', lambda name='wednesday': Path('/nowhere/missing.onnx'))

    assert wake.for_connection() is None


def test_a_missing_wakeword_package_leaves_the_stage_off(monkeypatch):
    def boom(paths):
        raise wake.WakeUnavailableError('not installed')

    monkeypatch.setattr(wake, 'VOICE_WAKE_MODEL', '/tmp/wednesday.onnx')
    monkeypatch.setattr(wake, '_load', boom)

    assert wake.for_connection() is None


def test_the_segmenter_keeps_the_frames_before_the_first_voiced_one():
    segmenter = fake_segmenter(voiced_frames=3, silence_ms=60, min_ms=30)

    segments = segmenter.push(np.zeros(vad.FRAME_SIZE * 6, dtype=np.int16))

    assert len(segments) == 1
    assert segments[0].samples.size == vad.FRAME_SIZE * 5
    assert segments[0].truncated is False


def test_a_too_short_burst_is_not_an_utterance():
    segmenter = fake_segmenter(voiced_frames=1, silence_ms=60, min_ms=90)

    assert segmenter.push(np.zeros(vad.FRAME_SIZE * 6, dtype=np.int16)) == []


def test_an_utterance_that_never_ends_is_cut_at_the_cap():
    segmenter = vad.Segmenter(threshold=0.5, silence_ms=1000, min_ms=30, max_seconds=1, load=lambda: FakeVadModel(10_000))

    segments = segmenter.push(np.zeros(vad.FRAME_SIZE * 200, dtype=np.int16))

    assert segments
    assert all(segment.truncated for segment in segments)
    assert all(segment.duration_ms <= 1000 for segment in segments)


def test_flushing_a_silent_stream_yields_nothing():
    segmenter = fake_segmenter(voiced_frames=0)

    assert segmenter.flush() is None


def test_flushing_a_running_utterance_returns_it():
    segmenter = fake_segmenter(voiced_frames=3, silence_ms=1000, min_ms=30)
    segmenter.push(np.zeros(vad.FRAME_SIZE * 3, dtype=np.int16))

    segment = segmenter.flush()

    assert segment is not None
    assert segment.samples.size == vad.FRAME_SIZE * 3
    assert segmenter.flush() is None


async def test_ready_frame_describes_the_wire_format():
    conn = await run([])

    assert conn.sent[0]['type'] == 'ready'
    assert conn.sent[0]['sample_rate'] == 16000
    assert conn.sent[0]['format'] == 'pcm_s16le'
    assert conn.sent[0]['channels'] == 1
    assert conn.sent[0]['wake_word'] is False
    assert conn.sent[0]['vad'] is False


async def test_a_voiced_utterance_comes_back_as_a_final_event(monkeypatch):
    make_env(monkeypatch, segmenter=fake_segmenter(voiced_frames=3, silence_ms=60, min_ms=30))
    conn = await run([pcm(vad.FRAME_SIZE * 6)])

    assert conn.sent[0]['vad'] is True
    assert conn.sent[1]['type'] == 'final'
    assert conn.sent[1]['text'] == '你好'
    assert conn.sent[1]['truncated'] is False


async def test_a_wake_word_resets_the_utterance(monkeypatch):
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.9]))
    make_env(monkeypatch, detector=detector, segmenter=fake_segmenter(voiced_frames=0))
    conn = await run(['{"type":"mode","value":"wake"}', pcm(wake.FRAME_SIZE)])

    assert conn.sent[0]['wake_word'] is True
    assert conn.sent[1] == {'type': 'wake', 'score': 0.9, 'name': 'wednesday'}


async def test_speech_before_the_wake_word_never_reaches_the_transcriber(monkeypatch):
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.1] * 10))
    make_env(monkeypatch, detector=detector, segmenter=fake_segmenter(voiced_frames=3, silence_ms=60, min_ms=30))
    conn = await run(['{"type":"mode","value":"wake"}', pcm(vad.FRAME_SIZE * 6)])

    assert conn.sent[1:] == []


async def test_the_rest_of_the_chunk_after_the_wake_word_still_runs(monkeypatch):
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.9]))
    make_env(monkeypatch, detector=detector, segmenter=fake_segmenter(voiced_frames=3, silence_ms=60, min_ms=30))
    conn = await run(['{"type":"mode","value":"wake"}', pcm(wake.FRAME_SIZE + vad.FRAME_SIZE * 6)])

    assert conn.sent[1] == {'type': 'wake', 'score': 0.9, 'name': 'wednesday'}
    assert conn.sent[2]['type'] == 'final'


async def test_a_wake_word_nobody_followed_up_puts_the_socket_back_to_armed(monkeypatch):
    monkeypatch.setattr(service_module, 'VOICE_WAKE_WINDOW', 0.0)
    detector = wake.Detector(paths=['wednesday.onnx'], threshold=0.5, load=lambda paths: FakeWakeModel([0.9] + [0.0] * 10))
    make_env(monkeypatch, detector=detector, segmenter=fake_segmenter(voiced_frames=0))
    conn = await run(['{"type":"mode","value":"wake"}', pcm(wake.FRAME_SIZE), pcm(wake.FRAME_SIZE)])

    assert [frame.get('error', {}).get('code') for frame in conn.sent] == [None, None, 'VOICE_WAKE_TIMEOUT']


async def test_stop_without_speech_reports_an_empty_utterance(monkeypatch):
    make_env(monkeypatch, segmenter=fake_segmenter(voiced_frames=0))
    conn = await run(['{"type":"stop"}'])

    assert conn.sent[1]['error']['code'] == 'AUDIO_EMPTY'


async def test_stop_transcribes_what_is_buffered(monkeypatch):
    make_env(monkeypatch, segmenter=fake_segmenter(voiced_frames=3, silence_ms=1000, min_ms=30))
    conn = await run([pcm(vad.FRAME_SIZE * 3), '{"type":"stop"}'])

    assert conn.sent[-1]['type'] == 'final'
    assert conn.sent[-1]['text'] == '你好'


async def test_a_transcription_failure_keeps_the_socket_open(monkeypatch):
    make_env(monkeypatch, segmenter=fake_segmenter(voiced_frames=3, silence_ms=1000, min_ms=30))

    async def timeout(samples):
        raise AsrTimeoutError('too slow')

    monkeypatch.setattr(service_module.asr_engine, 'transcribe', timeout)
    conn = await run([pcm(vad.FRAME_SIZE * 3), '{"type":"stop"}', '{"type":"start"}'])

    assert conn.sent[-2]['error']['code'] == 'ASR_TIMEOUT'
    assert conn.sent[-1] == {'type': 'started'}
    assert conn.close_code is None


async def test_cancel_drops_the_audio(monkeypatch):
    make_env(monkeypatch, segmenter=fake_segmenter(voiced_frames=3, silence_ms=1000, min_ms=30))
    conn = await run([pcm(vad.FRAME_SIZE * 3), '{"type":"cancel"}', '{"type":"stop"}'])

    assert conn.sent[-1]['error']['code'] == 'AUDIO_EMPTY'


async def test_bad_json_is_reported_and_the_socket_stays_open():
    conn = await run(['not json', '{"type":"start"}'])

    assert conn.sent[1]['error']['code'] == 'BAD_FRAME'
    assert conn.sent[2] == {'type': 'started'}
    assert conn.close_code is None


async def test_unknown_frame_type_is_reported():
    conn = await run(['{"type":"heard-you"}'])

    assert conn.sent[1]['error']['code'] == 'BAD_FRAME'


async def test_missing_token_closes_4401():
    conn = await run([], token=None)

    assert conn.close_code == 4401
    assert conn.sent == []


async def test_a_user_without_id_closes_4401(monkeypatch):
    make_env(monkeypatch, user={'email': 'a@b.c'})
    conn = await run([])

    assert conn.close_code == 4401


async def test_disconnect_ends_the_loop_quietly():
    conn = await run([WebSocketDisconnect()])

    assert conn.close_code is None


def test_the_registry_refuses_a_second_socket_for_the_same_user():
    local = Registry(max_connections=4, max_per_user=1)
    local.add('u1', object())

    assert local.reason_to_refuse('u1') is not None
    assert local.reason_to_refuse('u2') is None


def test_the_registry_refuses_past_the_connection_cap():
    local = Registry(max_connections=1, max_per_user=1)
    local.add('u1', object())

    assert local.reason_to_refuse('u2') is not None


def test_the_registry_forgets_a_user_once_the_last_socket_leaves():
    local = Registry(max_connections=4, max_per_user=1)
    socket = object()
    local.add('u1', socket)
    local.remove('u1', socket)

    assert local.total == 0
    assert local.state()['users'] == 0


async def test_a_second_connection_for_one_user_is_closed_4429():
    registry._sockets['u1'] = {object()}

    conn = await run([])

    assert conn.sent[0]['error']['code'] == 'VOICE_BUSY'
    assert conn.close_code == 4429


async def test_the_socket_is_released_on_disconnect():
    await run([WebSocketDisconnect()])

    assert registry.total == 0


async def test_an_idle_socket_is_closed(monkeypatch):
    monkeypatch.setattr(service_module, 'VOICE_IDLE_TIMEOUT', 0.01)

    class SlowConn(FakeConn):
        async def receive(self):
            import asyncio

            await asyncio.sleep(0.2)
            return {'type': 'websocket.disconnect'}

    conn = SlowConn([], 't')
    await service_module.voice_websocket(conn)

    assert conn.sent[-1]['error']['code'] == 'VOICE_IDLE'
    assert conn.close_code == 4408
