import numpy as np

from src.config import ASR_SAMPLE_RATE

BYTES_PER_SAMPLE = 2


def samples_from_bytes(data: bytes) -> np.ndarray:
    usable = len(data) - len(data) % BYTES_PER_SAMPLE
    return np.frombuffer(data[:usable], dtype='<i2').astype(np.float32) / 32768.0


def int16_from_bytes(data: bytes) -> np.ndarray:
    usable = len(data) - len(data) % BYTES_PER_SAMPLE
    return np.frombuffer(data[:usable], dtype='<i2').copy()


def duration_ms(byte_count: int) -> int:
    return int(round(byte_count / BYTES_PER_SAMPLE * 1000 / ASR_SAMPLE_RATE))
