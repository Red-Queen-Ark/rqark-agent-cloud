import struct

import numpy as np
import pytest

from src.apps.asr import audio
from src.apps.asr.errors import AudioEmptyError, AudioTooLargeError, AudioTooLongError, AudioUnsupportedError


def build_wav(payload, rate=16000, channels=1, bits=16, tag=1):
    block = channels * bits // 8
    fmt = struct.pack('<HHIIHH', tag, channels, rate, rate * block, block, bits)
    body = b'fmt ' + struct.pack('<I', len(fmt)) + fmt + (b'\x00' if len(fmt) % 2 else b'')
    body += b'data' + struct.pack('<I', len(payload)) + payload + (b'\x00' if len(payload) % 2 else b'')
    return b'RIFF' + struct.pack('<I', 4 + len(body)) + b'WAVE' + body


def pcm16(values):
    return np.array(values, dtype='<i2').tobytes()


def test_decode_mono_16_bit():
    samples = audio.decode(build_wav(pcm16([0, 16384, -32768])))
    assert samples.dtype == np.float32
    assert samples.size == 3
    assert samples[0] == pytest.approx(0.0)
    assert samples[1] == pytest.approx(0.5)
    assert samples[2] == pytest.approx(-1.0)


def test_decode_mixes_stereo_to_mono():
    payload = np.array([[1000, 3000], [-1000, 1000]], dtype='<i2').tobytes()
    samples = audio.decode(build_wav(payload, channels=2))
    assert samples.size == 2
    assert samples[0] == pytest.approx(2000 / 32768)
    assert samples[1] == pytest.approx(0.0)


def test_decode_8_bit():
    samples = audio.decode(build_wav(bytes([128, 255, 0]), bits=8))
    assert samples[0] == pytest.approx(0.0)
    assert samples[1] == pytest.approx(127 / 128)
    assert samples[2] == pytest.approx(-1.0)


def test_decode_24_bit():
    payload = b''.join(int(value).to_bytes(3, 'little', signed=True) for value in (0, 4194304, -8388608))
    samples = audio.decode(build_wav(payload, bits=24))
    assert samples.size == 3
    assert samples[1] == pytest.approx(0.5)
    assert samples[2] == pytest.approx(-1.0)


def test_decode_32_bit_float():
    payload = np.array([0.0, 0.25, -0.5], dtype='<f4').tobytes()
    samples = audio.decode(build_wav(payload, bits=32, tag=3))
    assert samples[1] == pytest.approx(0.25)
    assert samples[2] == pytest.approx(-0.5)


def test_decode_resamples_to_sixteen_kilohertz():
    samples = audio.decode(build_wav(pcm16(np.zeros(4800, dtype='<i2')), rate=48000))
    assert samples.size == pytest.approx(1600, abs=1)


def test_decode_rejects_unsupported_sample_width():
    with pytest.raises(AudioUnsupportedError):
        audio.decode(build_wav(pcm16([0, 1]), bits=12))


def test_decode_rejects_encoding_other_than_pcm_or_float():
    with pytest.raises(AudioUnsupportedError):
        audio.decode(build_wav(b'\x00' * 8, bits=16, tag=7))


def test_decode_rejects_empty_payload():
    with pytest.raises(AudioEmptyError):
        audio.decode(b'')


def test_decode_rejects_oversized_payload(monkeypatch):
    monkeypatch.setattr(audio, 'ASR_MAX_BYTES', 10)
    with pytest.raises(AudioTooLargeError):
        audio.decode(build_wav(pcm16(range(20))))


def test_decode_rejects_audio_over_the_length_cap(monkeypatch):
    monkeypatch.setattr(audio, 'ASR_MAX_SECONDS', 0)
    with pytest.raises(AudioTooLongError):
        audio.decode(build_wav(pcm16([0] * 160)))


def test_decode_reports_missing_ffmpeg(monkeypatch):
    def explode(*args, **kwargs):
        raise FileNotFoundError('ffmpeg')

    monkeypatch.setattr(audio.subprocess, 'run', explode)
    with pytest.raises(AudioUnsupportedError):
        audio.decode(b'\x1aE\xdf\xa3 not a wav file')


def test_decode_reports_ffmpeg_failure(monkeypatch):
    class Result:
        returncode = 1
        stdout = b''
        stderr = b'Invalid data found when processing input'

    monkeypatch.setattr(audio.subprocess, 'run', lambda *args, **kwargs: Result())
    with pytest.raises(AudioUnsupportedError, match='Invalid data found'):
        audio.decode(b'\x1aE\xdf\xa3 not a wav file')


def test_duration_ms():
    assert audio.duration_ms(np.zeros(16000, dtype=np.float32)) == 1000
    assert audio.duration_ms(np.zeros(8000, dtype=np.float32)) == 500
