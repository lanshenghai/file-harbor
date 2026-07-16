import importlib
import time
import sys
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.backends import Entry, FakeBackend
from app.credentials import SavedCredentials
from app.download import DownloadManager
from app.session import SessionStore


class CloseTrackingBackend(FakeBackend):
    def __init__(self, tree, files):
        super().__init__(tree=tree, files=files)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _configure_sftp_host(monkeypatch):
    monkeypatch.setenv("SFTP_HOST", "sftp.example.com")


def _patch_saved_credentials(monkeypatch, main, username="alice", password="secret", domain="EXAMPLE.COM"):
    monkeypatch.setattr(
        main,
        "load_saved_credentials",
        lambda: SavedCredentials(username=username, password=password, domain=domain, source="test"),
    )


def test_api_returns_ui_defaults_from_environment(monkeypatch):
    main = importlib.import_module("app.main")
    monkeypatch.setenv("REMOTE_PATH", r"\\files.example.com\shared\project")
    monkeypatch.setenv("LOCAL_DIR", "/srv/downloads")
    monkeypatch.setenv("WORKERS", "6")

    response = TestClient(main.app).get("/api/config")

    assert response.status_code == 200
    assert response.json() == {
        "remote_path": r"\\files.example.com\shared\project",
        "local_dir": "/srv/downloads",
        "workers": 6,
    }


def test_api_connect_tree_download_and_delete(monkeypatch, tmp_path):
    root = "/root"
    backend = CloseTrackingBackend(
        tree={
            root: [
                Entry("logs", f"{root}/logs", "dir"),
                Entry("top.txt", f"{root}/top.txt", "file", 3),
            ],
            f"{root}/logs": [
                Entry("a.bin", f"{root}/logs/a.bin", "file", 2),
            ],
        },
        files={
            f"{root}/top.txt": b"abc",
            f"{root}/logs/a.bin": b"xy",
        },
    )

    main = importlib.import_module("app.main")
    _patch_saved_credentials(monkeypatch, main)
    monkeypatch.setattr(
        main,
        "open_backend",
        lambda protocol, username, password, domain, host, root: backend,
    )
    client = TestClient(main.app)

    connect = client.post(
        "/api/connect",
        json={"protocol": "sftp", "remote_path": "/root"},
    )
    assert connect.status_code == 200
    connect_data = connect.json()
    assert connect_data["protocol"] == "sftp"
    assert connect_data["host"] == "sftp.example.com"
    assert connect_data["root"] == "/root"
    session_id = connect_data["session_id"]

    tree = client.get("/api/tree", params={"session_id": session_id})
    assert tree.status_code == 200
    assert tree.json() == [
        {"name": "logs", "path": "/root/logs", "type": "dir", "size": None},
        {"name": "top.txt", "path": "/root/top.txt", "type": "file", "size": 3},
    ]

    download = client.post(
        "/api/download",
        json={
            "session_id": session_id,
            "items": [{"path": "/root/logs", "type": "dir"}],
            "local_dir": str(tmp_path),
            "workers": 2,
        },
    )
    assert download.status_code == 200
    job_id = download.json()["job_id"]

    progress = None
    for _ in range(50):
        progress = client.get(f"/api/download/{job_id}", params={"session_id": session_id})
        assert progress.status_code == 200
        if progress.json()["status"] in {"done", "done_with_errors", "failed"}:
            break
        time.sleep(0.05)

    assert progress is not None
    progress_data = progress.json()
    assert progress_data["status"] == "done"
    assert progress_data["done"] == 1
    assert progress_data["total"] == 1
    assert progress_data["protocol"] == "sftp"
    assert progress_data["workers"] == 2
    assert progress_data["total_bytes"] == 2
    assert progress_data["bytes_done"] == 2
    assert progress_data["speed_bps"] >= 0
    assert progress_data["errors"] == []
    assert (tmp_path / "logs" / "a.bin").read_bytes() == b"xy"

    deleted = client.delete(f"/api/session/{session_id}")
    assert deleted.status_code == 204
    assert backend.close_calls == 1

    missing = client.get("/api/tree", params={"session_id": session_id})
    assert missing.status_code == 404


def test_api_tree_rejects_path_outside_root(monkeypatch):
    root = "/root"
    backend = CloseTrackingBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )

    main = importlib.import_module("app.main")
    _patch_saved_credentials(monkeypatch, main)
    monkeypatch.setattr(
        main,
        "open_backend",
        lambda protocol, username, password, domain, host, root: backend,
    )
    client = TestClient(main.app)

    connect = client.post(
        "/api/connect",
        json={"protocol": "sftp", "remote_path": root},
    )
    assert connect.status_code == 200
    session_id = connect.json()["session_id"]

    response = client.get("/api/tree", params={"session_id": session_id, "path": "/root/../outside"})
    assert response.status_code == 400
    assert "under session root" in response.json()["detail"]

    deleted = client.delete(f"/api/session/{session_id}")
    assert deleted.status_code == 204
    assert backend.close_calls == 1


