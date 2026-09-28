from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import CORS_ALLOW_CREDENTIALS, CORS_ORIGINS


def setup_cors(app: FastAPI) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=CORS_ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
