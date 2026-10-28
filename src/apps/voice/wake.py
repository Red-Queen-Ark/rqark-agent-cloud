import importlib
import importlib.util
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np

from src.config import VOICE_WAKE_MODEL, VOICE_WAKE_THRESHOLD

FRAME_SIZE = 1280
MODELS_DIR = Path(__file__).parent / 'models'
BUNDLED = 'wednesday'


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


def bundled(name: str = BUNDLED) -> Path:
    return MODELS_DIR / f'{name}.onnx'


def model_path() -> str:
    """The configured model, or the one shipped in the package.

    A path in VOICE_WAKE_MODEL wins, so a deployment can point at its own model without a
    rebuild. An empty setting is not "off": the bundled model is used unless it is gone.
    """
    if VOICE_WAKE_MODEL:
        return VOICE_WAKE_MODEL
    candidate = bundled()
    return str(candidate) if candidate.exists() else ''


def configured() -> bool:
    return bool(model_path()) and available()


def model_name() -> str:
    path = model_path()
    return Path(path).stem if path else ''


class Detector:
    """One connection's wake word stream.

    Frames arrive in whatever size the browser picked, so the tail of a short chunk is
    carried into the next one: the model only scores whole 80ms frames.
    """

    def __init__(self, path: str = '', threshold: float = VOICE_WAKE_THRESHOLD, load: Any = _load):
        chosen = path or model_path()
        if not chosen:
            raise WakeUnavailableError('no wake word model is configured or bundled')
        self.model = load(chosen)
        self.name = Path(chosen).stem
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
            current = float(max(prediction.values(), default=0.0))
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
    if not available():
        return None
    try:
        return Detector()
    except WakeUnavailableError:
        return None
