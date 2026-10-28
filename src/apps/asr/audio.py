import struct
import subprocess
from dataclasses import dataclass

import numpy as np

from src.apps.asr.errors import AudioEmptyError, AudioTooLargeError, AudioTooLongError, AudioUnsupportedError
from src.config import ASR_MAX_BYTES, ASR_MAX_SECONDS, ASR_SAMPLE_RATE

FFMPEG_TIMEOUT = 60.0
_PCM = 1
_FLOAT = 3
_EXTENSIBLE = 0xFFFE


@dataclass(frozen=True)
class WaveFormat:
    tag: int
    channels: int
    rate: int
    bits: int


def decode(data: bytes) -> np.ndarray:
    """Decode an uploaded audio blob into mono float32 samples at ASR_SAMPLE_RATE."""
    if not data:
        raise AudioEmptyError('the audio payload is empty')
    if len(data) > ASR_MAX_BYTES:
        raise AudioTooLargeError(f'audio is too large: {len(data)} bytes')
    samples = _decode_wav(data)
    if samples is None:
        samples = _decode_ffmpeg(data)
    if samples.size == 0:
        raise AudioEmptyError('the audio payload holds no samples')
    if not np.all(np.isfinite(samples)):
        raise AudioUnsupportedError('the audio payload holds non finite samples')
    if samples.size / ASR_SAMPLE_RATE > ASR_MAX_SECONDS:
        raise AudioTooLongError(f'audio is longer than {ASR_MAX_SECONDS:g}s')
    return samples


def duration_ms(samples: np.ndarray) -> int:
    return int(round(samples.size * 1000 / ASR_SAMPLE_RATE))


def _decode_wav(data: bytes) -> np.ndarray | None:
    if len(data) < 12 or data[0:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    position = 12
    wave_format: WaveFormat | None = None
    while position + 8 <= len(data):
        chunk = data[position : position + 4]
        size = struct.unpack('<I', data[position + 4 : position + 8])[0]
        body = position + 8
        available = max(0, len(data) - body)
        size = min(size, available)
        if chunk == b'fmt ':
            wave_format = _parse_format(data[body : body + size])
        elif chunk == b'data':
            if wave_format is None:
                raise AudioUnsupportedError('the wav file has no format chunk')
            return _decode_pcm(data[body : body + size], wave_format)
        position = body + size + (size & 1)
    return None


def _parse_format(body: bytes) -> WaveFormat:
    if len(body) < 16:
        raise AudioUnsupportedError('the wav format chunk is truncated')
    tag, channels, rate, _byte_rate, _block_align, bits = struct.unpack('<HHIIHH', body[:16])
    if tag == _EXTENSIBLE and len(body) >= 26:
        tag = struct.unpack('<H', body[24:26])[0]
    if channels < 1 or rate < 1:
        raise AudioUnsupportedError('the wav format chunk is invalid')
    return WaveFormat(tag=tag, channels=channels, rate=rate, bits=bits)


def _decode_pcm(raw: bytes, wave_format: WaveFormat) -> np.ndarray:
    if wave_format.tag == _FLOAT and wave_format.bits == 32:
        samples = np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype='<f4').astype(np.float32)
    elif wave_format.tag == _PCM:
        samples = _decode_integer_pcm(raw, wave_format.bits)
    else:
        raise AudioUnsupportedError(f'unsupported wav encoding tag={wave_format.tag} bits={wave_format.bits}')
    if wave_format.channels > 1:
        usable = samples.size - samples.size % wave_format.channels
        samples = samples[:usable].reshape(-1, wave_format.channels).mean(axis=1)
    return _resample(samples.astype(np.float32), wave_format.rate)


def _decode_integer_pcm(raw: bytes, bits: int) -> np.ndarray:
    if bits == 8:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if bits == 16:
        return np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype='<i2').astype(np.float32) / 32768.0
    if bits == 24:
        usable = len(raw) - len(raw) % 3
        blocks = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        packed = blocks[:, 0] | (blocks[:, 1] << 8) | (blocks[:, 2] << 16)
        signed = np.where(packed & 0x800000, packed - 0x1000000, packed)
        return signed.astype(np.float32) / 8388608.0
    if bits == 32:
        return np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype='<i4').astype(np.float32) / 2147483648.0
    raise AudioUnsupportedError(f'unsupported wav sample width bits={bits}')


def _resample(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == ASR_SAMPLE_RATE or samples.size == 0:
        return samples
    if rate > ASR_SAMPLE_RATE:
        window = max(1, round(rate / ASR_SAMPLE_RATE))
        if window > 1:
            samples = np.convolve(samples, np.ones(window, dtype=np.float32) / window, mode='same').astype(np.float32)
    count = int(round(samples.size * ASR_SAMPLE_RATE / rate))
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    span = (samples.size - 1) / rate
    source = np.linspace(0.0, span, num=samples.size)
    target = np.linspace(0.0, span, num=count)
    return np.interp(target, source, samples).astype(np.float32)


def _decode_ffmpeg(data: bytes) -> np.ndarray:
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0', '-t', f'{ASR_MAX_SECONDS + 1:g}', '-f', 'f32le', '-acodec', 'pcm_f32le', '-ac', '1', '-ar', str(ASR_SAMPLE_RATE), 'pipe:1']
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False)
    except FileNotFoundError as error:
        raise AudioUnsupportedError('ffmpeg is not installed, only wav uploads are accepted') from error
    except subprocess.TimeoutExpired as error:
        raise AudioUnsupportedError('ffmpeg timed out while decoding the audio') from error
    if result.returncode != 0:
        detail = result.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise AudioUnsupportedError(detail[-1] if detail else 'ffmpeg could not decode the audio')
    return np.frombuffer(result.stdout, dtype='<f4').astype(np.float32)
