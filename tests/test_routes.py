import pytest

from src.apps.chat import routes as routes_module
from src.middleware.auth import AuthError


def request(message_id='m1'):
    return routes_module.RevertRequest(message_id=message_id)


async def allow(token, session_id, claim=False):
    return True


def free(session_id, user_id):
    return False


async def test_revert_requires_a_user_id():
    with pytest.raises(AuthError) as error:
        await routes_module.revert_message('s1', request(), {}, 't')
    assert error.value.status_code == 401


async def test_revert_forbids_a_foreign_session(monkeypatch):
    async def deny(token, session_id, claim=False):
        return False

    monkeypatch.setattr(routes_module, 'authorize_session', deny)
    with pytest.raises(AuthError) as error:
        await routes_module.revert_message('s1', request(), {'id': 'u1'}, 't')
    assert error.value.status_code == 403


async def test_revert_rejects_a_busy_session(monkeypatch):
    monkeypatch.setattr(routes_module, 'authorize_session', allow)
    monkeypatch.setattr(routes_module, 'is_busy', lambda session_id, user_id: True)
    with pytest.raises(AuthError) as error:
        await routes_module.revert_message('s1', request(), {'id': 'u1'}, 't')
    assert error.value.status_code == 409


async def test_revert_reports_an_impossible_revert(monkeypatch):
    monkeypatch.setattr(routes_module, 'authorize_session', allow)
    monkeypatch.setattr(routes_module, 'is_busy', free)

    async def fail(session_id, message_id, token, user_id):
        return False

    monkeypatch.setattr(routes_module, 'revert_session', fail)
    with pytest.raises(AuthError) as error:
        await routes_module.revert_message('s1', request(), {'id': 'u1'}, 't')
    assert error.value.status_code == 400


async def test_revert_returns_the_echoed_message(monkeypatch):
    called: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(routes_module, 'authorize_session', allow)
    monkeypatch.setattr(routes_module, 'is_busy', free)

    async def ok(session_id, message_id, token, user_id):
        called.append((session_id, message_id, token, user_id))
        return True

    monkeypatch.setattr(routes_module, 'revert_session', ok)

    result = await routes_module.revert_message('s1', request('m2'), {'id': 'u1'}, 't')

    assert result == {'session_id': 's1', 'message_id': 'm2'}
    assert called == [('s1', 'm2', 't', 'u1')]
