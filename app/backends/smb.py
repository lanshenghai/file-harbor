from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Event, Lock
import time
from typing import Callable
from urllib.parse import quote

from app.pathutil import parse_unc

from .base import DownloadCancelled, Entry


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SMBCLIENT = _PROJECT_ROOT / ".tools" / "samba" / "usr" / "bin" / "smbclient"
_LISTING_RE = re.compile(
    r"^\s{2}(?P<name>.*?)\s+(?P<attrs>[A-Z]+)\s+(?P<size>\d+)\s+"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
)
_GVFS_LOCK = Lock()
_GVFS_FUSE_PROCESS: subprocess.Popen[bytes] | None = None


class GvfsSmbClient:
    """Use one persistent GVFS/libsmbclient session for concurrent file reads."""

    def __init__(
        self,
        *,
        host: str,
        share: str,
        username: str,
        password: str,
        domain: str,
    ) -> None:
        realm = domain.upper()
        if not realm:
            raise ValueError("SMB domain (Kerberos realm) is required")
        if "." not in realm:
            realm += ".NET"

        self.host = host
        self.share = share
        self.principal = f"{username}@{realm}"
        self.uri = f"smb://{quote(host)}/{quote(share)}"
        self._closed = False

        result = subprocess.run(
            ["kinit", "-f", self.principal],
            input=(password + "\n").encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise PermissionError(f"Kerberos authentication failed for {self.principal}: {detail}")

        self.mount_root = self._ensure_mount()

    def listdir(self, relative_path: str) -> list[tuple[str, bool, int]]:
        uri = self._uri_path(relative_path)
        try:
            result = subprocess.run(
                [
                    "gio",
                    "list",
                    "-a",
                    "standard::name,standard::type,standard::size",
                    uri,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConnectionError("GVFS SMB listing timed out after 30s") from exc
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise ConnectionError(f"GVFS SMB listing failed: {detail}")

        entries: list[tuple[str, bool, int]] = []
        for line in result.stdout.decode(errors="replace").splitlines():
            fields = line.rsplit("\t", 2)
            if len(fields) != 3:
                continue
            name, size_text, kind = fields
            is_dir = kind == "(directory)"
            entries.append((name, is_dir, int(size_text)))
        return entries

    def download(
        self,
        relative_path: str,
        local_path: Path,
        cancel_event: Event | None = None,
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        cancel_event = cancel_event or Event()
        partial_path = local_path.with_name(local_path.name + ".part")
        last_error: OSError | None = None

        for _ in range(4):
            if cancel_event.is_set():
                partial_path.unlink(missing_ok=True)
                raise DownloadCancelled()

            offset = partial_path.stat().st_size if partial_path.exists() else 0
            try:
                source_path = self._local_path(relative_path)
                with source_path.open("rb") as source:
                    source.seek(offset)
                    with partial_path.open("ab") as target:
                        while True:
                            if cancel_event.is_set():
                                partial_path.unlink(missing_ok=True)
                                raise DownloadCancelled()
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                partial_path.replace(local_path)
                                return
                            target.write(chunk)
                            if progress_callback is not None:
                                progress_callback(len(chunk))
            except DownloadCancelled:
                raise
            except OSError as exc:
                last_error = exc
                if isinstance(exc, FileNotFoundError):
                    try:
                        self.mount_root = self._ensure_mount()
                    except Exception:
                        pass
                time.sleep(1)

        assert last_error is not None
        raise ConnectionError(f"GVFS SMB download failed after retries: {last_error}") from last_error

    def close(self) -> None:
        self._closed = True

    def _ensure_mount(self) -> Path:
        global _GVFS_FUSE_PROCESS

        runtime_root = Path(f"/run/user/{os.getuid()}/gvfs")
        mount_root = runtime_root / f"smb-share:server={self.host.lower()},share={self.share.lower()}"
        with _GVFS_LOCK:
            mount = subprocess.run(
                ["gio", "mount", self.uri],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            mount_detail = (mount.stderr or mount.stdout).decode(errors="replace")
            if mount.returncode != 0 and "already mounted" not in mount_detail.lower():
                raise ConnectionError(f"GVFS SMB mount failed: {mount_detail.strip()}")

            if mount_root.is_dir():
                return mount_root

            runtime_root.mkdir(parents=True, exist_ok=True)
            fuse_binary = Path("/usr/libexec/gvfsd-fuse")
            if not fuse_binary.is_file():
                raise ConnectionError("GVFS FUSE bridge is not installed")
            if _GVFS_FUSE_PROCESS is None or _GVFS_FUSE_PROCESS.poll() is not None:
                _GVFS_FUSE_PROCESS = subprocess.Popen(
                    [str(fuse_binary), str(runtime_root), "-f", "-o", "big_writes"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            for _ in range(50):
                if mount_root.is_dir():
                    return mount_root
                time.sleep(0.1)

        raise ConnectionError(f"GVFS SMB mount is not visible at {mount_root}")

    def _local_path(self, relative_path: str) -> Path:
        parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError("SMB path contains an unsafe segment")
        return self.mount_root.joinpath(*parts)

    def _uri_path(self, relative_path: str) -> str:
        parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise ValueError("SMB path contains an unsafe segment")
        suffix = "/".join(quote(part, safe="") for part in parts)
        return f"{self.uri}/{suffix}" if suffix else self.uri


class NativeSmbClient:
    """Run the native Samba client with a private Kerberos credential cache."""

    def __init__(
        self,
        *,
        host: str,
        share: str,
        username: str,
        password: str,
        domain: str,
        binary: Path = _DEFAULT_SMBCLIENT,
    ) -> None:
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ConnectionError(
                f"Native Samba client is not installed. Run {_PROJECT_ROOT / 'install_samba_client.sh'}"
            )

        self.host = host
        self.share = share
        self.binary = binary
        realm = domain.upper()
        if not realm:
            raise ValueError("SMB domain (Kerberos realm) is required")
        if "." not in realm:
            realm += ".NET"
        self.principal = f"{username}@{realm}"
        self._closed = False

        fd, cache_path = tempfile.mkstemp(prefix="file-harbor-krb5cc-")
        os.close(fd)
        os.unlink(cache_path)
        self.cache_path = Path(cache_path)

        try:
            result = subprocess.run(
                ["kinit", "-f", "-c", str(self.cache_path), self.principal],
                input=(password + "\n").encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except Exception:
            self.close()
            raise

        if result.returncode != 0:
            self.close()
            detail = result.stderr.decode(errors="replace").strip()
            raise PermissionError(f"Kerberos authentication failed for {self.principal}: {detail}")

    def listdir(self, relative_path: str) -> list[tuple[str, bool, int]]:
        pattern = self._join(relative_path, "*")
        last_error: ConnectionError | None = None
        for attempt in range(4):
            try:
                output = self._run(f'ls "{self._quote(pattern)}"', timeout=20)
                break
            except ConnectionError as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(1)
        else:
            assert last_error is not None
            raise last_error

        entries: list[tuple[str, bool, int]] = []
        for line in output.splitlines():
            match = _LISTING_RE.match(line)
            if match is None:
                continue
            name = match.group("name").rstrip()
            if name in {".", ".."}:
                continue
            is_dir = "D" in match.group("attrs")
            size = int(match.group("size"))
            entries.append((name, is_dir, size))
        return entries

    def download(
        self,
        relative_path: str,
        local_path: Path,
        cancel_event: Event | None = None,
        progress_callback: Callable[[int], None] | None = None,
    ) -> None:
        partial_path = local_path.with_name(local_path.name + ".part")
        last_error: ConnectionError | None = None

        try:
            for _ in range(4):
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled()
                action = "reget" if partial_path.exists() and partial_path.stat().st_size else "get"
                command = (
                    "timeout 600; iosize 262144; "
                    f'{action} "{self._quote(relative_path)}" "{self._quote(str(partial_path))}"'
                )
                try:
                    self._run(command, timeout=3600, cancel_event=cancel_event)
                except ConnectionError as exc:
                    last_error = exc
                    continue

                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled()
                partial_path.replace(local_path)
                if progress_callback is not None:
                    progress_callback(local_path.stat().st_size)
                return
        except DownloadCancelled:
            partial_path.unlink(missing_ok=True)
            raise

        assert last_error is not None
        raise last_error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.cache_path.unlink(missing_ok=True)
        except AttributeError:
            if self.cache_path.exists():
                self.cache_path.unlink()

    def _run(
        self, command: str, *, timeout: int = 60, cancel_event: Event | None = None
    ) -> str:
        if self._closed:
            raise ConnectionError("SMB session is closed")

        env = os.environ.copy()
        env["KRB5CCNAME"] = f"FILE:{self.cache_path}"
        args = [
            str(self.binary),
            "-s",
            "/dev/null",
            "--use-kerberos=required",
            "-N",
            "-U",
            self.principal,
            f"//{self.host}/{self.share}",
            "-c",
            command,
        ]
        if cancel_event is None:
            try:
                result = subprocess.run(
                    args,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ConnectionError(f"Native SMB command timed out after {timeout}s") from exc
            stdout_bytes = result.stdout
            stderr_bytes = result.stderr
            returncode = result.returncode
        else:
            process = subprocess.Popen(
                args,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise DownloadCancelled()
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise ConnectionError(f"Native SMB command timed out after {timeout}s")
                time.sleep(0.05)
            stdout_bytes, stderr_bytes = process.communicate()
            returncode = process.returncode

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        if returncode == 0:
            return stdout

        detail = (stderr or stdout).strip()
        upper = detail.upper()
        if any(code in upper for code in ("NT_STATUS_LOGON_FAILURE", "NT_STATUS_ACCESS_DENIED")):
            raise PermissionError(f"SMB access denied for {self.principal}: {detail}")
        if any(
            code in upper
            for code in (
                "NT_STATUS_NO_SUCH_FILE",
                "NT_STATUS_OBJECT_NAME_NOT_FOUND",
                "NT_STATUS_OBJECT_PATH_NOT_FOUND",
                "NT_STATUS_BAD_NETWORK_NAME",
            )
        ):
            raise FileNotFoundError(detail)
        raise ConnectionError(f"Native SMB command failed: {detail}")

    @staticmethod
    def _join(parent: str, child: str) -> str:
        return parent.rstrip("\\/") + "\\" + child if parent else child

    @staticmethod
    def _quote(value: str) -> str:
        if any(char in value for char in ("\0", "\r", "\n")):
            raise ValueError("SMB path contains an unsupported control character")
        return value.replace('"', '\\"')


class SmbBackend:
    def __init__(
        self,
        *,
        host: str,
        root: str,
        username: str,
        password: str,
        domain: str = "",
    ) -> None:
        if not host.strip():
            raise ValueError("SMB host is required")
        if not root.strip():
            raise ValueError("SMB root is required")
        if not username.strip() or not password:
            raise ConnectionError("SMB username and password are required")

        root_parts = parse_unc(root)
        if root_parts.host.lower() != host.lower():
            raise ValueError("SMB root host does not match connection host")

        self.host = host
        self.root = root
        self.share = root_parts.share
        self._client = GvfsSmbClient(
            host=host,
            share=self.share,
            username=username,
            password=password,
            domain=domain,
        )
        try:
            self.listdir(root)
        except Exception:
            self.close()
            raise

    def listdir(self, path: str) -> list[Entry]:
        relative = self._relative(path)
        entries = []
        for name, is_dir, size in self._client.listdir(relative):
            entries.append(
                Entry(
                    name=name,
                    path=self._join(path, name),
                    type="dir" if is_dir else "file",
                    size=None if is_dir else size,
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
        self._client.download(
            self._relative(remote_path),
            local_path,
            cancel_event,
            progress_callback=progress_callback,
        )

    def close(self) -> None:
        self._client.close()

    def _relative(self, path: str) -> str:
        parts = parse_unc(path)
        if parts.host.lower() != self.host.lower() or parts.share.lower() != self.share.lower():
            raise ValueError("SMB path is outside the connected share")
        return parts.rel_path.replace("/", "\\")

    @staticmethod
    def _join(parent: str, child: str) -> str:
        return parent.rstrip("\\/") + "\\" + child
