from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import uuid


@dataclass
class Session:
    id: str
    protocol: str
    username: str
    password: str
    domain: str
    host: str
    root: str
    last_used: float


class SessionStore:
    def __init__(self, idle_ttl: float = 3600) -> None:
        self._idle_ttl = idle_ttl
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def _is_expired(self, session: Session, now: float) -> bool:
        return now - session.last_used > self._idle_ttl

    def create(
        self,
        *,
        protocol: str,
        username: str,
        password: str,
        domain: str,
        host: str,
        root: str,
    ) -> Session:
        session = Session(
            id=uuid.uuid4().hex,
            protocol=protocol,
            username=username,
            password=password,
            domain=domain,
            host=host,
            root=root,
            last_used=time.monotonic(),
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, id: str) -> Session:
        with self._lock:
            session = self._sessions.get(id)
            if session is None:
                raise KeyError(id)
            if self._is_expired(session, time.monotonic()):
                del self._sessions[id]
                raise KeyError(id)
            return session

    def touch(self, id: str) -> None:
        with self._lock:
            session = self._sessions.get(id)
            if session is None:
                raise KeyError(id)
            now = time.monotonic()
            if self._is_expired(session, now):
                del self._sessions[id]
                raise KeyError(id)
            session.last_used = now

    def delete(self, id: str) -> None:
        with self._lock:
            if id not in self._sessions:
                raise KeyError(id)
            del self._sessions[id]

    def sweep_expired(self) -> list[str]:
        now = time.monotonic()
        expired_ids: list[str] = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                if self._is_expired(session, now):
                    expired_ids.append(session_id)
                    del self._sessions[session_id]
        return expired_ids
