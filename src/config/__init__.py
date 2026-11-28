import os

from rqark_sandbox.config import WORKSPACE_MAX_BYTES as WORKSPACE_MAX_BYTES
from rqark_sandbox.config import WORKSPACE_SHELL_MAX_BYTES as WORKSPACE_SHELL_MAX_BYTES


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() == 'true'


def _int_env(name: str, default: str) -> int:
    return int(os.getenv(name, default))


def _float_env(name: str, default: str) -> float:
    return float(os.getenv(name, default))


# App
API_PREFIX = os.getenv('API_PREFIX', '/api')
DOCS_URL = os.getenv('DOCS_URL', '/api/docs')
REDOC_URL = os.getenv('REDOC_URL', '/api/redoc')
OPENAPI_URL = os.getenv('OPENAPI_URL', '/api/openapi.json')

# CORS
CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*').split(',')
CORS_ALLOW_CREDENTIALS = _bool_env('CORS_ALLOW_CREDENTIALS', 'true')

# Redis
REDIS_SERVER = os.getenv('REDIS_SERVER', 'localhost')
REDIS_PORT = os.getenv('REDIS_PORT', '6379')
REDIS_URL = os.getenv('REDIS_URL') or f'redis://{REDIS_SERVER}:{REDIS_PORT}/0'

# Data cloud
DATA_CLOUD_URL = os.getenv('DATA_CLOUD_URL', 'http://localhost:7302')
DATA_CLOUD_TIMEOUT = _float_env('DATA_CLOUD_TIMEOUT', '30')

# Chat
CHAT_IDLE_TIMEOUT = _float_env('CHAT_IDLE_TIMEOUT', '900')
CHAT_IDLE_CHECK = _float_env('CHAT_IDLE_CHECK', '5')
# How many provider failures in a row, with no content between them, end a turn.
# A single provider that keeps refusing answers once a second, so the idle
# timeout above would take 15 minutes to report something the user could see in
# seconds. Content progress resets the streak, so a flaky model that recovers is
# not cut off.
CHAT_RETRY_LIMIT = _int_env('CHAT_RETRY_LIMIT', '5')
CHAT_SHUTDOWN_TIMEOUT = _float_env('CHAT_SHUTDOWN_TIMEOUT', '30')

# Images
IMAGE_MAX_BYTES = _int_env('IMAGE_MAX_BYTES', str(5 * 1024 * 1024))
IMAGE_MAX_PER_MESSAGE = _int_env('IMAGE_MAX_PER_MESSAGE', '4')

# ASR
ASR_ENGINE = os.getenv('ASR_ENGINE', '')
ASR_MODEL = os.getenv('ASR_MODEL', '')
ASR_MODEL_DIR = os.getenv('ASR_MODEL_DIR', '')
ASR_LANGUAGE = os.getenv('ASR_LANGUAGE', 'Cantonese')
ASR_SAMPLE_RATE = _int_env('ASR_SAMPLE_RATE', '16000')
ASR_MAX_BYTES = _int_env('ASR_MAX_BYTES', str(25 * 1024 * 1024))
ASR_MAX_SECONDS = _int_env('ASR_MAX_SECONDS', '60')
ASR_TIMEOUT = _float_env('ASR_TIMEOUT', '60')
ASR_QUEUE_TIMEOUT = _float_env('ASR_QUEUE_TIMEOUT', '30')
ASR_QUEUE_DEPTH = _int_env('ASR_QUEUE_DEPTH', '2')
ASR_CONCURRENCY = _int_env('ASR_CONCURRENCY', '1')
ASR_OPENCC = _bool_env('ASR_OPENCC', 'true')
ASR_OPENCC_CONFIG = os.getenv('ASR_OPENCC_CONFIG', 's2hk')

# Voice
SPEECH_ENABLED = _bool_env('SPEECH_ENABLED', 'false')
VOICE_MAX_BYTES = _int_env('VOICE_MAX_BYTES', str(ASR_MAX_BYTES))
VOICE_MAX_SECONDS = _int_env('VOICE_MAX_SECONDS', str(ASR_MAX_SECONDS))
VOICE_MAX_FRAME_BYTES = _int_env('VOICE_MAX_FRAME_BYTES', str(64 * 1024))
VOICE_PREROLL_MS = _int_env('VOICE_PREROLL_MS', '400')
VOICE_WAKE_MODEL = os.getenv('VOICE_WAKE_MODEL', '')
VOICE_WAKE_THRESHOLD = _float_env('VOICE_WAKE_THRESHOLD', '0.5')
VOICE_WAKE_COOLDOWN = _float_env('VOICE_WAKE_COOLDOWN', '1.5')
VOICE_WAKE_WINDOW = _float_env('VOICE_WAKE_WINDOW', '10')
VOICE_WAKE_GUARD_MS = _int_env('VOICE_WAKE_GUARD_MS', '400')
VOICE_VAD_THRESHOLD = _float_env('VOICE_VAD_THRESHOLD', '0.5')
VOICE_VAD_SILENCE_MS = _int_env('VOICE_VAD_SILENCE_MS', '800')
VOICE_VAD_LEAD_MS = _int_env('VOICE_VAD_LEAD_MS', '250')
VOICE_VAD_MIN_MS = _int_env('VOICE_VAD_MIN_MS', '250')
VOICE_MAX_CONNECTIONS = _int_env('VOICE_MAX_CONNECTIONS', '8')
VOICE_MAX_PER_USER = _int_env('VOICE_MAX_PER_USER', '1')
VOICE_IDLE_TIMEOUT = _float_env('VOICE_IDLE_TIMEOUT', '300')
VOICE_REDIS = _bool_env('VOICE_REDIS', 'true')
VOICE_LEASE_TTL = _int_env('VOICE_LEASE_TTL', '360')

# Sandbox
SANDBOX_ENABLED = _bool_env('SANDBOX_ENABLED', 'true')
SANDBOX_IMAGE = os.getenv('SANDBOX_IMAGE', 'ubuntu:24.04')
WORKSPACE_START_TIMEOUT = _float_env('WORKSPACE_START_TIMEOUT', '30')
