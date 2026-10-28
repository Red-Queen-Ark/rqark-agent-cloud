import importlib
import importlib.util
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol, cast

import numpy as np

from src.config import ASR_SAMPLE_RATE, VOICE_MAX_SECONDS, VOICE_VAD_LEAD_MS, VOICE_VAD_MIN_MS, VOICE_VAD_SILENCE_MS, VOICE_VAD_THRESHOLD

FRAME_SIZE = 480


class VadUnavailableError(Exception):
    code = 'VAD_UNAVAILABLE'

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class VadModel(Protocol):
    def predict(self, frame: np.ndarray, /, frame_size: int = FRAME_SIZE) -> Any: ...

    def reset_states(self) -> None: ...


def _load() -> VadModel:
    try:
        module = importlib.import_module('openwakeword.vad')
    except ImportError as error:
        raise VadUnavailableError('openwakeword is not installed, the vad stage cannot run') from error
    return cast(VadModel, module.VAD())


@dataclass(frozen=True)
class Segment:
    samples: np.ndarray
    duration_ms: int
    truncated: bool


def available() -> bool:
    return importlib.util.find_spec('openwakeword.vad') is not None


def _frames(count_ms: int) -> int:
    return max(0, round(count_ms * ASR_SAMPLE_RATE / 1000 / FRAME_SIZE))


class Segmenter:
    """Splits a continuous PCM stream into utterances.

    Frames that arrive before speech starts are kept in a ring buffer, so the beginning of
    the utterance is not clipped: the ring is prepended when the first voiced frame lands.
    """

    def __init__(
        self,
        threshold: float = VOICE_VAD_THRESHOLD,
        silence_ms: int = VOICE_VAD_SILENCE_MS,
        lead_ms: int = VOICE_VAD_LEAD_MS,
        min_ms: int = VOICE_VAD_MIN_MS,
        max_seconds: int = VOICE_MAX_SECONDS,
        load: Any = _load,
    ):
        self.model = load()
        self.threshold = threshold
        self.silence_frames = max(1, _frames(silence_ms))
        self.min_frames = max(1, _frames(min_ms))
        self.max_frames = max(1, round(max_seconds * ASR_SAMPLE_RATE / FRAME_SIZE))
        self.ring: deque[np.ndarray] = deque(maxlen=max(1, _frames(lead_ms)))
        self._tail = np.zeros(0, dtype=np.int16)
        self._speech: list[np.ndarray] = []
        self._quiet = 0
        self._voiced = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def probability(self, frame: np.ndarray) -> float:
        scores = self.model.predict(frame, frame_size=frame.size)
        values = np.asarray(scores, dtype=np.float32).ravel()
        return float(values[-1]) if values.size else 0.0

    def push(self, samples: np.ndarray) -> list[Segment]:
        if samples.size == 0:
            return []
        buffer = np.concatenate((self._tail, samples.astype(np.int16, copy=False)))
        whole = buffer.size - buffer.size % FRAME_SIZE
        self._tail = buffer[whole:].copy()
        done: list[Segment] = []
        for start in range(0, whole, FRAME_SIZE):
            segment = self._step(buffer[start : start + FRAME_SIZE].copy())
            if segment is not None:
                done.append(segment)
        return done

    def _step(self, frame: np.ndarray) -> Segment | None:
        voiced = self.probability(frame) >= self.threshold
        if not self._active:
            if not voiced:
                self.ring.append(frame)
                return None
            self._active = True
            self._speech = [*self.ring, frame]
            self.ring.clear()
            self._voiced = 1
            self._quiet = 0
            return None
        self._speech.append(frame)
        if voiced:
            self._voiced += 1
            self._quiet = 0
        else:
            self._quiet += 1
        if self._quiet >= self.silence_frames:
            return self._finish()
        if len(self._speech) >= self.max_frames:
            return self._finish(truncated=True)
        return None

    def _finish(self, truncated: bool = False) -> Segment | None:
        frames = self._speech
        voiced = self._voiced
        self.reset()
        if voiced < self.min_frames:
            return None
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        return Segment(samples=audio, duration_ms=int(round(audio.size * 1000 / ASR_SAMPLE_RATE)), truncated=truncated)

    def flush(self) -> Segment | None:
        if not self._active:
            self.reset()
            return None
        return self._finish()

    def reset(self) -> None:
        self._speech = []
        self._quiet = 0
        self._voiced = 0
        self._active = False
        self.ring.clear()
        self._tail = np.zeros(0, dtype=np.int16)
        self.model.reset_states()