def test_api_download_rejects_items_outside_root(monkeypatch, tmp_path):
    root = "/root"
    backend = CloseTrackingBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )

    main = importlib.import_module("app.main")
    _patch_saved_credentials(monkeypatch, main)
    monkeypatch.setattr(
        main,
        "open_backend",
        lambda protocol, username, password, domain, host, root: backend,
    )
    client = TestClient(main.app)

    connect = client.post(
        "/api/connect",
        json={"protocol": "sftp", "remote_path": root},
    )
    assert connect.status_code == 200
    session_id = connect.json()["session_id"]

    response = client.post(
        "/api/download",
        json={
            "session_id": session_id,
            "items": [{"path": "/elsewhere/a.bin", "type": "file"}],
            "local_dir": str(tmp_path),
        },
    )
    assert response.status_code == 400
    assert "under session root" in response.json()["detail"]

    deleted = client.delete(f"/api/session/{session_id}")
    assert deleted.status_code == 204
    assert backend.close_calls == 1


def test_api_download_status_touches_session(monkeypatch, tmp_path):
    current = {"value": 100.0}
    monkeypatch.setattr("app.session.time.monotonic", lambda: current["value"])
    root = "/root"
    backend = CloseTrackingBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )

    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "SESSIONS", SessionStore(idle_ttl=0.1))
    monkeypatch.setattr(main, "DOWNLOADS", DownloadManager())
    _patch_saved_credentials(monkeypatch, main)
    monkeypatch.setattr(
        main,
        "open_backend",
        lambda protocol, username, password, domain, host, root: backend,
    )
    client = TestClient(main.app)

    connect = client.post(
        "/api/connect",
        json={"protocol": "sftp", "remote_path": root},
    )
    assert connect.status_code == 200
    session_id = connect.json()["session_id"]

    download = client.post(
        "/api/download",
        json={
            "session_id": session_id,
            "items": [{"path": f"{root}/a.bin", "type": "file"}],
            "local_dir": str(tmp_path),
        },
    )
    assert download.status_code == 200
    job_id = download.json()["job_id"]

    current["value"] = 100.08
    progress = client.get(f"/api/download/{job_id}", params={"session_id": session_id})
    assert progress.status_code == 200

    current["value"] = 100.16
    tree = client.get("/api/tree", params={"session_id": session_id})
    assert tree.status_code == 200

    deleted = client.delete(f"/api/session/{session_id}")
    assert deleted.status_code == 204
    assert backend.close_calls == 1


def test_api_lists_downloads_without_browser_session(monkeypatch, tmp_path):
    root = "/root"
    backend = FakeBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )
    manager = DownloadManager()
    job_id = manager.start(
        backend=backend,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/a.bin", "type": "file"}],
        local_dir=str(tmp_path),
        workers=1,
    )
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "DOWNLOADS", manager)
    client = TestClient(main.app)

    response = client.get("/api/downloads")

    assert response.status_code == 200
    assert response.json()[0]["id"] == job_id


