"""Atomic, fail-closed video-reply preference and receive eligibility."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import json, os, re, threading
from pathlib import Path
from typing import Callable, Mapping

_KEY, _SCHEMA, _UNAVAILABLE = "video_reply_enabled", 1, "VIDEO_REPLY_SETTING_UNAVAILABLE"
_ID = re.compile(r"^video_reply_setting:[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

class VideoReplySettingsError(RuntimeError):
    def __init__(self, code: str, *, status: int = 503) -> None:
        self.code, self.status = code, status; super().__init__(code)

@dataclass(frozen=True)
class VideoReplySettingsSnapshot:
    state: str; enabled: bool | None = None; reason_code: str | None = None
    def __post_init__(self) -> None:
        if self.state not in {"available", "unavailable"} or (self.state == "available") != (type(self.enabled) is bool): raise ValueError("setting variant is invalid")
        if self.state == "available" and self.reason_code is not None or self.state == "unavailable" and (self.enabled is not None or not isinstance(self.reason_code, str)): raise ValueError("setting shape is invalid")
    def to_dict(self) -> dict[str, object]:
        key = "enabled" if self.state == "available" else "reason_code"; return {"state": self.state, key: self.enabled if key == "enabled" else self.reason_code}

@dataclass(frozen=True)
class VideoReplyReceiveEligibility:
    enabled: bool

@dataclass(frozen=True)
class VideoReplySettingMutation:
    request_id: str; status: str; enabled: bool
    def to_dict(self) -> dict[str, object]: return {"request_id": self.request_id, "status": self.status, "enabled": self.enabled}

def receive_eligibility_from_letter(letter: Mapping[str, object]) -> VideoReplyReceiveEligibility:
    value = letter.get(_KEY, True); return VideoReplyReceiveEligibility(type(value) is bool and value is True)

class VideoReplySettingsStore:
    """Own only the short setting transaction; no provider/router lock is exposed."""
    def __init__(self, root: Path, *, writer: Callable[[Path, bytes], None] | None = None) -> None:
        if not isinstance(root, Path) or not root.is_absolute(): raise VideoReplySettingsError(_UNAVAILABLE)
        self.path, self.marker = root / "video_reply_settings.json", root / "video_reply_settings.initialized"; self._writer, self._lock = writer or self._atomic_write, threading.Lock(); self._document = {}; self._committed = VideoReplySettingsSnapshot("unavailable", reason_code=_UNAVAILABLE); self._open()
    @classmethod
    def initialize(cls, root: Path, *, writer: Callable[[Path, bytes], None] | None = None) -> "VideoReplySettingsStore":
        if not isinstance(root, Path) or not root.is_absolute(): raise VideoReplySettingsError(_UNAVAILABLE)
        writer = writer or cls._atomic_write
        try:
            root.mkdir(parents=True, exist_ok=True); path, marker = root / "video_reply_settings.json", root / "video_reply_settings.initialized"
            if not path.exists():
                if marker.exists(): raise VideoReplySettingsError(_UNAVAILABLE)
                writer(path, cls._encode({"schema_version": _SCHEMA, "initialized": True, "settings": {_KEY: False}, "ledger": {}})); writer(marker, b"1\n")
        except (OSError, TypeError, ValueError): raise VideoReplySettingsError(_UNAVAILABLE) from None
        return cls(root, writer=writer)
    @classmethod
    def unavailable(cls) -> "VideoReplySettingsStore":
        item = cls.__new__(cls); item.path = item.marker = item._writer = None; item._lock, item._document = threading.Lock(), {}; item._committed = VideoReplySettingsSnapshot("unavailable", reason_code=_UNAVAILABLE); return item
    def snapshot(self) -> VideoReplySettingsSnapshot: return self._committed
    def receive_snapshot(self) -> VideoReplyReceiveEligibility: return VideoReplyReceiveEligibility(self._committed.enabled is True)
    def reload(self) -> None:
        with self._lock: self._open()
    @classmethod
    def validate_mutation(cls, request_id: object, enabled: object) -> None:
        cls._request(request_id)
        if type(enabled) is not bool: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_PAYLOAD_INVALID", status=400)
    def mutate(self, request_id: object, enabled: object) -> VideoReplySettingMutation:
        self.validate_mutation(request_id, enabled); request = self._request(request_id)
        with self._lock:
            if self._committed.state != "available": raise VideoReplySettingsError(_UNAVAILABLE)
            ledger = self._ledger(self._document); old = ledger.get(request)
            if old is not None:
                if not isinstance(old, Mapping) or type(old.get("enabled")) is not bool: raise VideoReplySettingsError(_UNAVAILABLE)
                if old["enabled"] is not enabled: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", status=409)
                result = old.get("result")
                if not isinstance(result, Mapping): raise VideoReplySettingsError(_UNAVAILABLE)
                return VideoReplySettingMutation(request, str(result["status"]), enabled)
            status = "NOOP" if self._committed.enabled is enabled else "APPLIED"; candidate = deepcopy(self._document); settings, ledger = candidate.setdefault("settings", {}), candidate.setdefault("ledger", {})
            if not isinstance(settings, dict) or not isinstance(ledger, dict): raise VideoReplySettingsError(_UNAVAILABLE)
            settings[_KEY] = enabled; ledger[request] = {"enabled": enabled, "result": {"status": status}}
            try: self._writer(self.path, self._encode(candidate))
            except (OSError, UnicodeError, TypeError, ValueError):
                self._committed = VideoReplySettingsSnapshot("unavailable", reason_code=_UNAVAILABLE); raise VideoReplySettingsError(_UNAVAILABLE) from None
            self._document, self._committed = candidate, VideoReplySettingsSnapshot("available", enabled=enabled); return VideoReplySettingMutation(request, status, enabled)
    def _open(self) -> None:
        try:
            document = self._read(); self._validate(document); settings = document.get("settings", {}); value = settings.get(_KEY, True) if isinstance(settings, Mapping) else None
            if type(value) is not bool: raise VideoReplySettingsError(_UNAVAILABLE)
        except (OSError, UnicodeError, TypeError, ValueError, VideoReplySettingsError): self._committed = VideoReplySettingsSnapshot("unavailable", reason_code=_UNAVAILABLE); return
        self._document, self._committed = document, VideoReplySettingsSnapshot("available", enabled=value)
    def _read(self) -> dict[str, object]:
        if self.path is None or not self.path.is_file(): raise VideoReplySettingsError(_UNAVAILABLE)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version", _SCHEMA) != _SCHEMA or not all(isinstance(document.get(name, {}), dict) for name in ("settings", "ledger")): raise VideoReplySettingsError(_UNAVAILABLE)
        if "initialized" in document and document["initialized"] is not True: raise VideoReplySettingsError(_UNAVAILABLE)
        if document.get("initialized") is True and (self.marker is None or not self.marker.is_file() or self.marker.read_text(encoding="utf-8") != "1\n"): raise VideoReplySettingsError(_UNAVAILABLE)
        return document
    @classmethod
    def _ledger(cls, document: Mapping[str, object]) -> dict[str, object]:
        ledger = document.get("ledger", {})
        if not isinstance(ledger, dict): raise VideoReplySettingsError(_UNAVAILABLE)
        return ledger
    @classmethod
    def _validate(cls, document: Mapping[str, object]) -> None:
        for request, record in cls._ledger(document).items():
            cls._request(request); result = record.get("result") if isinstance(record, Mapping) else None
            if not isinstance(record, Mapping) or type(record.get("enabled")) is not bool or not isinstance(result, Mapping) or result.get("status") not in {"APPLIED", "NOOP", "DUPLICATE"}: raise VideoReplySettingsError(_UNAVAILABLE)
    @staticmethod
    def _request(value: object) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value): raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_ID_INVALID", status=400)
        return value
    @staticmethod
    def _encode(document: Mapping[str, object]) -> bytes: return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        temp = path.with_name(f".{path.name}.tmp")
        try:
            with temp.open("wb") as handle: handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try: temp.unlink()
            except FileNotFoundError: pass

__all__ = ["VideoReplyReceiveEligibility", "VideoReplySettingMutation", "VideoReplySettingsError", "VideoReplySettingsSnapshot", "VideoReplySettingsStore", "receive_eligibility_from_letter"]
