import os
from pathlib import Path

import pytest

from app.pathutil import (
    normalize_remote_root,
    parse_unc,
    normalize_remote_path_under_root,
    rel_under_root,
    unc_to_sftp_path,
    validate_local_dir,
)


def test_parse_unc():
    p = parse_unc(r"\\files.example.com\shared\projects\alice\x")
    assert p.host == "files.example.com"
    assert p.share == "shared"
    assert p.rel_path == "projects/alice/x"


def test_unc_to_sftp_path():
    assert unc_to_sftp_path(r"\\h\shared\projects\a") == "/shared/projects/a"


def test_normalize_remote_root_sftp_unc_keeps_explicit_host():
    normalized = normalize_remote_root(
        "sftp",
        r"\\files.example.com\shared\projects\alice",
        "custom-sftp.example.net",
    )
    assert normalized == {
        "host": "custom-sftp.example.net",
        "root": "/shared/projects/alice",
    }


def test_normalize_remote_root_sftp_unc_uses_configured_host(monkeypatch):
    monkeypatch.setenv("SFTP_HOST", "sftp.example.com")
    normalized = normalize_remote_root(
        "sftp",
        r"\\files.example.com\shared\projects\alice",
        None,
    )
    assert normalized == {
        "host": "sftp.example.com",
        "root": "/shared/projects/alice",
    }


def test_normalize_remote_root_sftp_requires_host(monkeypatch):
    monkeypatch.delenv("SFTP_HOST", raising=False)

    with pytest.raises(ValueError, match="SFTP host"):
        normalize_remote_root("sftp", "/shared/projects/alice", None)


def test_validate_local_dir_ok(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    assert validate_local_dir(str(d)) == d.resolve()


def test_validate_local_dir_allows_missing_dir_under_writable_parent(tmp_path):
    target = tmp_path / "new" / "child"
    assert validate_local_dir(str(target)) == target.resolve()


def test_validate_local_dir_rejects_etc():
    with pytest.raises(ValueError):
        validate_local_dir("/etc/passwd")


def test_validate_local_dir_rejects_relative():
    with pytest.raises(ValueError):
        validate_local_dir("relative/path")


def test_validate_local_dir_rejects_existing_file(tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("x")
    with pytest.raises(ValueError, match="directory"):
        validate_local_dir(str(target))


def test_validate_local_dir_rejects_non_writable_existing_dir(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod-based writability checks do not work as root")

    target = tmp_path / "blocked"
    target.mkdir()
    target.chmod(0o555)
    if os.access(target, os.W_OK):
        pytest.skip("filesystem still reports directory as writable")

    with pytest.raises(ValueError, match="writable"):
        validate_local_dir(str(target))


def test_validate_local_dir_rejects_missing_dir_under_non_writable_parent(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod-based writability checks do not work as root")

    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.mkdir()
    blocked_parent.chmod(0o555)
    if os.access(blocked_parent, os.W_OK):
        pytest.skip("filesystem still reports parent as writable")

    target = blocked_parent / "child"
    with pytest.raises(ValueError, match="writable"):
        validate_local_dir(str(target))


def test_rel_under_root_smb():
    root = r"\\h\share\root"
    file = r"\\h\share\root\a\b.bin"
    assert rel_under_root(file, root, "smb") == "a/b.bin"


def test_remote_path_under_root_rejects_traversal():
    with pytest.raises(ValueError):
        normalize_remote_path_under_root("/root/../outside", "/root", "sftp")


def test_remote_path_under_root_rejects_smb_traversal():
    with pytest.raises(ValueError):
        normalize_remote_path_under_root(r"\\host\share\root\..\outside", r"\\host\share\root", "smb")
