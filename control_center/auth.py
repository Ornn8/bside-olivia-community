"""In-memory, loopback-only sessions for the local Control Center."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets
from threading import RLock
import time
from typing import Callable


CONTROL_SESSION_COOKIE = "olivia_control_session"
CONTROL_CSRF_HEADER = "X-CSRF-Token"


class ControlAuthError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class SessionCredentials:
    session_token: str
    csrf_token: str
    expires_at: float


@dataclass(frozen=True)
class _Session:
    csrf_digest: str
    expires_at: float


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ControlSessionManager:
    def __init__(
        self,
        *,
        bootstrap_ttl_seconds: float = 120.0,
        idle_timeout_seconds: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if bootstrap_ttl_seconds <= 0 or idle_timeout_seconds <= 0:
            raise ValueError("control session TTLs must be positive")
        self.bootstrap_ttl_seconds = float(bootstrap_ttl_seconds)
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self._clock = clock
        self._bootstrap: dict[str, float] = {}
        self._sessions: dict[str, _Session] = {}
        self._lock = RLock()

    def _prune(self, now: float) -> None:
        self._bootstrap = {
            key: expires
            for key, expires in self._bootstrap.items()
            if expires > now
        }
        self._sessions = {
            key: session
            for key, session in self._sessions.items()
            if session.expires_at > now
        }

    def issue_bootstrap_token(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._bootstrap[_digest(token)] = (
                now + self.bootstrap_ttl_seconds
            )
        return token

    def bootstrap(self, token: str) -> SessionCredentials:
        if not isinstance(token, str) or not token:
            raise ControlAuthError("CONTROL_BOOTSTRAP_INVALID")
        now = self._clock()
        with self._lock:
            self._prune(now)
            expires = self._bootstrap.pop(_digest(token), None)
            if expires is None or expires <= now:
                raise ControlAuthError("CONTROL_BOOTSTRAP_INVALID")
            session_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            session_expires = now + self.idle_timeout_seconds
            self._sessions[_digest(session_token)] = _Session(
                _digest(csrf_token),
                session_expires,
            )
        return SessionCredentials(
            session_token,
            csrf_token,
            session_expires,
        )

    def authenticate(self, session_token: str | None) -> float:
        if not isinstance(session_token, str) or not session_token:
            raise ControlAuthError("CONTROL_SESSION_REQUIRED")
        now = self._clock()
        session_digest = _digest(session_token)
        with self._lock:
            self._prune(now)
            session = self._sessions.get(session_digest)
            if session is None or session.expires_at <= now:
                raise ControlAuthError("CONTROL_SESSION_INVALID")
            expires = now + self.idle_timeout_seconds
            self._sessions[session_digest] = _Session(
                session.csrf_digest,
                expires,
            )
        return expires

    def validate_csrf(
        self,
        session_token: str | None,
        csrf_token: str | None,
    ) -> None:
        if not isinstance(session_token, str) or not session_token:
            raise ControlAuthError("CONTROL_SESSION_REQUIRED")
        if not isinstance(csrf_token, str) or not csrf_token:
            raise ControlAuthError("CONTROL_CSRF_REQUIRED")
        now = self._clock()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(_digest(session_token))
            if session is None or session.expires_at <= now:
                raise ControlAuthError("CONTROL_SESSION_INVALID")
            if not hmac.compare_digest(
                session.csrf_digest,
                _digest(csrf_token),
            ):
                raise ControlAuthError("CONTROL_CSRF_INVALID")

    def logout(self, session_token: str | None) -> None:
        if not isinstance(session_token, str) or not session_token:
            return
        with self._lock:
            self._sessions.pop(_digest(session_token), None)

    def status(self) -> dict[str, int | str]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            sessions = len(self._sessions)
            bootstraps = len(self._bootstrap)
        return {
            "status": "READY",
            "active_sessions": sessions,
            "pending_bootstraps": bootstraps,
        }


__all__ = [
    "CONTROL_CSRF_HEADER",
    "CONTROL_SESSION_COOKIE",
    "ControlAuthError",
    "ControlSessionManager",
    "SessionCredentials",
]
