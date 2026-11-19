import pytest
from fastapi.testclient import TestClient
from rqark_sandbox.workspace import Entry, FileMissingError, Workspace

from src.app import create_fastapi_app
from src.apps.workspace import routes
from src.middleware.auth import get_current_user

HEADERS = {'Authorization': 'Bearer test'}


def entry(name='a.txt', path='a.txt', kind='file', size=3):
    return Entry(name=name, path=path, type=kind, size=size, mtime=1_700_000_000.0)


class FakeWorkspace:
    def __init__(self, user_id='u1', image=None):
        self.user_id = user_id
        self.uploads = []
        self.deleted = []
        self.moved = []
        self.error = None

    async def ensure(self):
        if self.error:
            raise self.error

    def relative(self, path):
        return Workspace('u1').api_path(path).removeprefix('/workspace/').removeprefix('/workspace')

    def list_dir(self, path, show_hidden=False):
        if self.error:
            raise self.error
        return [entry(), entry('docs', 'docs', 'dir', 4096)]

    def stat(self, path):
        return entry()

    def complete(self, query, limit=50):
        return [entry('main.py', 'src/main.py'), entry('src', 'src', 'dir', 4096)]

    def open_file(self, path):
        return entry(), iter([b'abc'])

    def archive(self, path):
        return 'workspace.zip', 3, iter([b'PK'])

    def read_text_file(self, path):
        return b'url = https://x-access-token:secret@example.com/r.git\n', entry('.git/config', '.git/config')

    def put_file(self, directory, name, source, size, *, overwrite=True):
        self.uploads.append((directory, name, source.read(), size, overwrite))
        return entry(name, f'{directory}/{name}'.lstrip('/'), size=size)

    def put(self, path, source, size, *, overwrite=True, if_unmodified_since=None):
        self.uploads.append((path, source.read(), size, overwrite, if_unmodified_since))
        return entry(path.rsplit('/', 1)[-1], path, size=size)

    def mkdir(self, path):
        return entry(path.rsplit('/', 1)[-1], path, 'dir', 4096)

    def move(self, path, new_path, *, overwrite=False):
        self.moved.append((path, new_path, overwrite))
        return entry(new_path.rsplit('/', 1)[-1], new_path)

    def delete(self, path):
        self.deleted.append(path)


@pytest.fixture
def client(monkeypatch):
    fake = FakeWorkspace()
    monkeypatch.setattr(routes, 'Workspace', lambda user_id, image=None: fake)
    app = create_fastapi_app()
    app.dependency_overrides[get_current_user] = lambda: {'id': 'u1'}
    with TestClient(app) as test_client:
        test_client.fake = fake
        yield test_client
    app.dependency_overrides.clear()


def test_list_route(client):
    response = client.get('/api/workspace/list', headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body['path'] == ''
    assert [item['name'] for item in body['entries']] == ['a.txt', 'docs']
    assert body['entries'][1]['type'] == 'dir'


def test_stat_route(client):
    response = client.get('/api/workspace/stat?path=a.txt', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['path'] == 'a.txt'


def test_complete_route_returns_matching_paths(client):
    response = client.get('/api/workspace/complete?query=src/ma', headers=HEADERS)
    assert response.status_code == 200
    assert [item['path'] for item in response.json()['entries']] == ['src/main.py', 'src']


def test_download_streams_the_file(client):
    response = client.get('/api/workspace/file?path=a.txt', headers=HEADERS)
    assert response.status_code == 200
    assert response.content == b'abc'
    assert response.headers['content-length'] == '3'
    assert 'attachment' in response.headers['content-disposition']


def test_download_redacts_sensitive_files(client):
    response = client.get('/api/workspace/file?path=.git/config', headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['x-content-redacted'] == 'true'
    assert 'secret' not in response.text


def test_archive_route(client):
    response = client.get('/api/workspace/archive', headers=HEADERS)
    assert response.status_code == 200
    assert response.content == b'PK'
    assert 'workspace.zip' in response.headers['content-disposition']


def test_upload_route_reads_every_file(client):
    response = client.post('/api/workspace/upload?path=docs', headers=HEADERS, files=[('files', ('a.txt', b'hello', 'text/plain')), ('files', ('b.txt', b'world', 'text/plain'))])
    assert response.status_code == 200
    assert [item['name'] for item in response.json()['entries']] == ['a.txt', 'b.txt']
    assert client.fake.uploads == [('docs', 'a.txt', b'hello', 5, True), ('docs', 'b.txt', b'world', 5, True)]


def test_write_route_passes_the_precondition(client):
    response = client.put('/api/workspace/file?path=a.txt', headers={**HEADERS, 'If-Unmodified-Since': 'Tue, 14 Nov 2023 22:13:20 GMT'}, content=b'hello')
    assert response.status_code == 200
    assert client.fake.uploads[0][0] == 'a.txt'
    assert client.fake.uploads[0][4] == 1_700_000_000


def test_write_route_rejects_a_bad_precondition(client):
    response = client.put('/api/workspace/file?path=a.txt', headers={**HEADERS, 'If-Unmodified-Since': 'yesterday'}, content=b'hello')
    assert response.status_code == 400


def test_mkdir_route(client):
    response = client.post('/api/workspace/mkdir?path=docs', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['type'] == 'dir'


def test_move_route(client):
    response = client.patch('/api/workspace/file?path=a.txt', headers=HEADERS, json={'new_path': 'b.txt'})
    assert response.status_code == 200
    assert client.fake.moved == [('a.txt', 'b.txt', False)]


def test_delete_route(client):
    response = client.delete('/api/workspace?path=docs', headers=HEADERS)
    assert response.status_code == 200
    assert client.fake.deleted == ['docs']


def test_missing_file_maps_to_404(client):
    client.fake.error = FileMissingError('path not found: nope')
    response = client.get('/api/workspace/list', headers=HEADERS)
    assert response.status_code == 404
    assert response.json()['error']['code'] == 'FILE_NOT_FOUND'


def test_upload_enforces_the_size_cap(client, monkeypatch):
    monkeypatch.setattr(routes, 'WORKSPACE_MAX_BYTES', 2)
    response = client.post('/api/workspace/upload', headers=HEADERS, files=[('files', ('a.txt', b'hello', 'text/plain'))])
    assert response.status_code == 413
    assert client.fake.uploads == []
