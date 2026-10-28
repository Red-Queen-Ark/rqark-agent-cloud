from typing import Any

from src.config import ASR_SAMPLE_RATE, VOICE_MAX_BYTES, VOICE_MAX_SECONDS


def ready_event(concurrency: int, wake_word: bool, vad: bool) -> dict[str, Any]:
    return {'type': 'ready', 'sample_rate': ASR_SAMPLE_RATE, 'format': 'pcm_s16le', 'channels': 1, 'max_seconds': VOICE_MAX_SECONDS, 'max_bytes': VOICE_MAX_BYTES, 'concurrency': concurrency, 'wake_word': wake_word, 'vad': vad}


def started_event() -> dict[str, str]:
    return {'type': 'started'}


def final_event(text: str, duration_ms: int, elapsed_ms: int, truncated: bool) -> dict[str, Any]:
    return {'type': 'final', 'text': text, 'duration_ms': duration_ms, 'elapsed_ms': elapsed_ms, 'truncated': truncated}


def wake_event(score: float) -> dict[str, Any]:
    return {'type': 'wake', 'score': round(score, 3)}


def error_event(code: str, message: str) -> dict[str, Any]:
    return {'type': 'error', 'error': {'code': code, 'message': message}}
