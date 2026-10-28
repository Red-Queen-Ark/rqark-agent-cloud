import importlib
import platform
import sys
from typing import Any, Protocol

import numpy as np

from src.apps.asr.errors import AsrUnavailableError
from src.config import ASR_LANGUAGE, ASR_MODEL, ASR_MODEL_DIR

QWEN3_MODEL = 'mlx-community/Qwen3-ASR-0.6B-8bit'
WHISPER_MODEL = 'large-v3'
WHISPER_BEAM_SIZE = 5
WHISPER_PATIENCE = 1.0
WHISPER_SILENCE_THRESHOLD = 2.0


class AsrEngine(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray) -> str: ...


def _module(path: str, extra: str) -> Any:
    try:
        return importlib.import_module(path)
    except ImportError as error:
        raise AsrUnavailableError(f'{path} is not installed, the {extra} engine cannot run in this image') from error


class Qwen3Engine:
    """Qwen3-ASR through mlx-audio, Apple Silicon only.

    Cantonese is a first class language here, which Whisper cannot match: Whisper's yue
    support rewrites spoken Cantonese into written Mandarin and no initial_prompt undoes
    that. Measured on an M series Mac with 0.6B-8bit: 2.7s to load once, 0.36s for 30s
    of audio.
    """

    name = 'qwen3'

    def __init__(self, model: str, language: str | None, hotwords: str | None = None):
        self.model_name = model or QWEN3_MODEL
        self.language = language
        self.system_prompt = hotwords or None
        self.model = _module('mlx_audio.stt', 'asr-mlx').load(self.model_name)

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ''
        result = self.model.generate(audio, language=self.language, system_prompt=self.system_prompt, temperature=0.0)
        return str(result.text).strip()


class FasterWhisperEngine:
    """faster-whisper through ctranslate2, the CPU fallback."""

    name = 'faster-whisper'

    def __init__(self, model: str, language: str | None, hotwords: str | None = None):
        self.model_name = model or WHISPER_MODEL
        self.language = language
        self.hotwords = hotwords
        module = _module('faster_whisper', 'asr-whisper')
        self.model = module.WhisperModel(self.model_name, device='auto', compute_type='int8', download_root=ASR_MODEL_DIR or None)

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ''
        segments, _info = self.model.transcribe(
            audio, language=self.language, beam_size=WHISPER_BEAM_SIZE, patience=WHISPER_PATIENCE, condition_on_previous_text=False, hotwords=self.hotwords, hallucination_silence_threshold=WHISPER_SILENCE_THRESHOLD
        )
        return ''.join(segment.text for segment in segments).strip()


def resolve(name: str) -> str:
    if name:
        return name
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        return 'qwen3'
    return 'faster-whisper'


def build_engine(name: str = '', model: str = '', language: str | None = ASR_LANGUAGE, hotwords: str | None = None) -> AsrEngine:
    engine = resolve(name)
    chosen = model or ASR_MODEL
    match engine:
        case 'qwen3':
            return Qwen3Engine(chosen, language, hotwords)
        case 'faster-whisper':
            return FasterWhisperEngine(chosen, language, hotwords)
        case _:
            raise AsrUnavailableError(f'unknown asr engine: {name!r}, expected qwen3 or faster-whisper')
