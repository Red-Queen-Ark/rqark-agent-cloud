#!/bin/sh
set -e

exec .venv/bin/uvicorn src.app:create_fastapi_app \
    --host 0.0.0.0 \
    --port 8000 \
    --factory \
    --workers 4
