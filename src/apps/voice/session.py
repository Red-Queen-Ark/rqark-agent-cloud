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


class PcmStream:
    """Turns binary frames into samples, keeping a stray half sample for the next frame.

    A frame with an odd byte count would shift every later sample by one byte and turn the
    rest of the stream into noise, so the leftover is carried instead of dropped.
    """

    def __init__(self) -> None:
        self._carried = b''
        self.total = 0

    def feed(self, chunk: bytes) -> np.ndarray:
        self.total += len(chunk)
        data = self._carried + chunk if self._carried else chunk
        usable = len(data) - len(data) % BYTES_PER_SAMPLE
        self._carried = data[usable:]
        if usable == 0:
            return np.zeros(0, dtype=np.int16)
        return np.frombuffer(data[:usable], dtype='<i2').copy()

    def reset(self) -> None:
        self._carried = b''
