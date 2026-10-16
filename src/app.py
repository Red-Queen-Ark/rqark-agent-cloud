from fastapi import APIRouter, FastAPI

from src.apps import ROUTERS
from src.config import API_PREFIX, DOCS_URL, OPENAPI_URL, REDOC_URL
from src.middleware import setup_cors, setup_exception_handlers


def create_fastapi_app():
    app = FastAPI(docs_url=DOCS_URL, redoc_url=REDOC_URL, openapi_url=OPENAPI_URL)
    api = APIRouter(prefix=API_PREFIX)

    setup_cors(app)
    setup_exception_handlers(app)

    for router in ROUTERS:
        api.include_router(router)

    app.include_router(api)
    return app
