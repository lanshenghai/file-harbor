from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
import argparse
import os
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.backends import Backend
from app.backends.factory import open_backend
from app.credentials import load_saved_credentials
from app.download import DownloadManager
from app.pathutil import (
    normalize_remote_path_under_root,
    normalize_remote_root,
    validate_local_dir,
)
from app.session import Session, SessionStore

SESSION_SWEEP_INTERVAL = 60.0
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ENV_FILE = STATIC_DIR.parent / ".env"


def load_project_env(path: str | Path = ENV_FILE) -> None:
    load_dotenv(dotenv_path=path, override=False)


load_project_env()

SESSIONS = SessionStore(idle_ttl=3600)
DOWNLOADS = DownloadManager()
_BACKENDS: dict[str, Backend] = {}
_BACKENDS_LOCK = Lock()


class ConnectRequest(BaseModel):
    protocol: Literal["smb", "sftp"]
    remote_path: str = Field(min_length=1)


class DownloadItem(BaseModel):
    path: str = Field(min_length=1)
    type: Literal["file", "dir"]


class DownloadRequest(BaseModel):
    session_id: str = Field(min_length=1)
    items: list[DownloadItem] = Field(min_length=1)
    local_dir: str = Field(min_length=1)
    workers: int | None = None


def _resolve_connect_credentials(protocol: str) -> tuple[str, str, str]:
    saved = load_saved_credentials()
    if saved is None:
        raise ValueError(
            "Missing ~/.smbcredentials (need username/password/domain) or SMB_PASS env"
        )
    domain = saved.domain if protocol == "smb" else ""
    return saved.username, saved.password, domain

def _close_backend(session_id: str) -> None:
    with _BACKENDS_LOCK:
        backend = _BACKENDS.pop(session_id, None)
    if backend is None:
        return
    try:
        backend.close()
    except Exception:
        # Best-effort cleanup for per-session connections.
        return


def _store_backend(session_id: str, backend: Backend) -> None:
    with _BACKENDS_LOCK:
        old_backend = _BACKENDS.get(session_id)
        _BACKENDS[session_id] = backend
    if old_backend is not None and old_backend is not backend:
        try:
            old_backend.close()
        except Exception:
            return


def _get_session(session_id: str) -> Session:
    try:
        return SESSIONS.get(session_id)
    except KeyError as exc:
        _close_backend(session_id)
        raise HTTPException(status_code=404, detail="Unknown or expired session") from exc


def _get_backend(session_id: str) -> Backend:
    with _BACKENDS_LOCK:
        backend = _BACKENDS.get(session_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    return backend


def _touch_session(session_id: str) -> None:
    try:
        SESSIONS.touch(session_id)
    except KeyError as exc:
        _close_backend(session_id)
        raise HTTPException(status_code=404, detail="Unknown or expired session") from exc


def _sweep_expired_sessions() -> None:
    for session_id in SESSIONS.sweep_expired():
        _close_backend(session_id)


def _session_sweeper_loop(stop_event: Event) -> None:
    while not stop_event.wait(SESSION_SWEEP_INTERVAL):
        _sweep_expired_sessions()


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_event = Event()
    sweeper = Thread(target=_session_sweeper_loop, args=(stop_event,), daemon=True)
    sweeper.start()
    try:
        yield
    finally:
        stop_event.set()
        sweeper.join(timeout=1)
        _sweep_expired_sessions()
        with _BACKENDS_LOCK:
            session_ids = list(_BACKENDS)
        for session_id in session_ids:
            _close_backend(session_id)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ValueError)
async def handle_value_error(_: object, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(PermissionError)
async def handle_permission_error(_: object, exc: PermissionError):
    return JSONResponse({"detail": str(exc)}, status_code=401)


@app.exception_handler(ConnectionError)
async def handle_connection_error(_: object, exc: ConnectionError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.post("/api/connect")
def connect(body: ConnectRequest) -> dict[str, str]:
    username, password, domain = _resolve_connect_credentials(body.protocol)
    if body.protocol != "smb":
        domain = ""
    normalized = normalize_remote_root(body.protocol, body.remote_path, None)
    backend = open_backend(
        body.protocol,
        username=username,
        password=password,
        domain=domain if body.protocol == "smb" else "",
        host=normalized["host"],
        root=normalized["root"],
    )
    session = SESSIONS.create(
        protocol=body.protocol,
        username=username,
        password=password,
        domain=domain if body.protocol == "smb" else "",
        host=normalized["host"],
        root=normalized["root"],
    )
    _store_backend(session.id, backend)
    return {
        "session_id": session.id,
        "root": session.root,
        "host": session.host,
        "protocol": session.protocol,
    }

@app.get("/api/tree")
def tree(session_id: str = Query(...), path: str | None = Query(None)) -> list[dict[str, object]]:
    session = _get_session(session_id)
    backend = _get_backend(session_id)
    try:
        target_path = normalize_remote_path_under_root(path or session.root, session.root, session.protocol)
    except ValueError as exc:
        raise ValueError(f"Remote path must stay under session root: {path or session.root}") from exc
    try:
        entries = backend.listdir(target_path)
    except (FileNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=f"Remote path not found: {target_path}") from exc
    _touch_session(session_id)
    return [asdict(entry) for entry in entries]


@app.post("/api/download")
def download(body: DownloadRequest) -> dict[str, str]:
    session = _get_session(body.session_id)
    backend = _get_backend(body.session_id)
    validate_local_dir(body.local_dir)
    normalized_items = []
    for item in body.items:
        try:
            normalized_path = normalize_remote_path_under_root(item.path, session.root, session.protocol)
        except ValueError as exc:
            raise ValueError(f"Remote path must stay under session root: {item.path}") from exc
        normalized_items.append({"path": normalized_path, "type": item.type})
    workers = body.workers if body.workers is not None else 8
    job_id = DOWNLOADS.start(
        backend=backend,
        root=session.root,
        protocol=session.protocol,
        items=normalized_items,
        local_dir=body.local_dir,
        workers=workers,
    )
    _touch_session(body.session_id)
    return {"job_id": job_id}


@app.get("/api/downloads")
def list_downloads() -> list[dict[str, object]]:
    return [asdict(progress) for progress in DOWNLOADS.list()]


@app.post("/api/download/{job_id}/cancel")
def cancel_download(job_id: str) -> dict[str, object]:
    try:
        progress = DOWNLOADS.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown job") from exc
    return asdict(progress)


@app.get("/api/download/{job_id}")
def download_status(job_id: str, session_id: str | None = Query(None)) -> dict[str, object]:
    if session_id is not None:
        _touch_session(session_id)
    try:
        progress = DOWNLOADS.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown job") from exc
    return asdict(progress)


@app.delete("/api/session/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    _close_backend(session_id)
    try:
        SESSIONS.delete(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown or expired session") from exc
    return Response(status_code=204)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the File Harbor web app.")
    parser.add_argument(
        "--host",
        help="Listen address (default 0.0.0.0 so other PCs can reach this machine).",
    )
    parser.add_argument("--port", type=int, help="Listen port for the server.")
    args = parser.parse_args()

    host = args.host or os.environ.get("FILE_HARBOR_HOST") or "0.0.0.0"
    env_port = os.environ.get("FILE_HARBOR_PORT")
    port = args.port
    if port is None:
        port = int(env_port) if env_port else 8088
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
