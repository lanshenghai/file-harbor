from __future__ import annotations

from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import time
from threading import Event
from types import SimpleNamespace

import pytest


class FakeSftpAttr:
    def __init__(self, filename: str, mode: int, size: int = 0) -> None:
        self.filename = filename
        self.st_mode = mode
        self.st_size = size


class FakeSftpClient:
    def __init__(self) -> None:
        self.closed = False
        self.tree = {
            "/root": [
                FakeSftpAttr("dir", 0o040755, 0),
                FakeSftpAttr("a.txt", 0o100644, 4),
            ],
            "/root/dir": [
                FakeSftpAttr("b.bin", 0o100644, 2),
            ],
        }
        self.files = {
            "/root/a.txt": b"test",
            "/root/dir/b.bin": b"xy",
        }

    def listdir_attr(self, path: str):
        if path not in self.tree:
            raise FileNotFoundError(path)
        return list(self.tree[path])

    def get(self, remote_path: str, local_path: str) -> None:
        from pathlib import Path

        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[remote_path])

    def open(self, remote_path: str, mode: str):
        assert mode == "rb"
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        return BytesIO(self.files[remote_path])

    def close(self) -> None:
        self.closed = True


class FakeSshClient:
    def __init__(self, sftp: FakeSftpClient) -> None:
        self.sftp = sftp
        self.policy = None
        self.connected_with = None
        self.closed = False

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def connect(self, **kwargs) -> None:
        self.connected_with = kwargs

    def open_sftp(self) -> FakeSftpClient:
        return self.sftp

    def close(self) -> None:
        self.closed = True


def test_sftp_backend_walks_downloads_and_closes(monkeypatch, tmp_path):
    from app.backends.sftp import SftpBackend

    fake_sftp = FakeSftpClient()
    fake_ssh = FakeSshClient(fake_sftp)
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: fake_ssh,
        AutoAddPolicy=lambda: "policy",
        S_ISDIR=lambda mode: bool(mode & 0o040000),
    )
    monkeypatch.setattr("app.backends.sftp.paramiko", fake_paramiko)

    backend = SftpBackend(
        host="sftp.example.com",
        root="/root",
        username="alice",
        password="secret",
    )

    assert fake_ssh.policy == "policy"
    assert fake_ssh.connected_with == {
        "hostname": "sftp.example.com",
        "port": 22,
        "username": "alice",
        "password": "secret",
    }

    entries = backend.listdir("/root")
    assert [(entry.name, entry.type, entry.size) for entry in entries] == [
        ("dir", "dir", None),
        ("a.txt", "file", 4),
    ]
    assert backend.walk_files("/root") == [
        ("/root/dir/b.bin", 2),
        ("/root/a.txt", 4),
    ]

    target = tmp_path / "out" / "a.txt"
    backend.download_file("/root/a.txt", target)
    assert target.read_bytes() == b"test"

    backend.close()
    assert fake_sftp.closed is True
    assert fake_ssh.closed is True


def test_sftp_backend_classifies_auth_failure(monkeypatch):
    from app.backends.sftp import SftpBackend

    class FakeAuthenticationError(Exception):
        pass

    class AuthFailingSshClient(FakeSshClient):
        def __init__(self) -> None:
            super().__init__(FakeSftpClient())

        def connect(self, **kwargs) -> None:
            raise FakeAuthenticationError("bad password")

    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: AuthFailingSshClient(),
        AutoAddPolicy=lambda: "policy",
        AuthenticationException=FakeAuthenticationError,
        S_ISDIR=lambda mode: bool(mode & 0o040000),
    )
    monkeypatch.setattr("app.backends.sftp.paramiko", fake_paramiko)

    with pytest.raises(PermissionError, match="authentication failed"):
        SftpBackend(
            host="sftp.example.com",
            root="/root",
            username="alice",
            password="wrong",
        )


