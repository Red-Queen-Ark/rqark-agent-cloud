from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from src.apps import routers as app_routers
from src.apps.chat import helper as chat_helper
from src.apps.chat import runner as chat_runner
from src.config import API_PREFIX, CHAT_SHUTDOWN_TIMEOUT, DOCS_URL, OPENAPI_URL, REDOC_URL
from src.middleware import setup_cors, setup_exception_handlers


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await chat_runner.shutdown()
        await chat_helper.drain_writes(CHAT_SHUTDOWN_TIMEOUT)


def create_fastapi_app():
    app = FastAPI(docs_url=DOCS_URL, redoc_url=REDOC_URL, openapi_url=OPENAPI_URL, lifespan=lifespan)
    api = APIRouter(prefix=API_PREFIX)

    setup_cors(app)
    setup_exception_handlers(app)

    for router in app_routers():
        api.include_router(router)

    app.include_router(api)
    return app
