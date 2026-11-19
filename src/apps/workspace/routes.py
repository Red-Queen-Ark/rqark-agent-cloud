import asyncio
import mimetypes
import tempfile
from email.utils import parsedate_to_datetime
from typing import Annotated, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.types.cloud import CloudConfig
from rqark_sandbox.project import load_project, project_subdir
from rqark_sandbox.workspace import Entry, FileTooLargeError, Workspace, WorkspaceNotReadyError, is_sensitive, redact

from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL, SANDBOX_IMAGE, WORKSPACE_MAX_BYTES, WORKSPACE_START_TIMEOUT
from src.middleware.auth import AuthError, get_bearer, get_current_user

router = APIRouter(prefix='/workspace', tags=['Workspace'])

User = Annotated[dict, Depends(get_current_user)]


class EntryModel(BaseModel):
    name: str
    path: str
    type: Literal['file', 'dir', 'link', 'other']
    size: int
    mtime: float
    target: str | None = None

    @classmethod
    def of(cls, entry: Entry) -> 'EntryModel':
        return cls(name=entry.name, path=entry.path, type=cast('Literal["file", "dir", "link", "other"]', entry.type), size=entry.size, mtime=entry.mtime, target=entry.target)


class ListModel(BaseModel):
    path: str
    entries: list[EntryModel]


class UploadModel(BaseModel):
    entries: list[EntryModel]


class MoveRequest(BaseModel):
    new_path: str


class CompleteModel(BaseModel):
    entries: list[EntryModel]


async def _workspace(user: dict, token: str, project_id: str | None) -> Workspace:
    """The caller's workspace, pointed at the project when one is named.

    project_id is optional: without it the whole volume is browsable, which is
    what a session with no project sees.
    """
    user_id = str(user.get('id') or '')
    if not user_id:
        raise AuthError(401, 'UNAUTHORIZED', 'Authenticated user has no id.')
    workspace = Workspace(user_id, image=SANDBOX_IMAGE)
    if project_id:
        workspace.subdir = project_subdir(await _project(token, project_id))
    return workspace


async def _project(token: str, project_id: str) -> dict | None:
    config = CloudConfig(url=DATA_CLOUD_URL, api_key=token)
    try:
        async with RQArkDataCloudClient(config, timeout=DATA_CLOUD_TIMEOUT) as client:
            return await load_project(client, project_id)
    except DataCloudError as error:
        if error.status_code >= 400:
            raise AuthError(error.status_code, error.code, error.message) from error
        raise AuthError(502, 'INTERNAL', 'Cannot reach data cloud.') from error


async def _ready(workspace: Workspace) -> None:
    try:
        await asyncio.wait_for(workspace.ensure(), WORKSPACE_START_TIMEOUT)
    except TimeoutError as error:
        raise WorkspaceNotReadyError('sandbox is still starting') from error


def _media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


def _headers(name: str) -> dict[str, str]:
    fallback = name.encode('ascii', 'ignore').decode().replace('"', '') or 'download'
    return {'Content-Disposition': f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name)}'}


def _since(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError) as error:
        raise AuthError(400, 'BAD_REQUEST', 'Invalid If-Unmodified-Since header.') from error


@router.get('/list', response_model=ListModel)
async def list_directory(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), show_hidden: bool = Query(default=False), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    entries = await asyncio.to_thread(workspace.list_dir, path, show_hidden)
    return ListModel(path=workspace.relative(path), entries=[EntryModel.of(entry) for entry in entries])


@router.get('/complete', response_model=CompleteModel)
async def complete_paths(user: User, token: Annotated[str, Depends(get_bearer)], query: str = Query(default=''), limit: int = Query(default=50, ge=1, le=200), project_id: str | None = Query(default=None)):
    """Basename-prefix completion for the `@` file picker in the chat input."""
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    entries = await asyncio.to_thread(workspace.complete, query, limit)
    return CompleteModel(entries=[EntryModel.of(entry) for entry in entries])


@router.get('/stat', response_model=EntryModel)
async def stat_path(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    entry = await asyncio.to_thread(workspace.stat, path)
    return EntryModel.of(entry)


@router.get('/file', responses={200: {'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}, 'description': 'File content'}})
async def download_file(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    if is_sensitive(workspace.relative(path)):
        data, entry = await asyncio.to_thread(workspace.read_text_file, path)
        headers = _headers(entry.name)
        headers['X-Content-Redacted'] = 'true'
        return Response(content=redact(data), media_type='text/plain; charset=utf-8', headers=headers)
    entry, chunks = await asyncio.to_thread(workspace.open_file, path)
    headers = _headers(entry.name)
    headers['Content-Length'] = str(entry.size)
    headers['X-Content-Type-Options'] = 'nosniff'
    return StreamingResponse(chunks, media_type=_media_type(entry.name), headers=headers)


@router.get('/archive', responses={200: {'content': {'application/zip': {'schema': {'type': 'string', 'format': 'binary'}}}, 'description': 'ZIP archive'}})
async def download_archive(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    name, _, chunks = await asyncio.to_thread(workspace.archive, path)
    return StreamingResponse(chunks, media_type='application/zip', headers=_headers(name))


@router.post('/upload', response_model=UploadModel)
async def upload_files(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), overwrite: bool = Query(default=True), files: list[UploadFile] = File(...), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    entries: list[Entry] = []
    for upload in files:
        name = upload.filename or 'upload'
        temporary = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            size = 0
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > WORKSPACE_MAX_BYTES:
                    raise FileTooLargeError(f'file is too large: {name}')
                temporary.write(chunk)
            temporary.seek(0)
            entries.append(await asyncio.to_thread(workspace.put_file, path, name, temporary, size, overwrite=overwrite))
        finally:
            temporary.close()
            await upload.close()
    return UploadModel(entries=[EntryModel.of(entry) for entry in entries])


@router.put('/file', response_model=EntryModel)
async def write_file(
    request: Request,
    user: User,
    token: Annotated[str, Depends(get_bearer)],
    path: str = Query(default=''),
    overwrite: bool = Query(default=True),
    if_unmodified_since: Annotated[str | None, Header(alias='If-Unmodified-Since')] = None,
    project_id: str | None = Query(default=None),
):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    since = _since(if_unmodified_since)
    temporary = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > WORKSPACE_MAX_BYTES:
                raise FileTooLargeError('body is too large')
            temporary.write(chunk)
        temporary.seek(0)
        entry = await asyncio.to_thread(workspace.put, path, temporary, size, overwrite=overwrite, if_unmodified_since=since)
    finally:
        temporary.close()
    return EntryModel.of(entry)


@router.post('/mkdir', response_model=EntryModel)
async def make_directory(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    entry = await asyncio.to_thread(workspace.mkdir, path)
    return EntryModel.of(entry)


@router.patch('/file', response_model=EntryModel)
async def move_path(body: MoveRequest, user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), overwrite: bool = Query(default=False), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    entry = await asyncio.to_thread(workspace.move, path, body.new_path, overwrite=overwrite)
    return EntryModel.of(entry)


@router.delete('')
async def delete_path(user: User, token: Annotated[str, Depends(get_bearer)], path: str = Query(default=''), project_id: str | None = Query(default=None)):
    workspace = await _workspace(user, token, project_id)
    await _ready(workspace)
    relative = workspace.relative(path)
    await asyncio.to_thread(workspace.delete, path)
    return {'path': relative, 'deleted': True}
