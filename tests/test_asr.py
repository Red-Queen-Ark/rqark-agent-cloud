import struct
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.app import create_fastapi_app
from src.apps.asr import engine as engine_module
from src.apps.asr import routes as routes_module
from src.apps.asr.engines import build_engine, qwen3_language, whisper_language
from src.apps.asr.errors import AsrBusyError, AsrTimeoutError, AsrUnavailableError
from src.middleware.auth import get_current_user

HEADERS = {'Authorization': 'Bearer test'}


def build_wav(values, rate=16000):
    payload = np.array(values, dtype='<i2').tobytes()
    fmt = struct.pack('<HHIIHH', 1, 1, rate, rate * 2, 2, 16)
    body = b'fmt ' + struct.pack('<I', len(fmt)) + fmt
    body += b'data' + struct.pack('<I', len(payload)) + payload
    return b'RIFF' + struct.pack('<I', 4 + len(body)) + b'WAVE' + body


class FakeEngine:
    name = 'fake'

    def __init__(self, gate=None):
        self.seen = []
        self.gate = gate

    def transcribe(self, audio):
        if self.gate is not None:
            self.gate.wait(timeout=2)
        self.seen.append(audio)
        return '你系边个'


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()

    async def transcribe(audio):
        return engine_module.postprocess(engine.transcribe(audio), engine_module.get_converter())

    monkeypatch.setattr(engine_module, '_engine', engine)
    monkeypatch.setattr(engine_module, 'get_engine', lambda: engine)
    monkeypatch.setattr(engine_module, 'transcribe', transcribe)
    return engine


@pytest.fixture
def client():
    app = create_fastapi_app()
    app.dependency_overrides[get_current_user] = lambda: {'id': 'u1'}
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_transcribe_returns_cantonese_text(client, fake_engine):
    response = client.post('/api/asr', headers=HEADERS, files={'file': ('a.wav', build_wav([0] * 16000), 'audio/wav')})
    assert response.status_code == 200
    body = response.json()
    assert body['text'] == '你係邊個'
    assert body['duration_ms'] == 1000
    assert body['elapsed_ms'] >= 0


def test_transcribe_needs_authentication():
    app = create_fastapi_app()
    with TestClient(app) as test_client:
        response = test_client.post('/api/asr', files={'file': ('a.wav', build_wav([0] * 160), 'audio/wav')})
    assert response.status_code == 401
    assert response.json()['error']['code'] == 'UNAUTHORIZED'


def test_transcribe_rejects_empty_upload(client, fake_engine):
    response = client.post('/api/asr', headers=HEADERS, files={'file': ('a.wav', b'', 'audio/wav')})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'AUDIO_EMPTY'


def test_transcribe_rejects_oversized_upload(client, fake_engine, monkeypatch):
    monkeypatch.setattr(routes_module, 'ASR_MAX_BYTES', 32)
    response = client.post('/api/asr', headers=HEADERS, files={'file': ('a.wav', build_wav([0] * 1600), 'audio/wav')})
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'AUDIO_TOO_LARGE'


def test_transcribe_rejects_unsupported_audio(client, fake_engine, monkeypatch):
    monkeypatch.setattr(routes_module.audio_module, '_decode_ffmpeg', lambda data: audio_unsupported())
    response = client.post('/api/asr', headers=HEADERS, files={'file': ('a.webm', b'not audio at all', 'audio/webm')})
    assert response.status_code == 415
    assert response.json()['error']['code'] == 'UNSUPPORTED_AUDIO'


def audio_unsupported():
    from src.apps.asr.errors import AudioUnsupportedError

    raise AudioUnsupportedError('ffmpeg is not installed, only wav uploads are accepted')


def test_transcribe_reports_a_full_queue(client, fake_engine, monkeypatch):
    async def busy(audio):
        raise AsrBusyError('the asr queue is full')

    monkeypatch.setattr(engine_module, 'transcribe', busy)
    response = client.post('/api/asr', headers=HEADERS, files={'file': ('a.wav', build_wav([0] * 160), 'audio/wav')})
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'ASR_BUSY'


