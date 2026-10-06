import asyncio
import io
import posixpath
import shlex
import tarfile
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, cast

from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

import docker


class WorkspaceError(Exception):
    pass


class PathOutsideWorkspaceError(WorkspaceError):
    pass


class FileMissingError(WorkspaceError):
    pass


class IsDirectoryError(WorkspaceError):
    pass


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str


def _text(data: bytes | None) -> str:
    return data.decode('utf-8', errors='replace') if data else ''


def _drain(stream: Iterator[bytes]) -> bytes:
    buffer = io.BytesIO()
    for chunk in stream:
        buffer.write(chunk)
    return buffer.getvalue()


_client: docker.DockerClient | None = None
_client_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _client_instance() -> docker.DockerClient:
    global _client
    with _client_guard:
        if _client is None:
            _client = docker.from_env()
        return _client


def _lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


class Workspace:
    WORKDIR: str = '/workspace'
    RESERVED_NETWORKS: frozenset[str] = frozenset({'none', 'host', 'bridge'})

    def __init__(
        self,
        user_id: str,
        image: str = 'ubuntu:24.04',
        network: str | None = None,
        memory: str = '1g',
        nano_cpus: int = 1_000_000_000,
        pids_limit: int = 256,
        cap_add: Sequence[str] = (),
    ):
        self.user_id = user_id
        self.image = image
        self.network = network
        self.memory = memory
        self.nano_cpus = nano_cpus
        self.pids_limit = pids_limit
        self.cap_add = tuple(cap_add)

    @property
    def name(self) -> str:
        return f'rqark-user-{self.user_id}'

    @property
    def container_name(self) -> str:
        return f'{self.name}-workspace'

    @property
    def network_name(self) -> str:
        return f'{self.name}-net'

    @staticmethod
    def to_container_path(path: str | None, default: str = '.') -> str:
        raw = (path or default).strip() or default
        joined = raw if raw.startswith('/') else posixpath.join(Workspace.WORKDIR, raw)
        normalized = posixpath.normpath(joined)
        if normalized != Workspace.WORKDIR and not normalized.startswith(Workspace.WORKDIR + '/'):
            raise PathOutsideWorkspaceError(f'path outside workspace: {raw}')
        return normalized

    def _volume(self, client: docker.DockerClient):
        try:
            return client.volumes.get(self.name)
        except NotFound:
            return client.volumes.create(self.name)

    def _network(self, client: docker.DockerClient) -> str:
        labels = {'rqark.sandbox': '1', 'rqark.user': self.user_id}
        try:
            return str(client.networks.get(self.network_name).name)
        except NotFound:
            return str(client.networks.create(self.network_name, driver='bridge', labels=labels).name)

    def _connection(self, client: docker.DockerClient) -> dict[str, Any]:
        if self.network in self.RESERVED_NETWORKS:
            return {'network_mode': str(self.network)}
        if self.network:
            return {'network': self.network}
        return {'network': self._network(client)}

    def _create(self, client: docker.DockerClient, pulled: bool = False) -> Container:
        volume = self._volume(client)
        try:
            return client.containers.run(
                self.image,
                command='sleep infinity',
                name=self.container_name,
                detach=True,
                init=True,
                mem_limit=self.memory,
                memswap_limit=self.memory,
                nano_cpus=self.nano_cpus,
                pids_limit=self.pids_limit,
                cap_drop=['ALL'],
                cap_add=list(self.cap_add),
                security_opt=['no-new-privileges'],
                volumes={volume.name: {'bind': self.WORKDIR, 'mode': 'rw'}},
                working_dir=self.WORKDIR,
                labels={'rqark.sandbox': '1', 'rqark.user': self.user_id},
                **self._connection(client),
            )
        except ImageNotFound:
            if pulled:
                raise
            client.images.pull(self.image)
            return self._create(client, pulled=True)
        except APIError as error:
            if error.status_code != 409:
                raise
            return client.containers.get(self.container_name)

    def _container(self, client: docker.DockerClient) -> Container:
        try:
            container = client.containers.get(self.container_name)
        except NotFound:
            return self._create(client)
        if container.status == 'running':
            return container
        try:
            container.start()
            return container
        except (APIError, DockerException):
            try:
                container.remove(force=True)
            except (APIError, DockerException):
                pass
            return self._create(client)

    def _ensure(self) -> Container:
        client = _client_instance()
        with _lock(self.container_name):
            return self._container(client)

    def _raw(self, script: str, cwd: str | None = None) -> ShellResult:
        try:
            container = self._ensure()
            result = container.exec_run(cmd=['/bin/sh', '-c', script], demux=True, workdir=cwd or self.WORKDIR)
        except (APIError, DockerException, NotFound) as error:
            return ShellResult(-1, '', str(error))
        stdout, stderr = cast('tuple[bytes | None, bytes | None]', result.output)
        exit_code = result.exit_code if result.exit_code is not None else -1
        return ShellResult(exit_code, _text(stdout), _text(stderr))

    def run_shell(self, command: str, cwd: str | None = None, timeout: int = 60, interpreter: str = '/bin/sh') -> ShellResult:
        return self._raw(f'exec timeout -k 5 {timeout} {interpreter} -c {shlex.quote(command)}', cwd)

    async def shell(self, command: str, cwd: str | None = None, timeout: int = 60, interpreter: str = '/bin/sh') -> ShellResult:
        return await asyncio.to_thread(self.run_shell, command, cwd, timeout, interpreter)

    async def read_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(self._read_bytes, path)

    def _read_bytes(self, path: str) -> bytes:
        container = self._ensure()
        try:
            stream, _ = container.get_archive(path)
        except NotFound as error:
            raise FileMissingError(f'file not found: {path}') from error
        with tarfile.open(fileobj=io.BytesIO(_drain(stream)), mode='r') as tar:
            for member in tar.getmembers():
                if member.isdir():
                    raise IsDirectoryError(f'path is a directory: {path}')
                if member.isfile():
                    handle = tar.extractfile(member)
                    return handle.read() if handle is not None else b''
        raise FileMissingError(f'file not found: {path}')

    async def write_bytes(self, path: str, data: bytes) -> None:
        await asyncio.to_thread(self._write_bytes, path, data)

    def _write_bytes(self, path: str, data: bytes) -> None:
        container = self._ensure()
        parent = posixpath.dirname(path) or self.WORKDIR
        self._raw(f'mkdir -p {shlex.quote(parent)}')
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            info = tarfile.TarInfo(posixpath.basename(path))
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        if not container.put_archive(parent, buffer.getvalue()):
            raise WorkspaceError(f'cannot write file: {path}')

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop)

    def _stop(self) -> None:
        container = self._find()
        if container is None or container.status != 'running':
            return
        try:
            container.stop(timeout=5)
        except (APIError, DockerException):
            pass

    async def remove(self) -> None:
        await asyncio.to_thread(self._remove)

    def _remove(self) -> None:
        container = self._find()
        if container is None:
            return
        try:
            container.remove(force=True)
        except (APIError, DockerException):
            pass

    async def purge(self) -> None:
        await asyncio.to_thread(self._purge)

    def _purge(self) -> None:
        self._remove()
        try:
            client = _client_instance()
        except DockerException:
            return
        try:
            client.volumes.get(self.name).remove(force=True)
        except (APIError, DockerException, NotFound):
            pass
        try:
            client.networks.get(self.network_name).remove()
        except (APIError, DockerException, NotFound):
            pass

    def _find(self) -> Container | None:
        try:
            return _client_instance().containers.get(self.container_name)
        except NotFound:
            return None
