import os


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() == 'true'


def _int_env(name: str, default: str) -> int:
    return int(os.getenv(name, default))


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
DATA_CLOUD_TIMEOUT = float(os.getenv('DATA_CLOUD_TIMEOUT', '30'))

# Chat
CHAT_IDLE_TIMEOUT = float(os.getenv('CHAT_IDLE_TIMEOUT', '900'))
CHAT_IDLE_CHECK = float(os.getenv('CHAT_IDLE_CHECK', '5'))

# Images
IMAGE_MAX_BYTES = _int_env('IMAGE_MAX_BYTES', str(5 * 1024 * 1024))
IMAGE_MAX_PER_MESSAGE = _int_env('IMAGE_MAX_PER_MESSAGE', '4')

# Tasks
INTERNAL_TOKEN = os.getenv('INTERNAL_TOKEN', '0a3be1ef5e090053bf78701be8c0fefdc955e5e878d26eed3ed1e26a0f5e9080')
TASK_RUN_TIMEOUT = float(os.getenv('TASK_RUN_TIMEOUT', '600'))
TASKS_ENABLED = _bool_env('TASKS_ENABLED', 'true')
TASK_TICK_SECONDS = float(os.getenv('TASK_TICK_SECONDS', '30'))
TASK_CONCURRENCY = _int_env('TASK_CONCURRENCY', '1')
TASK_STREAM_MAX_CHARS = _int_env('TASK_STREAM_MAX_CHARS', str(200_000))
TASK_LEADER_TTL = float(os.getenv('TASK_LEADER_TTL', '60'))

# ASR
ASR_ENGINE = os.getenv('ASR_ENGINE', '')
ASR_MODEL = os.getenv('ASR_MODEL', '')
ASR_MODEL_DIR = os.getenv('ASR_MODEL_DIR', '')
ASR_LANGUAGE = os.getenv('ASR_LANGUAGE', 'Cantonese')
ASR_SAMPLE_RATE = _int_env('ASR_SAMPLE_RATE', '16000')
ASR_MAX_BYTES = _int_env('ASR_MAX_BYTES', str(25 * 1024 * 1024))
ASR_MAX_SECONDS = _int_env('ASR_MAX_SECONDS', '60')
ASR_TIMEOUT = float(os.getenv('ASR_TIMEOUT', '60'))
ASR_QUEUE_TIMEOUT = float(os.getenv('ASR_QUEUE_TIMEOUT', '30'))
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
VOICE_WAKE_THRESHOLD = float(os.getenv('VOICE_WAKE_THRESHOLD', '0.5'))
VOICE_WAKE_COOLDOWN = float(os.getenv('VOICE_WAKE_COOLDOWN', '1.5'))
VOICE_WAKE_WINDOW = float(os.getenv('VOICE_WAKE_WINDOW', '10'))
VOICE_WAKE_GUARD_MS = _int_env('VOICE_WAKE_GUARD_MS', '400')
VOICE_VAD_THRESHOLD = float(os.getenv('VOICE_VAD_THRESHOLD', '0.5'))
VOICE_VAD_SILENCE_MS = _int_env('VOICE_VAD_SILENCE_MS', '800')
VOICE_VAD_LEAD_MS = _int_env('VOICE_VAD_LEAD_MS', '250')
VOICE_VAD_MIN_MS = _int_env('VOICE_VAD_MIN_MS', '250')
VOICE_MAX_CONNECTIONS = _int_env('VOICE_MAX_CONNECTIONS', '8')
VOICE_MAX_PER_USER = _int_env('VOICE_MAX_PER_USER', '1')
VOICE_IDLE_TIMEOUT = float(os.getenv('VOICE_IDLE_TIMEOUT', '300'))
VOICE_REDIS = _bool_env('VOICE_REDIS', 'true')
VOICE_LEASE_TTL = _int_env('VOICE_LEASE_TTL', '360')

# Sandbox
SANDBOX_ENABLED = _bool_env('SANDBOX_ENABLED', 'true')
SANDBOX_IMAGE = os.getenv('SANDBOX_IMAGE', 'ubuntu:24.04')
WORKSPACE_MAX_BYTES = _int_env('WORKSPACE_MAX_BYTES', str(100 * 1024 * 1024))
WORKSPACE_START_TIMEOUT = float(os.getenv('WORKSPACE_START_TIMEOUT', '30'))
WORKSPACE_SHELL_MAX_BYTES = _int_env('WORKSPACE_SHELL_MAX_BYTES', str(8 * 1024 * 1024))
