from pathlib import Path

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

        def download(self, relative_path, local_path):
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
