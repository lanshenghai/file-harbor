from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Event
import time

from app.pathutil import parse_unc

from .base import DownloadCancelled, Entry


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SMBCLIENT = _PROJECT_ROOT / ".tools" / "samba" / "usr" / "bin" / "smbclient"
_LISTING_RE = re.compile(
    r"^\s{2}(?P<name>.*?)\s+(?P<attrs>[A-Z]+)\s+(?P<size>\d+)\s+"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
)


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
        self, relative_path: str, local_path: Path, cancel_event: Event | None = None
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
        self._client = NativeSmbClient(
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
        self, remote_path: str, local_path: Path, cancel_event: Event | None = None
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download(self._relative(remote_path), local_path, cancel_event)

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
