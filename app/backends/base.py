from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol


@dataclass
class Entry:
    name: str
    path: str
    type: str
    size: int | None = None


class DownloadCancelled(Exception):
    pass


class Backend(Protocol):
    def listdir(self, path: str) -> list[Entry]: ...

    def walk_files(self, path: str) -> list[tuple[str, int | None]]: ...

    def download_file(
        self, remote_path: str, local_path: Path, cancel_event: Event | None = None
    ) -> None: ...

    def close(self) -> None: ...


class FakeBackend:
    def __init__(self, tree: dict[str, list[Entry]], files: dict[str, bytes]) -> None:
        self._tree = {path: list(entries) for path, entries in tree.items()}
        self._files = dict(files)

    def listdir(self, path: str) -> list[Entry]:
        if path not in self._tree:
            raise KeyError(path)
        return list(self._tree[path])

    def walk_files(self, path: str) -> list[tuple[str, int | None]]:
        if path in self._files:
            return [(path, len(self._files[path]))]

        if path not in self._tree:
            raise KeyError(path)

        walked: list[tuple[str, int | None]] = []
        for entry in self.listdir(path):
            if entry.type == "dir":
                walked.extend(self.walk_files(entry.path))
            else:
                size = entry.size
                if entry.path in self._files:
                    size = len(self._files[entry.path])
                walked.append((entry.path, size))
        return walked

    def download_file(
        self, remote_path: str, local_path: Path, cancel_event: Event | None = None
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        if remote_path not in self._files:
            raise KeyError(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = local_path.with_name(local_path.name + ".part")
        try:
            partial_path.write_bytes(self._files[remote_path])
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            partial_path.replace(local_path)
        except BaseException:
            partial_path.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        return None
