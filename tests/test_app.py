import os
import sys

from fastapi.testclient import TestClient

from src import config
from src.app import create_fastapi_app


def _build(monkeypatch, enabled: bool) -> TestClient:
    monkeypatch.setattr(config, 'SPEECH_ENABLED', enabled)
    return TestClient(create_fastapi_app())


def test_voice_routes_are_mounted_when_enabled(monkeypatch):
    client = _build(monkeypatch, True)
    assert client.get('/api/voice/health').status_code == 200
    assert client.post('/api/asr').status_code != 404


def test_disabling_voice_drops_asr_and_voice_routes(monkeypatch):
    client = _build(monkeypatch, False)
    assert client.get('/api/voice/health').status_code == 404
    assert client.post('/api/asr').status_code == 404
    assert client.get('/api/ping').status_code == 200


def test_disabling_voice_never_imports_the_audio_stages(monkeypatch):
    for name in list(sys.modules):
        if name.startswith(('openwakeword', 'onnxruntime', 'src.apps.asr', 'src.apps.voice')):
            monkeypatch.delitem(sys.modules, name)
    _build(monkeypatch, False)
    assert not any(name.startswith(('openwakeword', 'src.apps.asr.routes', 'src.apps.voice')) for name in sys.modules)


def test_voice_is_off_unless_the_env_turns_it_on(monkeypatch):
    monkeypatch.delenv('SPEECH_ENABLED', raising=False)
    import importlib

    importlib.reload(config)
    assert config.SPEECH_ENABLED is False
    os.environ['SPEECH_ENABLED'] = 'true'
    importlib.reload(config)
