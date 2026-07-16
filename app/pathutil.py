from __future__ import annotations

from dataclasses import dataclass
import posixpath
from pathlib import Path
import os
import re

DENIED_PREFIXES = ("/etc", "/usr", "/bin", "/sbin", "/boot")


@dataclass(frozen=True)
class UncParts:
    host: str
    share: str
    rel_path: str


def parse_unc(path: str) -> UncParts:
    s = path.strip().rstrip("\\/")
    norm = s.replace("/", "\\")
    if not norm.startswith("\\\\"):
        raise ValueError(f"Not a UNC path: {path}")

    match = re.match(r"^\\\\([^\\]+)\\([^\\]+)(?:\\(.*))?$", norm)
    if not match:
        raise ValueError(f"Not a UNC path: {path}")

    host, share, rest = match.group(1), match.group(2), match.group(3) or ""
    rel_path = rest.replace("\\", "/").strip("/")
    return UncParts(host=host, share=share, rel_path=rel_path)


def unc_to_sftp_path(path: str) -> str:
    parts = parse_unc(path)
    if parts.rel_path:
        return f"/{parts.share}/{parts.rel_path}"
    return f"/{parts.share}"


def looks_like_unc(path: str) -> bool:
    text = path.strip()
    return text.startswith("\\\\") or text.startswith("//")


def normalize_remote_root(protocol: str, remote_path: str, host: str | None) -> dict:
    protocol = protocol.lower()
    if protocol == "smb":
        if not looks_like_unc(remote_path):
            raise ValueError("SMB requires a UNC remote_path")
        parts = parse_unc(remote_path)
        root = rf"\\{parts.host}\{parts.share}"
        if parts.rel_path:
            root += "\\" + parts.rel_path.replace("/", "\\")
        return {"host": parts.host, "root": root}

    if protocol == "sftp":
        resolved_host = host or os.environ.get("SFTP_HOST", "")
        if not resolved_host:
            raise ValueError("SFTP host is required; set SFTP_HOST")
        if looks_like_unc(remote_path):
            root = unc_to_sftp_path(remote_path)
        else:
            root = remote_path if remote_path.startswith("/") else "/" + remote_path
        return {"host": resolved_host, "root": root.rstrip("/") or "/"}

    raise ValueError(f"Unknown protocol: {protocol}")


def _normalize_remote_path(path: str, protocol: str) -> str:
    protocol = protocol.lower()
    if protocol == "smb":
        parts = parse_unc(path)
        segments: list[str] = []
        for segment in re.split(r"[\\/]+", parts.rel_path):
            if not segment or segment == ".":
                continue
            if segment == "..":
                if not segments:
                    raise ValueError(f"Remote path escapes root: {path}")
                segments.pop()
                continue
            segments.append(segment)
        normalized = rf"\\{parts.host}\{parts.share}"
        if segments:
            normalized += "\\" + "\\".join(segments)
        return normalized.rstrip("\\")

    if protocol == "sftp":
        candidate = path if path.startswith("/") else "/" + path
        normalized = posixpath.normpath(candidate)
        return normalized if normalized.startswith("/") else "/" + normalized

    raise ValueError(f"Unknown protocol: {protocol}")


def normalize_remote_path_under_root(remote_path: str, root: str, protocol: str) -> str:
    normalized_remote = _normalize_remote_path(remote_path, protocol)
    normalized_root = _normalize_remote_path(root, protocol)

    if protocol.lower() == "smb":
        remote_lower = normalized_remote.lower()
        root_lower = normalized_root.lower()
        if remote_lower != root_lower and not remote_lower.startswith(root_lower + "\\"):
            raise ValueError("file not under root")
        return normalized_remote

    root_prefix = normalized_root.rstrip("/")
    if normalized_remote != normalized_root and not normalized_remote.startswith(root_prefix + "/"):
        raise ValueError("file not under root")
    return normalized_remote


def validate_local_dir(path: str) -> Path:
    if not path or not path.strip():
        raise ValueError("local_dir is required")

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("local_dir must be an absolute path")

    resolved = candidate.resolve()
    resolved_text = str(resolved)
    for prefix in DENIED_PREFIXES:
        if resolved_text == prefix or resolved_text.startswith(prefix + os.sep):
            raise ValueError(f"Refusing to write under {prefix}")

    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"local_dir must be a directory: {resolved}")
        if not os.access(resolved, os.W_OK):
            raise ValueError(f"local_dir is not writable: {resolved}")
        return resolved

    parent = resolved.parent
    while not parent.exists():
        next_parent = parent.parent
        if next_parent == parent:
            break
        parent = next_parent
    if not parent.is_dir():
        raise ValueError(f"local_dir parent must be a directory: {parent}")
    if not os.access(parent, os.W_OK):
        raise ValueError(f"local_dir parent is not writable: {parent}")
    return resolved


def rel_under_root(file_path: str, root: str, protocol: str) -> str:
    file_norm = normalize_remote_path_under_root(file_path, root, protocol)
    root_norm = _normalize_remote_path(root, protocol)

    if protocol.lower() == "smb":
        remainder = file_norm[len(root_norm) :].lstrip("\\")
        relative = remainder.replace("\\", "/")
    else:
        relative = file_norm[len(root_norm) :].lstrip("/")

    if any(part == ".." for part in relative.split("/")):
        raise ValueError("file escapes root")
    return relative


def validate_remote_path_under_root(remote_path: str, root: str, protocol: str) -> None:
    try:
        normalize_remote_path_under_root(remote_path, root, protocol)
    except ValueError as exc:
        raise ValueError(f"Remote path must stay under session root: {remote_path}") from exc