def test_sftp_download_cancellation_removes_partial_file(monkeypatch, tmp_path):
    from app.backends import DownloadCancelled
    from app.backends.sftp import SftpBackend

    cancel_event = Event()

    class CancellingStream(BytesIO):
        def read(self, size=-1):
            data = super().read(size)
            cancel_event.set()
            return data

    class CancellingSftpClient(FakeSftpClient):
        def open(self, remote_path: str, mode: str):
            assert mode == "rb"
            return CancellingStream(self.files[remote_path])

    fake_sftp = CancellingSftpClient()
    fake_ssh = FakeSshClient(fake_sftp)
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: fake_ssh,
        AutoAddPolicy=lambda: "policy",
        S_ISDIR=lambda mode: bool(mode & 0o040000),
    )
    monkeypatch.setattr("app.backends.sftp.paramiko", fake_paramiko)
    backend = SftpBackend(
        host="sftp.example.com",
        root="/root",
        username="alice",
        password="secret",
    )
    target = tmp_path / "a.txt"

    with pytest.raises(DownloadCancelled):
        backend.download_file("/root/a.txt", target, cancel_event)

    assert not target.exists()
    assert not (tmp_path / "a.txt.part").exists()


def test_sftp_backend_serializes_parallel_downloads_on_single_client(monkeypatch, tmp_path):
    from app.backends.sftp import SftpBackend

    class NonThreadSafeSftpClient(FakeSftpClient):
        def __init__(self) -> None:
            super().__init__()
            self.files["/root/c.txt"] = b"more"
            self._active_readers = 0

        def open(self, remote_path: str, mode: str):
            assert mode == "rb"
            parent = self
            stream = BytesIO(self.files[remote_path])
            orig_read = stream.read

            def guarded_read(size=-1):
                parent._active_readers += 1
                if parent._active_readers > 1:
                    parent._active_readers -= 1
                    raise RuntimeError("Garbage packet received")
                try:
                    # Hold the read briefly so a parallel read overlaps.
                    time.sleep(0.05)
                    return orig_read(size)
                finally:
                    parent._active_readers -= 1

            stream.read = guarded_read
            return stream

    fake_sftp = NonThreadSafeSftpClient()
    fake_ssh = FakeSshClient(fake_sftp)
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: fake_ssh,
        AutoAddPolicy=lambda: "policy",
        S_ISDIR=lambda mode: bool(mode & 0o040000),
    )
    monkeypatch.setattr("app.backends.sftp.paramiko", fake_paramiko)
    backend = SftpBackend(
        host="sftp.example.com",
        root="/root",
        username="alice",
        password="secret",
    )

    target_a = tmp_path / "a.txt"
    target_c = tmp_path / "c.txt"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(backend.download_file, "/root/a.txt", target_a),
            pool.submit(backend.download_file, "/root/c.txt", target_c),
        ]
        for future in futures:
            future.result()

    assert target_a.read_bytes() == b"test"
    assert target_c.read_bytes() == b"more"


def test_factory_selects_backend_and_rejects_unknown(monkeypatch):
    from app.backends.factory import open_backend

    called = {}

    class DummySmb:
        def __init__(self, **kwargs) -> None:
            called["smb"] = kwargs

    class DummySftp:
        def __init__(self, **kwargs) -> None:
            called["sftp"] = kwargs

    monkeypatch.setattr("app.backends.factory.SmbBackend", DummySmb)
    monkeypatch.setattr("app.backends.factory.SftpBackend", DummySftp)

    smb_backend = open_backend(
        "SMB",
        username="alice",
        password="secret",
        domain="DOMAIN",
        host="server",
        root=r"\\server\share\root",
    )
    sftp_backend = open_backend(
        "sftp",
        username="alice",
        password="secret",
        domain="ignored",
        host="sftp.example.com",
        root="/root",
    )

    assert isinstance(smb_backend, DummySmb)
    assert called["smb"]["domain"] == "DOMAIN"
    assert isinstance(sftp_backend, DummySftp)
    assert called["sftp"]["root"] == "/root"

    with pytest.raises(ValueError, match="Unsupported backend protocol"):
        open_backend(
            "ftp",
            username="alice",
            password="secret",
            domain="",
            host="host",
            root="/root",
        )