def test_transcribe_reports_a_timeout(client, fake_engine, monkeypatch):
    async def slow(audio):
        raise AsrTimeoutError('transcription took longer than 60s')

    monkeypatch.setattr(engine_module, 'transcribe', slow)
    response = client.post('/api/asr', headers=HEADERS, files={'file': ('a.wav', build_wav([0] * 160), 'audio/wav')})
    assert response.status_code == 504
    assert response.json()['error']['code'] == 'ASR_TIMEOUT'


def test_health_reports_the_engine_state(client, fake_engine):
    response = client.get('/api/asr/health')
    assert response.status_code == 200
    body = response.json()
    assert body['loaded'] is True
    assert body['engine'] == 'fake'
    assert body['opencc'] is True


def test_get_engine_loads_once(monkeypatch):
    built = []

    def fake_build(name, model, language=None, hotwords=None):
        built.append(name)
        return FakeEngine()

    engine_module.reset()
    monkeypatch.setattr(engine_module, 'build_engine', fake_build)
    first = engine_module.get_engine()
    second = engine_module.get_engine()
    assert first is second
    assert built == [engine_module.ASR_ENGINE]
    engine_module.reset()


def test_cantonese_maps_to_a_whisper_code():
    assert whisper_language('Cantonese') == 'yue'
    assert whisper_language('yue') == 'yue'
    assert whisper_language('en') == 'en'
    assert whisper_language('') is None
    assert whisper_language(None) is None


def test_yue_maps_to_the_qwen3_language_name():
    assert qwen3_language('yue') == 'Cantonese'
    assert qwen3_language('Cantonese') == 'Cantonese'
    assert qwen3_language('en') == 'en'
    assert qwen3_language(None) is None


def test_unknown_engine_is_rejected():
    with pytest.raises(AsrUnavailableError, match='unknown asr engine'):
        build_engine('parakeet')


def test_engine_state_before_loading():
    engine_module.reset()
    state = engine_module.state()
    assert state['loaded'] is False
    assert state['engine'] is None
    assert state['concurrency'] >= 1


def test_a_full_queue_is_refused(monkeypatch):
    engine_module.reset()
    monkeypatch.setattr(engine_module, 'ASR_CONCURRENCY', 1)
    monkeypatch.setattr(engine_module, 'ASR_QUEUE_DEPTH', 0)
    gate = threading.Event()
    engine = FakeEngine(gate=gate)
    try:
        engine_module._submit(engine, np.zeros(10, dtype=np.float32))
        with pytest.raises(AsrBusyError):
            engine_module._submit(engine, np.zeros(10, dtype=np.float32))
    finally:
        gate.set()
        engine_module.reset()


def test_a_running_job_keeps_its_slot_counted(monkeypatch):
    engine_module.reset()
    monkeypatch.setattr(engine_module, 'ASR_CONCURRENCY', 1)
    monkeypatch.setattr(engine_module, 'ASR_QUEUE_DEPTH', 0)
    gate = threading.Event()
    engine = FakeEngine(gate=gate)
    try:
        engine_module._submit(engine, np.zeros(10, dtype=np.float32))
        assert engine_module.state()['queued'] == 1
    finally:
        gate.set()
        engine_module.reset()


async def test_transcribe_applies_postprocess(monkeypatch):
    engine_module.reset()
    monkeypatch.setattr(engine_module, 'get_engine', lambda: FakeEngine())
    text = await engine_module.transcribe(np.zeros(10, dtype=np.float32))
    assert text == '你係邊個'


async def test_transcribe_times_out(monkeypatch):
    engine_module.reset()
    monkeypatch.setattr(engine_module, 'get_engine', lambda: FakeEngine())
    monkeypatch.setattr(engine_module, 'ASR_TIMEOUT', 0.01)

    gate = threading.Event()

    def hang(audio):
        gate.wait(timeout=2)
        return 'too late'

    monkeypatch.setattr(FakeEngine, 'transcribe', lambda self, audio: hang(audio))
    try:
        with pytest.raises(AsrTimeoutError):
            await engine_module.transcribe(np.zeros(10, dtype=np.float32))
    finally:
        gate.set()
        engine_module.reset()


async def test_transcribe_of_silence_returns_empty(monkeypatch):
    engine_module.reset()
    monkeypatch.setattr(engine_module, 'get_engine', lambda: FakeEngine())
    monkeypatch.setattr(FakeEngine, 'transcribe', lambda self, audio: '係。')
    text = await engine_module.transcribe(np.zeros(10, dtype=np.float32))
    assert text == ''
