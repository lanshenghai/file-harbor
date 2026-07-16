import pytest

from app.session import Session, SessionStore


def test_create_and_get_session(monkeypatch):
    current = {"value": 100.0}

    monkeypatch.setattr("app.session.time.monotonic", lambda: current["value"])

    store = SessionStore()
    session = store.create(
        protocol="sftp",
        username="alice",
        password="secret",
        domain="example",
        host="files.example.com",
        root="/data",
    )

    assert isinstance(session, Session)
    assert session.id
    assert session.protocol == "sftp"
    assert session.username == "alice"
    assert session.password == "secret"
    assert session.domain == "example"
    assert session.host == "files.example.com"
    assert session.root == "/data"
    assert session.last_used == 100.0
    assert store.get(session.id) == session


def test_touch_updates_last_used(monkeypatch):
    current = {"value": 200.0}

    monkeypatch.setattr("app.session.time.monotonic", lambda: current["value"])

    store = SessionStore()
    session = store.create(
        protocol="smb",
        username="bob",
        password="pw",
        domain="domain",
        host="host",
        root="root",
    )

    current["value"] = 250.0
    store.touch(session.id)

    touched = store.get(session.id)
    assert touched.last_used == 250.0


def test_delete_removes_session(monkeypatch):
    monkeypatch.setattr("app.session.time.monotonic", lambda: 300.0)

    store = SessionStore()
    session = store.create(
        protocol="sftp",
        username="carol",
        password="pw",
        domain="domain",
        host="host",
        root="/",
    )

    store.delete(session.id)

    with pytest.raises(KeyError):
        store.get(session.id)


def test_get_expires_idle_session(monkeypatch):
    current = {"value": 400.0}

    monkeypatch.setattr("app.session.time.monotonic", lambda: current["value"])

    store = SessionStore(idle_ttl=0.01)
    session = store.create(
        protocol="sftp",
        username="dave",
        password="pw",
        domain="domain",
        host="host",
        root="/",
    )

    current["value"] = 400.02

    with pytest.raises(KeyError):
        store.get(session.id)

    with pytest.raises(KeyError):
        store.get(session.id)


def test_sweep_expired_returns_expired_ids(monkeypatch):
    current = {"value": 500.0}

    monkeypatch.setattr("app.session.time.monotonic", lambda: current["value"])

    store = SessionStore(idle_ttl=0.01)
    expired = store.create(
        protocol="sftp",
        username="erin",
        password="pw",
        domain="domain",
        host="host",
        root="/expired",
    )
    current["value"] = 500.015
    live = store.create(
        protocol="sftp",
        username="frank",
        password="pw",
        domain="domain",
        host="host",
        root="/live",
    )

    current["value"] = 500.02
    store.touch(live.id)

    swept = store.sweep_expired()

    assert swept == [expired.id]
    with pytest.raises(KeyError):
        store.get(expired.id)
    assert store.get(live.id) == live

