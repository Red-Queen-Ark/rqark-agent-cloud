import importlib
import importlib.util
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np

from src.config import VOICE_WAKE_MODEL, VOICE_WAKE_THRESHOLD

FRAME_SIZE = 1280


class WakeUnavailableError(Exception):
    code = 'WAKE_UNAVAILABLE'

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class WakeModel(Protocol):
    def predict(self, frame: np.ndarray, /) -> dict[str, float]: ...

    def reset(self) -> None: ...


def _load(model_path: str) -> WakeModel:
    try:
        module = importlib.import_module('openwakeword.model')
    except ImportError as error:
        raise WakeUnavailableError('openwakeword is not installed, the wake word stage cannot run') from error
    return cast(WakeModel, module.Model(wakeword_model_paths=[model_path]))


def available() -> bool:
    return importlib.util.find_spec('openwakeword.model') is not None


def configured() -> bool:
    return bool(VOICE_WAKE_MODEL) and available()


class Detector:
    """One connection's wake word stream.

    Frames arrive in whatever size the browser picked, so the tail of a short chunk is
    carried into the next one: the model only scores whole 80ms frames.
    """

    def __init__(self, model_path: str = VOICE_WAKE_MODEL, threshold: float = VOICE_WAKE_THRESHOLD, load: Any = _load):
        if not model_path:
            raise WakeUnavailableError('VOICE_WAKE_MODEL is not set, the wake word stage cannot run')
        self.model = load(model_path)
        self.threshold = threshold
        self._tail = np.zeros(0, dtype=np.int16)

    def push(self, samples: np.ndarray) -> float | None:
        if samples.size == 0:
            return None
        buffer = np.concatenate((self._tail, samples.astype(np.int16, copy=False)))
        whole = buffer.size - buffer.size % FRAME_SIZE
        self._tail = buffer[whole:].copy()
        score: float | None = None
        for start in range(0, whole, FRAME_SIZE):
            prediction = self.model.predict(buffer[start : start + FRAME_SIZE].copy())
            current = max(prediction.values(), default=0.0)
            score = current if score is None else max(score, current)
        return score

    def fired(self, score: float | None) -> bool:
        if score is None or score < self.threshold:
            return False
        self.reset()
        return True

    def reset(self) -> None:
        self._tail = np.zeros(0, dtype=np.int16)
        self.model.reset()


def for_connection() -> Detector | None:
    if not configured():
        return None
    try:
        return Detector()
    except WakeUnavailableError:
        return None


def model_name() -> str:
    return Path(VOICE_WAKE_MODEL).stem if VOICE_WAKE_MODEL else ''
