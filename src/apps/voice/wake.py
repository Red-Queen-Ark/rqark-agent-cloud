import importlib
import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, cast

import numpy as np

from src.config import ASR_SAMPLE_RATE, VOICE_MAX_CONNECTIONS, VOICE_WAKE_COOLDOWN, VOICE_WAKE_MODEL, VOICE_WAKE_THRESHOLD

FRAME_SIZE = 1280
MODELS_DIR = Path(__file__).parent / 'models'
BUNDLED = 'wednesday'
NO_SAMPLES = np.zeros(0, dtype=np.int16)


class WakeUnavailableError(Exception):
    code = 'WAKE_UNAVAILABLE'

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class WakeModel(Protocol):
    def predict(self, frame: np.ndarray, /) -> dict[str, float]: ...

    def reset(self) -> None: ...


def _load(paths: Sequence[str]) -> WakeModel:
    try:
        module = importlib.import_module('openwakeword.model')
    except ImportError as error:
        raise WakeUnavailableError('openwakeword is not installed, the wake word stage cannot run') from error
    return cast(WakeModel, module.Model(wakeword_model_paths=list(paths)))


def available() -> bool:
    return importlib.util.find_spec('openwakeword.model') is not None


def bundled(name: str = BUNDLED) -> Path:
    return MODELS_DIR / f'{name}.onnx'


def _resolve(entry: str) -> str:
    """A bare name means "the model that ships with the package".

    Deployments set `wednesday,diana` instead of a path, so the same env works from the
    repo, from an image, and from a container with a different WORKDIR.
    """
    if '/' in entry or '\\' in entry:
        return entry
    name = entry.removesuffix('.onnx')
    candidate = bundled(name)
    return str(candidate) if candidate.exists() else entry


def models() -> list[str]:
    """The configured models, or the one shipped in the package.

    VOICE_WAKE_MODEL wins and may be comma separated, either as bundled names or as paths,
    so a deployment can point at several models without a rebuild. An empty setting is not
    "off": the bundled model is used unless it is gone.
    """
    if VOICE_WAKE_MODEL:
        return [_resolve(part.strip()) for part in VOICE_WAKE_MODEL.split(',') if part.strip()]
    candidate = bundled()
    return [str(candidate)] if candidate.exists() else []


def configured() -> bool:
    return bool(models()) and available()


def model_names() -> list[str]:
    return [Path(path).stem for path in models()]


class Detector:
    """One connection's wake word stream.

    Every configured model is scored on the same frames: one openWakeWord instance handles
    the whole list and shares the feature extractor. Frames arrive in whatever size the
    browser picked, so the tail of a short chunk is carried into the next one: the model
    only scores whole 80ms frames. After a model fires its threshold is ignored for a
    moment, otherwise the same word scores again on the next frame and the client sees a
    burst of wake events for one utterance.
    """

    def __init__(
        self,
        paths: Sequence[str] | None = None,
        threshold: float = VOICE_WAKE_THRESHOLD,
        cooldown: float = VOICE_WAKE_COOLDOWN,
        load: Any = None,
    ):
        chosen = list(paths) if paths else models()
        if not chosen:
            raise WakeUnavailableError('no wake word model is configured or bundled')
        self._key = tuple(chosen)
        self._pooled = load is None
        self.model = _borrow(chosen) if load is None else load(chosen)
        self.names = [Path(path).stem for path in chosen]
        self.threshold = threshold
        self.cooldown_frames = max(0, round(cooldown * ASR_SAMPLE_RATE / FRAME_SIZE))
        self._mute: dict[str, int] = {}
        self._tail = NO_SAMPLES

    def push(self, samples: np.ndarray) -> Detection:
        if samples.size == 0:
            return Detection(None, None, False, NO_SAMPLES)
        buffer = np.concatenate((self._tail, samples.astype(np.int16, copy=False)))
        whole = buffer.size - buffer.size % FRAME_SIZE
        self._tail = buffer[whole:].copy()
        score: float | None = None
        best: str | None = None
        for start in range(0, whole, FRAME_SIZE):
            prediction = {name: float(value) for name, value in self.model.predict(buffer[start : start + FRAME_SIZE].copy()).items()}
            hottest = max(prediction, key=lambda name: prediction[name], default=None)
            current = prediction[hottest] if hottest is not None else 0.0
            if score is None or current > score:
                score = current
                best = hottest
            ready = {name: value for name, value in prediction.items() if value >= self.threshold and self._mute.get(name, 0) == 0}
            self._mute = {name: frames - 1 for name, frames in self._mute.items() if frames > 1}
            if not ready:
                continue
            fired = max(ready, key=lambda name: ready[name])
            self.reset()
            self._mute[fired] = self.cooldown_frames
            return Detection(ready[fired], fired, True, buffer[start + FRAME_SIZE :].copy())
        return Detection(score, best, False, NO_SAMPLES)

    def reset(self) -> None:
        self._mute = {}
        self._tail = NO_SAMPLES
        self.model.reset()

    def close(self) -> None:
        if self._pooled:
            _give_back(self._key, self.model)


_spare: dict[tuple[str, ...], list[WakeModel]] = {}
_spare_lock = threading.Lock()


def _borrow(paths: Sequence[str]) -> WakeModel:
    """A loaded model, reused across connections when this process still has one.

    Loading costs about half a second and the streaming state is reset on the way in and
    out, so a warm model is as good as a fresh one.
    """
    key = tuple(paths)
    with _spare_lock:
        ready = _spare.get(key)
        if ready:
            return ready.pop()
    return _load(paths)


def _give_back(key: tuple[str, ...], model: WakeModel) -> None:
    model.reset()
    with _spare_lock:
        ready = _spare.setdefault(key, [])
        if len(ready) < VOICE_MAX_CONNECTIONS:
            ready.append(model)


@dataclass(frozen=True)
class Detection:
    """What one chunk of audio did to the wake stream.

    `name` is the model that scored highest, `rest` is the audio after the firing frame:
    the command usually starts in the same chunk, so it is handed back instead of dropped.
    """

    score: float | None
    name: str | None
    woke: bool
    rest: np.ndarray


def for_connection() -> Detector | None:
    if not available():
        return None
    try:
        return Detector()
    except WakeUnavailableError:
        return None
