from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Callable
from uuid import uuid4

from app.backends import Backend, DownloadCancelled
from app.pathutil import rel_under_root, validate_local_dir


@dataclass
class JobProgress:
    id: str
    status: str
    total: int
    done: int
    current: str
    protocol: str
    workers: int
    total_bytes: int
    bytes_done: int
    speed_bps: float
    paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RetryContext:
    backend: Backend | None
    root: str
    host: str
    protocol: str
    local_dir: str
    workers: int
    items: list[dict[str, str]]
    backend_owned: bool = False
    expanded_files: list[str] = field(default_factory=list)


class DownloadManager:
    def __init__(
        self,
        state_file: str | Path | None = None,
        backend_factory: Callable[[str, str, str], Backend] | None = None,
    ) -> None:
        self._jobs: dict[str, JobProgress] = {}
        self._inflight: dict[str, set[str]] = {}
        self._cancel_events: dict[str, Event] = {}
        self._futures: dict[str, set[Future[None]]] = {}
        self._retry_contexts: dict[str, RetryContext] = {}
        self._failed_paths: dict[str, set[str]] = {}
        self._job_started_at: dict[str, float] = {}
        self._deleted_job_ids: set[str] = set()
        self._lock = Lock()
        self._state_file = Path(state_file) if state_file is not None else None
        self._backend_factory = backend_factory
        self._load_state()

    def start(
        self,
        backend: Backend,
        root,
        protocol,
        items,
        local_dir,
        workers=8,
        host: str = "",
        backend_owned: bool = False,
    ) -> str:
        local_root = validate_local_dir(local_dir)
        requested_workers = max(1, min(16, workers))
        worker_count = min(4, requested_workers) if protocol.lower() == "smb" else requested_workers
        job_id = uuid4().hex
        job = JobProgress(
            id=job_id,
            status="queued",
            total=0,
            done=0,
            current="",
            protocol=protocol.lower(),
            workers=worker_count,
            total_bytes=0,
            bytes_done=0,
            speed_bps=0.0,
            paths=[],
            errors=[],
        )
        with self._lock:
            self._jobs[job_id] = job
            self._inflight[job_id] = set()
            self._cancel_events[job_id] = Event()
            self._futures[job_id] = set()
            self._failed_paths[job_id] = set()
            self._retry_contexts[job_id] = RetryContext(
                backend=None if backend_owned else backend,
                root=root,
                host=host,
                protocol=protocol,
                local_dir=str(local_root),
                workers=worker_count,
                items=[dict(item) for item in items],
                backend_owned=backend_owned,
            )
            self._persist_state_locked()

        thread = Thread(
            target=self._run_job,
            args=(job_id, job, backend, root, protocol, items, local_root, worker_count, backend_owned),
            daemon=True,
        )
        thread.start()
        return job_id

    def get(self, job_id: str) -> JobProgress:
        with self._lock:
            return self._snapshot(self._jobs[job_id])

    def list(self) -> list[JobProgress]:
        with self._lock:
            return [self._snapshot(job) for job in reversed(self._jobs.values())]

    def cancel(self, job_id: str) -> JobProgress:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {"done", "done_with_errors", "failed", "cancelled"}:
                return self._snapshot(job)
            job.status = "cancelling"
            self._cancel_events[job_id].set()
            for future in self._futures[job_id]:
                future.cancel()
            self._persist_state_locked()
            return self._snapshot(job)

    def retry(self, job_id: str) -> str:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {"queued", "running", "cancelling"}:
                raise ValueError("Retry is available after the job finishes")
            context = self._retry_contexts.get(job_id)
            if context is None:
                raise ValueError("Retry is unavailable after service restart; start a new download")
            failed_paths = sorted(self._failed_paths[job_id])
            expanded_files = list(context.expanded_files)

        backend = context.backend
        backend_owned = False
        if backend is None:
            backend = self._create_backend_for_retry(context)
            backend_owned = True

        if failed_paths:
            retry_items = [{"path": path, "type": "file"} for path in failed_paths]
        elif expanded_files:
            retry_items = [{"path": path, "type": "file"} for path in expanded_files]
        else:
            retry_items = [dict(item) for item in context.items]

        return self.start(
            backend=backend,
            root=context.root,
            protocol=context.protocol,
            items=retry_items,
            local_dir=context.local_dir,
            workers=context.workers,
            host=context.host,
            backend_owned=backend_owned,
        )

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {"queued", "running", "cancelling"}:
                raise ValueError("Cannot remove an active job")
            self._jobs.pop(job_id, None)
            self._inflight.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            self._futures.pop(job_id, None)
            self._retry_contexts.pop(job_id, None)
            self._failed_paths.pop(job_id, None)
            self._deleted_job_ids.add(job_id)
            self._persist_state_locked()

    @staticmethod
    def _snapshot(job: JobProgress) -> JobProgress:
        return JobProgress(
            id=job.id,
            status=job.status,
            total=job.total,
            done=job.done,
            current=job.current,
            protocol=job.protocol,
            workers=job.workers,
            total_bytes=job.total_bytes,
            bytes_done=job.bytes_done,
            speed_bps=job.speed_bps,
            paths=list(job.paths),
            errors=list(job.errors),
        )

    def _run_job(
        self,
        job_id: str,
        job: JobProgress,
        backend: Backend,
        root: str,
        protocol: str,
        items: list[dict[str, str]],
        local_root: Path,
        workers: int,
        backend_owned: bool,
    ) -> None:
        cancel_event = self._cancel_events[job_id]
        try:
            if cancel_event.is_set():
                raise DownloadCancelled()
            files = self._expand_items(backend, items)
            self._set_expanded_files(job_id, [path for path, _ in files])
            total_bytes = sum(size for _, size in files if isinstance(size, int))
            if cancel_event.is_set():
                raise DownloadCancelled()
            self._update(
                job_id,
                status="running",
                total=len(files),
                total_bytes=total_bytes,
                bytes_done=0,
                speed_bps=0.0,
            )
            with self._lock:
                self._job_started_at[job_id] = time.monotonic()

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._download_one,
                        job_id,
                        backend,
                        root,
                        protocol,
                        remote_path,
                        local_root,
                        cancel_event,
                    ): remote_path
                    for remote_path, _ in files
                }
                with self._lock:
                    self._futures[job_id] = set(futures)
                    if cancel_event.is_set():
                        for future in futures:
                            future.cancel()
                for future in as_completed(futures):
                    remote_path = futures[future]
                    try:
                        future.result()
                    except (CancelledError, DownloadCancelled):
                        continue
                    except Exception as exc:
                        self._mark_failed_path(job_id, remote_path)
                        self._record_error(job_id, f"{remote_path}: {exc}")
                    else:
                        self._mark_done(job_id)

            if cancel_event.is_set():
                status = "cancelled"
            else:
                final = self.get(job_id)
                status = "done_with_errors" if final.errors else "done"
            self._update(job_id, status=status, current="")
        except DownloadCancelled:
            self._update(job_id, status="cancelled", current="")
        except Exception as exc:
            self._update(job_id, status="failed", current="")
            self._record_error(job_id, str(exc))
        finally:
            if backend_owned:
                try:
                    backend.close()
                except Exception:
                    pass
            # Backend lifecycle belongs to the caller/session layer.
            with self._lock:
                self._inflight.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._futures.pop(job_id, None)
                self._job_started_at.pop(job_id, None)

    def _expand_items(self, backend: Backend, items: list[dict[str, str]]) -> list[tuple[str, int | None]]:
        seen: set[str] = set()
        files: list[tuple[str, int | None]] = []

        for item in items:
            remote_path = item["path"]
            item_type = item["type"]
            if item_type == "dir":
                candidates = backend.walk_files(remote_path)
            elif item_type == "file":
                try:
                    candidates = backend.walk_files(remote_path)
                except Exception:
                    candidates = [(remote_path, None)]
            else:
                raise ValueError(f"Unknown item type: {item_type}")

            for path, size in candidates:
                if path in seen:
                    continue
                seen.add(path)
                files.append((path, size))

        return files

    def _download_one(
        self,
        job_id: str,
        backend: Backend,
        root: str,
        protocol: str,
        remote_path: str,
        local_root: Path,
        cancel_event: Event,
    ) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled()
        self._begin_download(job_id, remote_path)
        try:
            relative = rel_under_root(remote_path, root, protocol)
            local_path = local_root / relative
            progress_callback = lambda n: self._add_bytes(job_id, n)
            try:
                backend.download_file(
                    remote_path,
                    local_path,
                    cancel_event,
                    progress_callback=progress_callback,
                )
            except TypeError as exc:
                if "progress_callback" not in str(exc):
                    raise
                # Compatibility fallback for custom test backends.
                backend.download_file(remote_path, local_path, cancel_event)
                if local_path.exists():
                    self._add_bytes(job_id, local_path.stat().st_size)
            if cancel_event.is_set():
                raise DownloadCancelled()
        finally:
            self._end_download(job_id, remote_path)

    def _begin_download(self, job_id: str, remote_path: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._inflight[job_id].add(remote_path)
            job.current = remote_path
            self._persist_state_locked()

    def _end_download(self, job_id: str, remote_path: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            inflight = self._inflight[job_id]
            if remote_path in inflight:
                inflight.remove(remote_path)
            if job.current == remote_path:
                job.current = next(iter(inflight), "")
            self._persist_state_locked()

    def _mark_done(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id].done += 1
            self._persist_state_locked()

    def _add_bytes(self, job_id: str, byte_count: int) -> None:
        if byte_count <= 0:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.bytes_done += byte_count
            started_at = self._job_started_at.get(job_id)
            if started_at is not None:
                elapsed = max(time.monotonic() - started_at, 1e-6)
                job.speed_bps = job.bytes_done / elapsed

    def _record_error(self, job_id: str, error: str) -> None:
        with self._lock:
            self._jobs[job_id].errors.append(error)
            self._persist_state_locked()

    def _mark_failed_path(self, job_id: str, remote_path: str) -> None:
        with self._lock:
            self._failed_paths[job_id].add(remote_path)
            self._persist_state_locked()

    def _set_expanded_files(self, job_id: str, files: list[str]) -> None:
        with self._lock:
            self._retry_contexts[job_id].expanded_files = list(files)
            self._jobs[job_id].paths = list(files)
            self._persist_state_locked()

    def _update(self, job_id: str, **changes: str | int | float) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            self._persist_state_locked()

    def _load_state(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        raw_jobs = payload.get("jobs")
        raw_retry_contexts = payload.get("retry_contexts")
        raw_deleted_job_ids = payload.get("deleted_job_ids")
        if not isinstance(raw_jobs, list):
            return
        if not isinstance(raw_retry_contexts, dict):
            raw_retry_contexts = {}
        if isinstance(raw_deleted_job_ids, list):
            self._deleted_job_ids = {str(job_id) for job_id in raw_deleted_job_ids}
        else:
            self._deleted_job_ids = set()

        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            raw_id = raw.get("id")
            if raw_id is not None and str(raw_id) in self._deleted_job_ids:
                continue
            try:
                job = JobProgress(
                    id=str(raw["id"]),
                    status=str(raw["status"]),
                    total=int(raw["total"]),
                    done=int(raw["done"]),
                    current=str(raw.get("current", "")),
                    protocol=str(raw.get("protocol", "")),
                    workers=int(raw.get("workers", 1)),
                    total_bytes=int(raw.get("total_bytes", 0)),
                    bytes_done=int(raw.get("bytes_done", 0)),
                    speed_bps=float(raw.get("speed_bps", 0.0)),
                    paths=[str(path) for path in raw.get("paths", []) if isinstance(path, str)],
                    errors=[str(item) for item in raw.get("errors", [])],
                )
            except Exception:
                continue
            if job.status in {"queued", "running", "cancelling"}:
                job.status = "failed"
                job.current = ""
                job.errors.append("Service restarted while this job was still running")
            self._jobs[job.id] = job
            self._inflight[job.id] = set()
            self._failed_paths[job.id] = self._extract_failed_paths(job.errors)
            raw_context = raw_retry_contexts.get(job.id)
            if isinstance(raw_context, dict):
                self._retry_contexts[job.id] = RetryContext(
                    backend=None,
                    root=str(raw_context.get("root", "")),
                    host=str(raw_context.get("host", "")),
                    protocol=str(raw_context.get("protocol", job.protocol)),
                    local_dir=str(raw_context.get("local_dir", "")),
                    workers=int(raw_context.get("workers", job.workers)),
                    items=[dict(item) for item in raw_context.get("items", []) if isinstance(item, dict)],
                    backend_owned=bool(raw_context.get("backend_owned", False)),
                    expanded_files=[
                        str(path) for path in raw_context.get("expanded_files", []) if isinstance(path, str)
                    ],
                )
                if not job.paths:
                    job.paths = list(self._retry_contexts[job.id].expanded_files)

    def _persist_state_locked(self) -> None:
        if self._state_file is None:
            return
        payload = {
            "jobs": [
                {
                    "id": job.id,
                    "status": job.status,
                    "total": job.total,
                    "done": job.done,
                    "current": job.current,
                    "protocol": job.protocol,
                    "workers": job.workers,
                    "total_bytes": job.total_bytes,
                    "bytes_done": job.bytes_done,
                    "speed_bps": job.speed_bps,
                    "paths": list(job.paths),
                    "errors": list(job.errors),
                }
                for job in self._jobs.values()
            ],
            "retry_contexts": {
                job_id: {
                    "root": context.root,
                    "host": context.host,
                    "protocol": context.protocol,
                    "local_dir": context.local_dir,
                    "workers": context.workers,
                    "items": [dict(item) for item in context.items],
                    "expanded_files": list(context.expanded_files),
                    "backend_owned": context.backend_owned,
                }
                for job_id, context in self._retry_contexts.items()
            },
            "deleted_job_ids": sorted(self._deleted_job_ids),
        }
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        temp_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        temp_file.replace(self._state_file)

    @staticmethod
    def _extract_failed_paths(errors: list[str]) -> set[str]:
        paths: set[str] = set()
        for error in errors:
            if ": " not in error:
                continue
            candidate, _ = error.split(": ", 1)
            if candidate.startswith("/") or candidate.startswith("\\\\"):
                paths.add(candidate)
        return paths

    def _create_backend_for_retry(self, context: RetryContext) -> Backend:
        if self._backend_factory is None:
            raise ValueError("Retry is unavailable after service restart; start a new download")
        return self._backend_factory(context.protocol, context.host, context.root)
