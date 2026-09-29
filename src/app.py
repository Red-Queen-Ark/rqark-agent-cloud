from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from src.apps import ROUTERS
from src.config import API_PREFIX, DOCS_URL, OPENAPI_URL, REDOC_URL
from src.middleware import setup_cors
from src.middleware.auth import AuthError


def _auth_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, AuthError)
    return JSONResponse(status_code=error.status_code, content={'error': {'code': error.code, 'message': error.message}})


def create_fastapi_app():
    app = FastAPI(
        docs_url=DOCS_URL,
        redoc_url=REDOC_URL,
        openapi_url=OPENAPI_URL,
    )
    api = APIRouter(prefix=API_PREFIX)

    setup_cors(app)
    app.add_exception_handler(AuthError, _auth_error_handler)

    for router in ROUTERS:
        api.include_router(router)

    app.include_router(api)
    return app
