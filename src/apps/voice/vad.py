import importlib
import importlib.util
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol, cast

import numpy as np

from src.config import ASR_SAMPLE_RATE, VOICE_MAX_CONNECTIONS, VOICE_MAX_SECONDS, VOICE_VAD_LEAD_MS, VOICE_VAD_MIN_MS, VOICE_VAD_SILENCE_MS, VOICE_VAD_THRESHOLD

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


_spare: list[VadModel] = []
_spare_lock = threading.Lock()


def _borrow() -> VadModel:
    with _spare_lock:
        if _spare:
            return _spare.pop()
    return _load()


def _give_back(model: VadModel) -> None:
    model.reset_states()
    with _spare_lock:
        if len(_spare) < VOICE_MAX_CONNECTIONS:
            _spare.append(model)


def _frames(count_ms: int) -> int:
    return max(0, round(count_ms * ASR_SAMPLE_RATE / 1000 / FRAME_SIZE))


class Segmenter:
    """Splits a continuous PCM stream into utterances.

    Frames that arrive before speech starts are kept in a ring buffer, so the beginning of
    the utterance is not clipped: the ring is prepended when the first voiced frame lands.
    """

    def __init__(self, threshold: float = VOICE_VAD_THRESHOLD, silence_ms: int = VOICE_VAD_SILENCE_MS, lead_ms: int = VOICE_VAD_LEAD_MS, min_ms: int = VOICE_VAD_MIN_MS, max_seconds: int = VOICE_MAX_SECONDS, load: Any = None):
        self._pooled = load is None
        self.model = _borrow() if load is None else load()
        self.threshold = threshold
        self.silence_frames = max(1, _frames(silence_ms))
        self.min_frames = max(1, _frames(min_ms))
        self.max_frames = max(1, round(max_seconds * ASR_SAMPLE_RATE / FRAME_SIZE))
        self.ring: deque[np.ndarray] = deque(maxlen=max(1, _frames(lead_ms)))
        self._tail = np.zeros(0, dtype=np.int16)
        self._speech: list[np.ndarray] = []
        self._count = 0
        self._quiet = 0
        self._voiced = 0
        self._active = False
        self._pending: np.ndarray | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def speaking(self) -> bool:
        """Whether the user has actually started an utterance.

        Audio handed over by `prime()` does not count: it was said before the wake word, and
        the wake word alone must not look like a finished turn.
        """
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
            lead = [self._pending, *self.ring] if self._pending is not None else list(self.ring)
            self._pending = None
            self._speech = [*lead, frame]
            self.ring.clear()
            self._count = int(np.ceil(sum(frame.size for frame in self._speech) / FRAME_SIZE))
            self._voiced = 1
            self._quiet = 0
            return None
        self._speech.append(frame)
        self._count += 1
        if voiced:
            self._voiced += 1
            self._quiet = 0
        else:
            self._quiet += 1
        if self._quiet >= self.silence_frames:
            return self._finish()
        if self._count >= self.max_frames:
            return self._finish(truncated=True)
        return None

    def prime(self, samples: np.ndarray) -> None:
        """Keep the audio spoken before the wake word, without opening an utterance.

        The wake word fires a moment after the user started talking, so the audio in front of
        the hit is real speech the VAD never scored. Holding it until the first voiced frame
        keeps that speech in the utterance.

        It must not open the utterance on its own: the wake word is proof that someone spoke,
        so opening here would let the silence timer close a turn that contains no request at
        all — which answers the wake word itself.
        """
        if samples.size == 0:
            return
        self._pending = samples.astype(np.int16, copy=False)
        self._speech = []
        self._count = 0
        self._voiced = 0
        self._quiet = 0
        self._active = False
        self.ring.clear()

    def extend_pending(self, samples: np.ndarray) -> None:
        """Append audio to the lead-in without letting it open an utterance.

        Used for the moment right after the wake word fires: the tail of the word is still
        arriving and the VAD would score it as the start of a request, which turns "hey
        assistant" followed by silence into an empty turn. Keeping it out of the voice
        decision but still inside the audio means a command spoken straight away is not
        clipped either.
        """
        if samples.size == 0:
            return
        incoming = samples.astype(np.int16, copy=False)
        self._pending = incoming if self._pending is None else np.concatenate((self._pending, incoming))

    def _finish(self, truncated: bool = False) -> Segment | None:
        frames = self._speech
        voiced = self._voiced
        tail = self._tail
        self.reset()
        self._tail = tail
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
        self._count = 0
        self._quiet = 0
        self._voiced = 0
        self._active = False
        self._pending = None
        self.ring.clear()
        self._tail = np.zeros(0, dtype=np.int16)
        self.model.reset_states()

    def close(self) -> None:
        if self._pooled:
            _give_back(self.model)
