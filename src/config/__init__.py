import os


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _int_env(name: str, default: str) -> int:
    return int(os.getenv(name, default))

# App
API_PREFIX = os.getenv("API_PREFIX", "/api")
DOCS_URL = os.getenv("DOCS_URL", "/api/docs")
REDOC_URL = os.getenv("REDOC_URL", "/api/redoc")
OPENAPI_URL = os.getenv("OPENAPI_URL", "/api/openapi.json")

# CORS
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
CORS_ALLOW_CREDENTIALS = _bool_env("CORS_ALLOW_CREDENTIALS", "true")

# Redis
REDIS_SERVER = os.getenv("REDIS_SERVER", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = os.getenv("REDIS_URL") or f"redis://{REDIS_SERVER}:{REDIS_PORT}/0"

# Data cloud
DATA_CLOUD_URL = os.getenv('DATA_CLOUD_URL', 'http://localhost:7302')
DATA_CLOUD_TIMEOUT = float(os.getenv('DATA_CLOUD_TIMEOUT', '30'))

# Sandbox
SANDBOX_IMAGE = os.getenv('SANDBOX_IMAGE', 'rqark-sandbox:latest')