def test_api_cancels_download_without_browser_session(monkeypatch, tmp_path):
    root = "/root"

    class BlockingBackend(FakeBackend):
        def __init__(self, tree, files):
            super().__init__(tree=tree, files=files)
            self.started = Event()

        def download_file(self, remote_path, local_path, cancel_event=None):
            self.started.set()
            if cancel_event is not None:
                cancel_event.wait(timeout=2)

    backend = BlockingBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )
    manager = DownloadManager()
    job_id = manager.start(
        backend=backend,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/a.bin", "type": "file"}],
        local_dir=str(tmp_path),
        workers=1,
    )
    assert backend.started.wait(timeout=1)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "DOWNLOADS", manager)
    client = TestClient(main.app)

    response = client.post(f"/api/download/{job_id}/cancel")
    repeated = client.post(f"/api/download/{job_id}/cancel")
    missing = client.post("/api/download/missing/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    assert repeated.status_code == 200
    assert repeated.json()["status"] in {"cancelling", "cancelled"}
    assert missing.status_code == 404


def test_api_retries_download_after_errors(monkeypatch, tmp_path):
    root = "/root"
    fail_once_path = f"{root}/first.bin"
    ok_path = f"{root}/second.bin"

    class FlakyBackend(FakeBackend):
        def __init__(self, tree, files):
            super().__init__(tree=tree, files=files)
            self._failed_once: set[str] = set()

        def download_file(self, remote_path, local_path, cancel_event=None):
            if remote_path == fail_once_path and remote_path not in self._failed_once:
                self._failed_once.add(remote_path)
                raise RuntimeError("temporary remote read failure")
            super().download_file(remote_path, local_path, cancel_event)

    backend = FlakyBackend(
        tree={
            root: [
                Entry("first.bin", fail_once_path, "file", 1),
                Entry("second.bin", ok_path, "file", 1),
            ]
        },
        files={fail_once_path: b"a", ok_path: b"b"},
    )
    manager = DownloadManager()
    failed_job_id = manager.start(
        backend=backend,
        root=root,
        protocol="sftp",
        items=[{"path": root, "type": "dir"}],
        local_dir=str(tmp_path),
        workers=2,
    )
    for _ in range(100):
        failed_job = manager.get(failed_job_id)
        if failed_job.status in {"done_with_errors", "failed"}:
            break
        time.sleep(0.01)

    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "DOWNLOADS", manager)
    client = TestClient(main.app)

    retry = client.post(f"/api/download/{failed_job_id}/retry")
    assert retry.status_code == 200
    retry_job_id = retry.json()["job_id"]
    assert retry_job_id != failed_job_id

    for _ in range(100):
        progress = client.get(f"/api/download/{retry_job_id}")
        assert progress.status_code == 200
        if progress.json()["status"] in {"done", "done_with_errors", "failed"}:
            break
        time.sleep(0.01)

    payload = progress.json()
    assert payload["status"] == "done"
    assert payload["total"] == 1
    assert payload["done"] == 1


def test_api_removes_completed_job(monkeypatch, tmp_path):
    root = "/root"
    backend = FakeBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )
    manager = DownloadManager()
    job_id = manager.start(
        backend=backend,
        root=root,
        protocol="sftp",
        items=[{"path": f"{root}/a.bin", "type": "file"}],
        local_dir=str(tmp_path),
        workers=1,
    )
    for _ in range(100):
        progress = manager.get(job_id)
        if progress.status in {"done", "done_with_errors", "failed"}:
            break
        time.sleep(0.01)

    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "DOWNLOADS", manager)
    client = TestClient(main.app)

    removed = client.delete(f"/api/download/{job_id}")
    missing = client.get(f"/api/download/{job_id}")

    assert removed.status_code == 204
    assert missing.status_code == 404


def test_api_download_rejects_bad_local_dir(monkeypatch):
    root = "/root"
    backend = CloseTrackingBackend(
        tree={root: [Entry("a.bin", f"{root}/a.bin", "file", 1)]},
        files={f"{root}/a.bin": b"x"},
    )

    main = importlib.import_module("app.main")
    _patch_saved_credentials(monkeypatch, main)
    monkeypatch.setattr(
        main,
        "open_backend",
        lambda protocol, username, password, domain, host, root: backend,
    )
    client = TestClient(main.app)

    connect = client.post(
        "/api/connect",
        json={"protocol": "sftp", "remote_path": "/root"},
    )
    assert connect.status_code == 200
    session_id = connect.json()["session_id"]

    response = client.post(
        "/api/download",
        json={
            "session_id": session_id,
            "items": [{"path": "/root/a.bin", "type": "file"}],
            "local_dir": "relative/path",
        },
    )
    assert response.status_code == 400
    assert "absolute" in response.json()["detail"]

    deleted = client.delete(f"/api/session/{session_id}")
    assert deleted.status_code == 204
    assert backend.close_calls == 1


def test_main_uses_default_and_overridden_bind(monkeypatch):
    main = importlib.import_module("app.main")
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(main.uvicorn, "run", lambda app, host, port: calls.append((host, port)))

    monkeypatch.delenv("FILE_HARBOR_PORT", raising=False)
    monkeypatch.delenv("FILE_HARBOR_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["app.main"])
    main.main()
    assert calls[-1] == ("0.0.0.0", 8088)

    monkeypatch.setenv("FILE_HARBOR_PORT", "8091")
    monkeypatch.setattr(sys, "argv", ["app.main"])
    main.main()
    assert calls[-1] == ("0.0.0.0", 8091)

    monkeypatch.setattr(sys, "argv", ["app.main", "--host", "127.0.0.1", "--port", "8099"])
    main.main()
    assert calls[-1] == ("127.0.0.1", 8099)

    monkeypatch.setenv("FILE_HARBOR_HOST", "192.168.0.24")
    monkeypatch.setattr(sys, "argv", ["app.main"])
    main.main()
    assert calls[-1] == ("192.168.0.24", 8091)
