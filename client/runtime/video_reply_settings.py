"""Atomic, fail-closed video-reply preference and receive eligibility."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import json, os, re, threading
from pathlib import Path
from typing import Callable, Mapping

_KEY, _SCHEMA, _UNAVAILABLE = "video_reply_enabled", 1, "VIDEO_REPLY_SETTING_UNAVAILABLE"
_ID = re.compile(r"^video_reply_setting:[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
REPLY_ROUTES = ("voice_reply", "singing_video", "voice_song_video")
DEFAULT_ROUTE_VIDEOS = {"voice_reply": False, "singing_video": True, "voice_song_video": True}
REPLY_TIERS = ("text", "audio", "video")
DEFAULT_IMAGE = {"enabled": True, "resolution": "1K"}
_IMAGE_MODEL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def image_model_capability(capabilities):
    """The cloud advertises only tested models with approved retail prices."""
    image = capabilities.get('image') if isinstance(capabilities, dict) else None
    if not isinstance(image, dict) or 'models' not in image:
        return None  # Old servers keep their existing default-image behavior.
    models, seen = [], set()
    items = image['models']
    if not isinstance(items, list) or len(items) > 32:
        return {'models': []}
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not _IMAGE_MODEL_ID.fullmatch(item['id']) or item['id'] in seen
                or not isinstance(item.get('display_name'), str) or not 1 <= len(item['display_name'].strip()) <= 80
                or not isinstance(item.get('resolutions'), list) or not item['resolutions']
                or any(value not in ('1K', '2K', '4K') for value in item['resolutions'])
                or len(set(item['resolutions'])) != len(item['resolutions'])):
            return {'models': []}
        seen.add(item['id'])
        model = {key: deepcopy(item[key]) for key in ('id', 'display_name', 'resolutions')}
        if 'price_ranges_cents' in item:
            prices = item['price_ranges_cents']
            if (not isinstance(prices, dict) or set(prices) != set(item['resolutions'])
                    or any(not isinstance(value, list) or len(value) != 2
                           or any(type(amount) is not int for amount in value)
                           or not 0 <= value[0] <= value[1] <= 2**53 - 1
                           for value in prices.values())):
                return {'models': []}
            model['price_ranges_cents'] = deepcopy(prices)
        models.append(model)
    result = {'models': models}
    if isinstance(image.get('default_model'), str) and image['default_model'] in seen:
        result['default_model'] = image['default_model']
    return result


def require_image_model(image, capabilities):
    if 'model' not in image:
        return
    catalog = image_model_capability(capabilities)
    if not catalog or not any(item['id'] == image['model'] and image.get('resolution') in item['resolutions']
                              for item in catalog['models']):
        raise VideoReplySettingsError('IMAGE_MODEL_UNAVAILABLE', status=503)

def tier_preferences(tier):
    if not isinstance(tier, str) or tier not in REPLY_TIERS:
        raise VideoReplySettingsError("VIDEO_REPLY_SETTING_PAYLOAD_INVALID", status=400)
    return dict.fromkeys(REPLY_ROUTES, tier != "text"), dict.fromkeys(REPLY_ROUTES, tier == "video")

def routed_video(mode, contexts, ceilings):
    if not ceilings.get(mode, False) or mode == "text_letter": return False
    if "explicit_audio_output_request" in contexts: return False
    if {"explicit_video_reply_request", "explicit_video_output_request"}.intersection(contexts): return True
    return mode in {"singing_video", "voice_song_video", "musical_video"}

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
    def routes_configured(self) -> bool:
        return "routes" in self._document.get("settings", {})
    def routes_snapshot(self) -> dict[str, bool]:
        with self._lock:
            if self._committed.state != "available":
                raise VideoReplySettingsError(_UNAVAILABLE)
            saved = self._document.get("settings", {}).get("routes")
            return dict(saved) if saved is not None else dict.fromkeys(REPLY_ROUTES, self._committed.enabled is True)
    def videos_snapshot(self) -> dict[str, bool]:
        with self._lock:
            if self._committed.state != "available": raise VideoReplySettingsError(_UNAVAILABLE)
            return dict(self._document.get("settings", {}).get("videos", DEFAULT_ROUTE_VIDEOS))
    def saved_tier(self):
        return self._document.get("settings", {}).get("tier")
    def image_snapshot(self):
        with self._lock:
            if self._committed.state != 'available':
                return {'enabled': False, 'resolution': '1K'}
            # Photos are on until the user turns them off; an explicit choice is stored and kept.
            image = dict(self._document.get('settings', {}).get('image', DEFAULT_IMAGE))
            style = self._document.get('settings', {}).get('wardrobe', {'style_id': 'original'})['style_id']
            if style != 'original': image['wardrobe_style'] = style
            return image
    def wardrobe_snapshot(self):
        with self._lock:
            if self._committed.state != 'available': raise VideoReplySettingsError(_UNAVAILABLE)
            return dict(self._document.get('settings', {}).get('wardrobe', {'style_id': 'original'}))
    def mutate_wardrobe(self, request_id, value):
        from runtime.wardrobe import validate
        request = self._request(request_id)
        try: value = validate(value)
        except ValueError: raise VideoReplySettingsError('WARDROBE_STYLE_INVALID', status=400) from None
        with self._lock:
            if self._committed.state != 'available': raise VideoReplySettingsError(_UNAVAILABLE)
            old = self._ledger(self._document).get(request)
            if old is not None:
                if old.get('wardrobe') != value: raise VideoReplySettingsError('VIDEO_REPLY_SETTING_REQUEST_CONFLICT', status=409)
                return {'status': 'DUPLICATE', 'wardrobe': value}
            candidate = deepcopy(self._document)
            candidate['settings']['wardrobe'] = value
            candidate.setdefault('ledger', {})[request] = {'enabled': self._committed.enabled,
                'wardrobe': value, 'result': {'status': 'APPLIED'}}
            try: self._writer(self.path, self._encode(candidate))
            except (OSError, UnicodeError, TypeError, ValueError): raise VideoReplySettingsError(_UNAVAILABLE) from None
            self._document = candidate
            return {'status': 'APPLIED', 'wardrobe': dict(value)}
    def mutate_image(self, request_id, image):
        request = self._request(request_id)
        self._validate_image(image)
        with self._lock:
            if self._committed.state != 'available': raise VideoReplySettingsError(_UNAVAILABLE)
            old = self._ledger(self._document).get(request)
            if old is not None:
                if old.get('image') != image: raise VideoReplySettingsError('VIDEO_REPLY_SETTING_REQUEST_CONFLICT', status=409)
                return {'status': 'DUPLICATE', 'image': dict(image)}
            candidate = deepcopy(self._document)
            candidate['settings']['image'] = dict(image)
            candidate.setdefault('ledger', {})[request] = {'enabled': self._committed.enabled, 'image': dict(image), 'result': {'status': 'APPLIED'}}
            try: self._writer(self.path, self._encode(candidate))
            except (OSError, ValueError): raise VideoReplySettingsError(_UNAVAILABLE) from None
            self._document = candidate
            return {'status': 'APPLIED', 'image': dict(image)}
    @staticmethod
    def _validate_image(image):
        if (not isinstance(image, dict) or not {'enabled', 'resolution'} <= set(image)
                or set(image) - {'enabled', 'resolution', 'model'}
                or type(image['enabled']) is not bool or image['resolution'] not in ('1K', '2K', '4K')
                or ('model' in image and (not isinstance(image['model'], str) or not _IMAGE_MODEL_ID.fullmatch(image['model'])))):
            raise VideoReplySettingsError('VIDEO_REPLY_SETTING_PAYLOAD_INVALID', status=400)
    def tier_snapshot(self):
        routes, videos = self.routes_snapshot(), self.videos_snapshot()
        return self.saved_tier() or ("video" if any(routes[k] and videos[k] for k in REPLY_ROUTES) else "audio" if any(routes.values()) else "text")
    def mutate_tier(self, request_id, tier, *, image=None):
        routes, videos = tier_preferences(tier)
        return self.mutate_routes(request_id, routes, videos, tier=tier, image=image)
    def mutate_routes(self, request_id: object, routes: object, videos: object = None, *, tier=None, image=None) -> dict[str, object]:
        request = self._request(request_id)
        if image is not None: self._validate_image(image)
        if not isinstance(routes, dict) or set(routes) != set(REPLY_ROUTES) or any(type(v) is not bool for v in routes.values()):
            raise VideoReplySettingsError("VIDEO_REPLY_SETTING_PAYLOAD_INVALID", status=400)
        if videos is not None and (not isinstance(videos, dict) or set(videos) != set(REPLY_ROUTES) or any(type(v) is not bool for v in videos.values())):
            raise VideoReplySettingsError("VIDEO_REPLY_SETTING_PAYLOAD_INVALID", status=400)
        with self._lock:
            if self._committed.state != "available": raise VideoReplySettingsError(_UNAVAILABLE)
            ledger = self._ledger(self._document)
            old = ledger.get(request)
            videos = dict(videos) if videos is not None else dict(self._document.get("settings", {}).get("videos", DEFAULT_ROUTE_VIDEOS))
            if old is not None:
                if old.get("tier") != tier: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", status=409)
                if old.get("routes") != routes or old.get("videos", DEFAULT_ROUTE_VIDEOS) != videos: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", status=409)
                if image is not None and old.get('image') != image: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", status=409)
                return {"status": "DUPLICATE", "routes": dict(routes), "videos": videos, "tier": tier, **({'image': dict(image)} if image is not None else {})}
            candidate = deepcopy(self._document)
            candidate["settings"].update(routes=dict(routes), video_reply_enabled=any(routes.values()))
            candidate["settings"]["videos"] = videos
            if tier is None: candidate["settings"].pop("tier", None)
            else: candidate["settings"]["tier"] = tier
            if image is not None: candidate["settings"]["image"] = dict(image)
            candidate.setdefault("ledger", {})[request] = {"enabled": any(routes.values()), "routes": dict(routes), "result": {"status": "APPLIED"}}
            candidate["ledger"][request]["videos"] = videos
            if tier is not None: candidate["ledger"][request]["tier"] = tier
            if image is not None: candidate["ledger"][request]["image"] = dict(image)
            try: self._writer(self.path, self._encode(candidate))
            except (OSError, UnicodeError, TypeError, ValueError): raise VideoReplySettingsError(_UNAVAILABLE) from None
            self._document, self._committed = candidate, VideoReplySettingsSnapshot("available", enabled=any(routes.values()))
            return {"status": "APPLIED", "routes": dict(routes), "videos": videos, "tier": tier, **({'image': dict(image)} if image is not None else {})}
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
                if "routes" in old or "wardrobe" in old: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", status=409)
                if old["enabled"] is not enabled: raise VideoReplySettingsError("VIDEO_REPLY_SETTING_REQUEST_CONFLICT", status=409)
                result = old.get("result")
                if not isinstance(result, Mapping): raise VideoReplySettingsError(_UNAVAILABLE)
                return VideoReplySettingMutation(request, str(result["status"]), enabled)
            status = "NOOP" if self._committed.enabled is enabled else "APPLIED"; candidate = deepcopy(self._document); settings, ledger = candidate.setdefault("settings", {}), candidate.setdefault("ledger", {})
            if not isinstance(settings, dict) or not isinstance(ledger, dict): raise VideoReplySettingsError(_UNAVAILABLE)
            settings[_KEY] = enabled; ledger[request] = {"enabled": enabled, "result": {"status": status}}
            settings.pop("tier", None)
            if "routes" in settings: settings["routes"] = dict.fromkeys(REPLY_ROUTES, enabled)
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
        if 'wardrobe' in document.get('settings', {}):
            from runtime.wardrobe import validate
            validate(document['settings']['wardrobe'])
        if 'image' in document.get('settings', {}): cls._validate_image(document['settings']['image'])
        tier = document.get("settings", {}).get("tier")
        if tier is not None:
            expected_routes, expected_videos = tier_preferences(tier)
            if document["settings"].get("routes") != expected_routes or document["settings"].get("videos") != expected_videos:
                raise VideoReplySettingsError(_UNAVAILABLE)
        videos = document.get("settings", {}).get("videos", DEFAULT_ROUTE_VIDEOS)
        if not isinstance(videos, dict) or set(videos) != set(REPLY_ROUTES) or any(type(v) is not bool for v in videos.values()):
            raise VideoReplySettingsError(_UNAVAILABLE)
        routes = document.get("settings", {}).get("routes")
        if routes is not None and (not isinstance(routes, dict) or set(routes) != set(REPLY_ROUTES) or any(type(v) is not bool for v in routes.values())):
            raise VideoReplySettingsError(_UNAVAILABLE)
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
