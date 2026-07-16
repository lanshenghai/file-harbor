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


def test_gvfs_backend_lists_and_downloads(monkeypatch, tmp_path):
    from app.backends import smb

    calls: list[tuple[str, object]] = []

    class FakeGvfsClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def listdir(self, relative_path):
            calls.append(("listdir", relative_path))
            if relative_path == "root":
                return [("sub dir", True, 0), ("a.bin", False, 3)]
            return []

        def download(self, relative_path, local_path, cancel_event=None, progress_callback=None):
            calls.append(("download", (relative_path, local_path)))
            Path(local_path).write_bytes(b"abc")
            if progress_callback is not None:
                progress_callback(3)

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(smb, "GvfsSmbClient", FakeGvfsClient)

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


def test_gvfs_download_remounts_when_mount_path_disappears(monkeypatch, tmp_path):
    from app.backends import smb

    data = b"abc"
    mounted_root = tmp_path / "mounted"
    mounted_root.mkdir()
    remote_file = mounted_root / "root" / "a.bin"
    remote_file.parent.mkdir(parents=True, exist_ok=True)
    remote_file.write_bytes(data)

    client = smb.GvfsSmbClient.__new__(smb.GvfsSmbClient)
    client.mount_root = tmp_path / "missing-mount"
    client._closed = False

    local_path_calls = {"count": 0}

    def fake_local_path(relative_path):
        local_path_calls["count"] += 1
        if local_path_calls["count"] == 1:
            return client.mount_root / "root" / "a.bin"
        return mounted_root / "root" / "a.bin"

    remount_calls = {"count": 0}

    def fake_ensure_mount():
        remount_calls["count"] += 1
        client.mount_root = mounted_root
        return mounted_root

    monkeypatch.setattr(client, "_local_path", fake_local_path)
    monkeypatch.setattr(client, "_ensure_mount", fake_ensure_mount)
    monkeypatch.setattr(smb.time, "sleep", lambda _: None)

    target = tmp_path / "downloaded.bin"
    client.download("root\\a.bin", target)

    assert target.read_bytes() == data
    assert remount_calls["count"] >= 1
    assert local_path_calls["count"] >= 2
