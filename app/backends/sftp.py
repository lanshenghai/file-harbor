from __future__ import annotations

from pathlib import Path
import socket
import stat
from threading import Event, Lock
from typing import Callable

import paramiko

from .base import DownloadCancelled, Entry


class SftpBackend:
    def __init__(
        self,
        *,
        host: str,
        root: str,
        username: str,
        password: str,
    ) -> None:
        if not host.strip():
            raise ValueError("SFTP host is required")
        if not root.strip():
            raise ValueError("SFTP root is required")
        if not username.strip() or not password:
            raise ConnectionError("SFTP username and password are required")

        self.host = host
        self.root = root
        self.username = username
        self._sftp_lock = Lock()
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            self._ssh.connect(
                hostname=self.host,
                port=22,
                username=username,
                password=password,
            )
            self._sftp = self._ssh.open_sftp()
            self.listdir(self.root)
        except FileNotFoundError as exc:
            self.close()
            raise ValueError(f"SFTP root does not exist: {self.root}") from exc
        except ValueError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise self._classify_init_error(exc) from exc

    def listdir(self, path: str) -> list[Entry]:
        entries: list[Entry] = []
        with self._sftp_lock:
            listed = list(self._sftp.listdir_attr(path))
        for attr in listed:
            is_dir = self._is_dir(attr.st_mode)
            entries.append(
                Entry(
                    name=attr.filename,
                    path=self._join(path, attr.filename),
                    type="dir" if is_dir else "file",
                    size=None if is_dir else getattr(attr, "st_size", None),
                )
            )
        return entries

    def walk_files(self, path: str) -> list[tuple[str, int | None]]:
        files: list[tuple[str, int | None]] = []
        for entry in self.listdir(path):
            if entry.type == "dir":
                files.extend(self.walk_files(entry.path))
            else:
                files.append((entry.path, entry.size))
        return files

    def download_file(
        self,
        remote_path: str,
        local_path: Path,
        cancel_event: Event | None = None,
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = local_path.with_name(local_path.name + ".part")
        try:
            with self._sftp_lock:
                with self._sftp.open(remote_path, "rb") as source, partial_path.open("wb") as target:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled()
                        chunk = source.read(256 * 1024)
                        if not chunk:
                            break
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled()
                        target.write(chunk)
                        if progress_callback is not None:
                            progress_callback(len(chunk))
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            partial_path.replace(local_path)
        except BaseException:
            partial_path.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        sftp = getattr(self, "_sftp", None)
        if sftp is not None:
            sftp.close()
            self._sftp = None

        ssh = getattr(self, "_ssh", None)
        if ssh is not None:
            ssh.close()
            self._ssh = None

    def _classify_init_error(self, exc: Exception) -> Exception:
        if self._is_auth_error(exc):
            return PermissionError(
                f"SFTP authentication failed for {self.username}@{self.host}"
            )
        if self._is_unreachable_error(exc):
            return ConnectionError(f"SFTP host unreachable: {self.host}")
        return ConnectionError(f"Unable to access SFTP root {self.root}: {exc}")

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        auth_type = getattr(paramiko, "AuthenticationException", None)
        if auth_type is not None and isinstance(exc, auth_type):
            return True
        return exc.__class__.__name__ == "AuthenticationException"

    @staticmethod
    def _is_unreachable_error(exc: Exception) -> bool:
        ssh_exception = getattr(paramiko, "ssh_exception", paramiko)
        no_valid_type = getattr(ssh_exception, "NoValidConnectionsError", None)
        unreachable_types = tuple(
            t for t in (no_valid_type, TimeoutError, socket.timeout, OSError) if t is not None
        )
        return isinstance(exc, unreachable_types) or exc.__class__.__name__ in {
            "NoValidConnectionsError",
        }

    @staticmethod
    def _join(parent: str, child: str) -> str:
        return parent.rstrip("/") + "/" + child

    @staticmethod
    def _is_dir(mode: int) -> bool:
        checker = getattr(paramiko, "S_ISDIR", stat.S_ISDIR)
        return checker(mode)
