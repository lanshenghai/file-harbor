from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread
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
    errors: list[str] = field(default_factory=list)


class DownloadManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobProgress] = {}
        self._inflight: dict[str, set[str]] = {}
        self._cancel_events: dict[str, Event] = {}
        self._futures: dict[str, set[Future[None]]] = {}
        self._lock = Lock()

    def start(self, backend: Backend, root, protocol, items, local_dir, workers=8) -> str:
        local_root = validate_local_dir(local_dir)
        worker_count = max(1, min(16, workers))
        job_id = uuid4().hex
        job = JobProgress(
            id=job_id,
            status="queued",
            total=0,
            done=0,
            current="",
            errors=[],
        )
        with self._lock:
            self._jobs[job_id] = job
            self._inflight[job_id] = set()
            self._cancel_events[job_id] = Event()
            self._futures[job_id] = set()

        thread = Thread(
            target=self._run_job,
            args=(job_id, job, backend, root, protocol, items, local_root, worker_count),
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
            return self._snapshot(job)

    @staticmethod
    def _snapshot(job: JobProgress) -> JobProgress:
        return JobProgress(
            id=job.id,
            status=job.status,
            total=job.total,
            done=job.done,
            current=job.current,
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
    ) -> None:
        cancel_event = self._cancel_events[job_id]
        try:
            if cancel_event.is_set():
                raise DownloadCancelled()
            files = self._expand_items(backend, items)
            if cancel_event.is_set():
                raise DownloadCancelled()
            self._update(job_id, status="running", total=len(files))

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
                    for remote_path in files
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
            # Backend lifecycle belongs to the caller/session layer.
            with self._lock:
                self._inflight.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._futures.pop(job_id, None)

    def _expand_items(self, backend: Backend, items: list[dict[str, str]]) -> list[str]:
        seen: set[str] = set()
        files: list[str] = []

        for item in items:
            remote_path = item["path"]
            item_type = item["type"]
            if item_type == "dir":
                candidates = [path for path, _ in backend.walk_files(remote_path)]
            elif item_type == "file":
                candidates = [remote_path]
            else:
                raise ValueError(f"Unknown item type: {item_type}")

            for path in candidates:
                if path in seen:
                    continue
                seen.add(path)
                files.append(path)

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
            backend.download_file(remote_path, local_root / relative, cancel_event)
            if cancel_event.is_set():
                raise DownloadCancelled()
        finally:
            self._end_download(job_id, remote_path)

    def _begin_download(self, job_id: str, remote_path: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._inflight[job_id].add(remote_path)
            job.current = remote_path

    def _end_download(self, job_id: str, remote_path: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            inflight = self._inflight[job_id]
            if remote_path in inflight:
                inflight.remove(remote_path)
            if job.current == remote_path:
                job.current = next(iter(inflight), "")

    def _mark_done(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id].done += 1

    def _record_error(self, job_id: str, error: str) -> None:
        with self._lock:
            self._jobs[job_id].errors.append(error)

    def _update(self, job_id: str, **changes: str | int) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
