from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import CORS_ALLOW_CREDENTIALS, CORS_ORIGINS
from src.middleware.auth import AuthError


def setup_cors(app: FastAPI) -> None:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=CORS_ALLOW_CREDENTIALS, allow_methods=['*'], allow_headers=['*'])


def _auth_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, AuthError)
    return JSONResponse(status_code=error.status_code, content={'error': {'code': error.code, 'message': error.message}})


def _validation_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    return JSONResponse(status_code=400, content={'error': {'code': 'BAD_REQUEST', 'message': 'Malformed request body.'}})


def setup_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthError, _auth_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
