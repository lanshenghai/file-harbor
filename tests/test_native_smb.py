from pathlib import Path
from threading import Event

import pytest


def test_native_samba_client_requires_kerberos_realm(tmp_path):
    from app.backends.smb import NativeSmbClient

    binary = tmp_path / "smbclient"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    with pytest.raises(ValueError, match="domain"):
        NativeSmbClient(
            host="files.example.com",
            share="shared",
            username="alice",
            password="secret",
            domain="",
            binary=binary,
        )


def test_native_samba_backend_lists_and_downloads(monkeypatch, tmp_path):
    from app.backends import smb

    calls: list[tuple[str, object]] = []

    class FakeNativeClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def listdir(self, relative_path):
            calls.append(("listdir", relative_path))
            if relative_path == "root":
                return [("sub dir", True, 0), ("a.bin", False, 3)]
            return []

        def download(self, relative_path, local_path, cancel_event=None):
            calls.append(("download", (relative_path, local_path)))
            Path(local_path).write_bytes(b"abc")

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(smb, "NativeSmbClient", FakeNativeClient)

    backend = smb.SmbBackend(
        host="server",
        root=r"\\server\share\root",
        username="alice",
        password="secret",
        domain="EXAMPLE.COM",
    )

    entries = backend.listdir(r"\\server\share\root")
    assert [(entry.name, entry.type, entry.size) for entry in entries] == [
        ("sub dir", "dir", None),
        ("a.bin", "file", 3),
    ]

    target = tmp_path / "a.bin"
    backend.download_file(r"\\server\share\root\a.bin", target)
    backend.close()

    assert target.read_bytes() == b"abc"
    assert ("listdir", "root") in calls
    assert ("download", ("root\\a.bin", target)) in calls
    assert calls[-1] == ("close", None)


def test_native_samba_download_cancellation_terminates_process_and_removes_partial(
    monkeypatch, tmp_path
):
    from app.backends import DownloadCancelled
    from app.backends import smb

    target = tmp_path / "a.bin"
    partial = tmp_path / "a.bin.part"
    cancel_event = Event()

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self.poll_calls = 0
            partial.write_bytes(b"partial")

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls == 1:
                cancel_event.set()
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return -15

        def communicate(self):
            return b"", b""

    process = FakeProcess()
    monkeypatch.setattr(smb.subprocess, "Popen", lambda *args, **kwargs: process)
    client = smb.NativeSmbClient.__new__(smb.NativeSmbClient)
    client.host = "server"
    client.share = "share"
    client.binary = Path("/fake/smbclient")
    client.principal = "alice@EXAMPLE.COM"
    client.cache_path = tmp_path / "krb5cc"
    client._closed = False

    with pytest.raises(DownloadCancelled):
        client.download("root\\a.bin", target, cancel_event)

    assert process.terminated is True
    assert process.killed is False
    assert not target.exists()
    assert not partial.exists()
