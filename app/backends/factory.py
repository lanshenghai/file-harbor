from __future__ import annotations

from .base import Backend
from .sftp import SftpBackend
from .smb import SmbBackend


def open_backend(
    protocol: str,
    username: str,
    password: str,
    domain: str,
    host: str,
    root: str,
) -> Backend:
    kind = protocol.lower()
    if kind == "smb":
        return SmbBackend(
            host=host,
            root=root,
            username=username,
            password=password,
            domain=domain,
        )
    if kind == "sftp":
        return SftpBackend(
            host=host,
            root=root,
            username=username,
            password=password,
        )
    raise ValueError(f"Unsupported backend protocol: {protocol}")
