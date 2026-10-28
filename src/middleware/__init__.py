from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.apps.sandbox.workspace import (
    ConflictError,
    FileMissingError,
    FileTooLargeError,
    InvalidOperationError,
    IsDirectoryError,
    PathNotDirectoryError,
    PathOutsideWorkspaceError,
    PreconditionFailedError,
    WorkspaceError,
    WorkspaceNotReadyError,
)
from src.config import CORS_ALLOW_CREDENTIALS, CORS_ORIGINS
from src.middleware.auth import AuthError

_STATUS = {
    WorkspaceNotReadyError: (503, 'SANDBOX_NOT_READY'),
    FileMissingError: (404, 'FILE_NOT_FOUND'),
    IsDirectoryError: (400, 'IS_A_DIRECTORY'),
    PathNotDirectoryError: (400, 'NOT_A_DIRECTORY'),
    PathOutsideWorkspaceError: (400, 'INVALID_PATH'),
    InvalidOperationError: (400, 'BAD_REQUEST'),
    FileTooLargeError: (413, 'FILE_TOO_LARGE'),
    ConflictError: (409, 'CONFLICT'),
    PreconditionFailedError: (412, 'PRECONDITION_FAILED'),
}


def setup_cors(app: FastAPI) -> None:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=CORS_ALLOW_CREDENTIALS, allow_methods=['*'], allow_headers=['*'])


def _auth_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, AuthError)
    return JSONResponse(status_code=error.status_code, content={'error': {'code': error.code, 'message': error.message}})


def _workspace_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, WorkspaceError)
    status, code = next(((status, code) for kind, (status, code) in _STATUS.items() if isinstance(error, kind)), (500, 'SANDBOX_ERROR'))
    headers = {'Retry-After': '10'} if status == 503 else None
    return JSONResponse(status_code=status, content={'error': {'code': code, 'message': str(error) or code}}, headers=headers)


def _validation_error_handler(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    return JSONResponse(status_code=400, content={'error': {'code': 'BAD_REQUEST', 'message': 'Malformed request body.'}})


def setup_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthError, _auth_error_handler)
    app.add_exception_handler(WorkspaceError, _workspace_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
