# Olivia 本地 toy API 兼容层（仅本地运行）
# 运行: python local_server.py   (监听 127.0.0.1:8899)
# 前端 patch: getClientConfig 的 toyApiUrl -> http://127.0.0.1:8899
#
# 本地适配点（按配置接入模型）:
#   letter_adapter.reply(content)  -> 本地 LLM 生成回信
#   music_adapter.generate(midi)   -> 本地音乐模型生成演奏
import asyncio
import json
import os as _os
import re as _re
import random
import time
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from aiohttp import web

import http_contract as contract
from asr.config import AsrConfig
from asr.errors import AsrError
from asr.provider import NemotronProvider, create_provider
from llm_gateway import (
    Gateway,
    GatewayConfig,
    GatewayDelta,
    GatewayError,
    GatewayResponse,
    ProviderTimeout,
    ProviderUnavailable,
    UnconfiguredAdapter,
    api_key_configured,
    create_gateway,
    load_gateway_config,
)
from persona_provider import (
    CompositePersonaEvidencePort,
    ConfigPersonaProvider,
    FilePersonaProvider,
    JsonPersonaEvidencePort,
    MemoryReferenceEvidencePort,
    persona_status,
)
from reply_orchestrator import ReplyOrchestrator, ReplyRequest, ReplyState
from letter_triage import LetterEmotionTriage
from music_reply import MusicReplyError, render_musical_reply
from reply_media import ReplyMediaError, render_reply_video
from local_memory import create_memory_adapter
from memory_port import LegacyLetter, MemoryPort, NullMemoryPort
from memory_prompt import MemoryPromptBuilder
from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from private_world_port import NullPrivateWorldPort, PrivateWorldPort, PrivateWorldSnapshot
from private_world_delivery import (
    DeliveryEvent,
    DeliveryStatus,
    PrivateWorldDeliveryCommitter,
)
from private_world_ledger import SQLitePrivateWorldLedger
from private_world_reducer import ReducerEventKind
from private_world_projection import project_private_world
from reply_context import (
    ReplyContext,
    ReplyMode,
    TrustedTime,
    TrustedWorldFact,
    WorldFactKind,
)
from reply_pipeline import ReplyPipeline, UnavailableRewriter
from reply_reviewer import NullReviewer

PORT = int(_os.environ.get("OLIVIA_PORT", "8899"))
LLM_TIMEOUT_SECONDS = 30
LETTER_RETRY_DEDUP_SECONDS = 60


def _exact_reply_mode(value: object) -> str:
    """Normalize legacy wire values without losing the new internal mode."""

    if isinstance(value, ReplyMode):
        return value.value
    normalized = str(value or "").strip().lower()
    if normalized in {"text", ReplyMode.TEXT_LETTER.value}:
        return ReplyMode.TEXT_LETTER.value
    if normalized == ReplyMode.SPOKEN_VIDEO.value:
        return ReplyMode.SPOKEN_VIDEO.value
    if normalized in {"video", ReplyMode.MUSICAL_VIDEO.value}:
        # Before P03 every video reply was rendered by the musical path.
        return ReplyMode.MUSICAL_VIDEO.value
    return ReplyMode.TEXT_LETTER.value


def _wire_reply_mode(value: object) -> str:
    exact = _exact_reply_mode(value)
    return "text" if exact == ReplyMode.TEXT_LETTER.value else "video"


def _safe_log(event: str, **fields) -> None:
    """Emit structured diagnostics without request bodies, URLs or user data."""
    record = {"event": event, **fields}
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))


def _diagnostic_code(prefix: str, exc: Exception) -> str:
    name = type(exc).__name__.upper()
    name = _re.sub(r"[^A-Z0-9]+", "_", name).strip("_") or "ERROR"
    return f"{prefix}_{name}"[:64]

LLM_CONFIG: GatewayConfig = load_gateway_config()
LLM_TIMEOUT_SECONDS = LLM_CONFIG.timeout_seconds
LLM_CFG = LLM_CONFIG.public_dict()
LLM_CFG["persona_file"] = LLM_CONFIG.persona_file


def _persona() -> str:
    """Compatibility accessor that always exposes the draft marker."""

    configured = LLM_CFG.get("persona_file") or LLM_CONFIG.persona_file
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return FilePersonaProvider(path).snapshot().system_prompt


class LLMError(RuntimeError):
    """A stable local error category with no provider details."""

    def __init__(self, code: str = "LLM_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


class _LetterGateway(Gateway):
    """Bridge the legacy sync facade to the async reply orchestrator."""

    def __init__(self, adapter: "LetterAdapter") -> None:
        self.adapter = adapter
        self.stream_enabled = bool(adapter.config.stream)

    async def complete(self, messages, *, request_id=None) -> GatewayResponse:
        content = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        try:
            text = await asyncio.to_thread(self.adapter.reply, content, "")
        except LLMError as exc:
            if exc.code == "LLM_TIMEOUT":
                raise ProviderTimeout() from None
            if exc.code == "LLM_PROVIDER_REJECTED":
                raise GatewayError(exc.code, retryable=False) from None
            if exc.code == "LLM_PROTOCOL_ERROR":
                raise GatewayError(exc.code, retryable=False) from None
            raise ProviderUnavailable() from None
        return GatewayResponse(
            text=text,
            request_id=request_id or uuid.uuid4().hex,
            provider=LLM_CONFIG.provider,
            model=LLM_CONFIG.model,
        )

    async def stream(self, messages, *, request_id=None):
        content = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        built_messages = await asyncio.to_thread(self.adapter._messages, content)
        async for delta in self.adapter.gateway.stream(built_messages, request_id=request_id):
            yield GatewayDelta(
                delta.text,
                delta.request_id,
                index=delta.index,
                finish_reason=delta.finish_reason,
            )


class LetterAdapter:
    """Compatibility facade used by B02 tests and local integrations."""

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        memory_port: MemoryPort | None = None,
        private_world_port: PrivateWorldPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or LLM_CONFIG
        try:
            self.gateway = create_gateway(self.config)
        except GatewayError:
            self.gateway = UnconfiguredAdapter()
        self.memory_port: MemoryPort = memory_port or NullMemoryPort()
        self.private_world_port: PrivateWorldPort = (
            private_world_port or NullPrivateWorldPort()
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        persona_path = Path(self.config.persona_file)
        if not persona_path.is_absolute():
            persona_path = Path(__file__).resolve().parent / persona_path
        persona_config_path = Path(self.config.persona_config)
        if not persona_config_path.is_absolute():
            persona_config_path = Path(__file__).resolve().parent / persona_config_path
        persona_evidence_path = Path(self.config.persona_evidence_file)
        if not persona_evidence_path.is_absolute():
            persona_evidence_path = Path(__file__).resolve().parent / persona_evidence_path
        persona_v2_path = Path(self.config.persona_v2_file)
        if not persona_v2_path.is_absolute():
            persona_v2_path = Path(__file__).resolve().parent / persona_v2_path
        self.persona_v2_path = persona_v2_path
        self.persona_provider = ConfigPersonaProvider(
            persona_config_path,
            draft_path=persona_path,
            evidence_port=CompositePersonaEvidencePort(
                JsonPersonaEvidencePort(persona_evidence_path),
                MemoryReferenceEvidencePort(self.memory_port),
            ),
            feature_enabled=self.config.feature_enabled,
        )
        self.memory_prompt_builder = MemoryPromptBuilder(self.memory_port)

    def get_system_prompt(self) -> str:
        return self.persona_provider.snapshot().system_prompt

    def public_persona_status(self) -> dict[str, str | None]:
        if self.config.persona_v2_enabled:
            loaded = load_persona(self.persona_v2_path)
            return {
                "status": loaded.snapshot.status,
                "source": loaded.snapshot.source,
                "error_code": loaded.error_code.value if loaded.error_code else None,
            }
        legacy = persona_status(self.persona_provider)
        return {
            "status": str(legacy.get("status", "DRAFT")),
            "source": str(legacy.get("source", "file")),
            "error_code": None,
        }

    def get_initial_messages(self) -> list[dict[str, str]]:
        # No legacy letter samples or hidden few-shot material are loaded.
        return []

    def _messages(self, content: str, context: str = "") -> tuple[dict[str, str], ...]:
        if self.config.persona_v2_enabled:
            return self._persona_v2_messages(content, context)
        return self._legacy_messages(content, context)

    def _legacy_messages(
        self, content: str, context: str = ""
    ) -> tuple[dict[str, str], ...]:
        user_content = content
        if context:
            user_content = content + "\n\n" + context
        snapshot = self.persona_provider.snapshot()
        remaining = max(
            0,
            self.config.max_input_chars - len(snapshot.system_prompt) - len(user_content) - 2,
        )
        if remaining:
            memory_context = self.memory_prompt_builder.build(
                content,
                max_chars=min(
                    remaining,
                    int(getattr(self.memory_port, "context_max_chars", 2400)),
                ),
            )
            if memory_context.text:
                user_content = user_content + "\n\n" + memory_context.text
        return self.persona_provider.messages_for(
            user_content,
            max_chars=self.config.max_input_chars,
        )

    def _persona_v2_messages(
        self, content: str, context: str = ""
    ) -> tuple[dict[str, str], ...]:
        user_content = content + ("\n\n" + context if context else "")
        loaded = load_persona(self.persona_v2_path)
        reply_context = self.build_reply_context(ReplyMode.TEXT_LETTER)
        memory_context = self.memory_prompt_builder.build(
            content,
            max_chars=min(
                self.config.max_input_chars,
                int(getattr(self.memory_port, "context_max_chars", 2400)),
            ),
        )
        history = (
            (UntrustedFragment("memory.references", memory_context.text),)
            if memory_context.text
            else ()
        )
        return assemble_persona(
            loaded.snapshot,
            reply_context,
            user_input=user_content,
            max_units=self.config.max_input_chars,
            history=history,
        ).to_messages()

    def build_reply_context(self, mode: ReplyMode) -> ReplyContext:
        try:
            private_snapshot = self.private_world_port.snapshot()
            if not isinstance(private_snapshot, PrivateWorldSnapshot):
                private_snapshot = PrivateWorldSnapshot()
        except Exception:
            private_snapshot = PrivateWorldSnapshot()
        projected = project_private_world(private_snapshot)
        facts = tuple(
            TrustedWorldFact(
                fact_id=f"private.nickname.{index}",
                source_id="private_world.character_view",
                statement=f"Currently authorized nickname: {nickname}",
                kind=WorldFactKind.TRUSTED_RUNTIME,
            )
            for index, nickname in enumerate(projected.authorized_nicknames)
        )
        if projected.continuation_known:
            facts += (
                TrustedWorldFact(
                    fact_id="private.continuation.known",
                    source_id="private_world.character_view",
                    statement="Local continuation is known to the character.",
                    kind=WorldFactKind.TRUSTED_RUNTIME,
                ),
            )
        return ReplyContext.create(
            mode,
            trusted_time=TrustedTime(self._now()),
            world_facts=facts,
            private_behavior=projected.behavior,
        )

    def remember_conversation(self, content: str, reply: str) -> None:
        """Write new-chat memory only when the opt-in profile is enabled."""

        if not getattr(self.memory_port, "conversation_enabled", False):
            return
        try:
            self.memory_port.remember_conversation(
                f"User sent a new letter: {str(content)[:4000]}",
                facts=(f"Assistant completed a reply: {str(reply)[:4000]}",),
            )
        except Exception:
            _safe_log("memory_write_skipped", reason="optional_backend_unavailable")

    def reply(self, content: str, context: str = "") -> str:
        try:
            messages = self._messages(content, context)
            return asyncio.run(self.gateway.complete(messages)).text
        except GatewayError as exc:
            code = "LLM_TIMEOUT" if isinstance(exc, ProviderTimeout) else "LLM_UNAVAILABLE"
            if exc.code == "PROVIDER_REJECTED":
                code = "LLM_PROVIDER_REJECTED"
            elif exc.code == "PROVIDER_PROTOCOL":
                code = "LLM_PROTOCOL_ERROR"
            _safe_log("llm_failure", provider=self.config.provider, error_code=code)
            raise LLMError(code) from None
        except (ValueError, RuntimeError):
            _safe_log("llm_failure", provider=self.config.provider, error_code="LLM_UNAVAILABLE")
            raise LLMError("LLM_UNAVAILABLE") from None


class MusicAdapter:
    """MIDI 生成边界：没有实现就明确 NOT_IMPLEMENTED，不伪造处理中。"""

    def submit(self, midi_url: str, filename: str) -> dict:
        return {
            "job_id": str(uuid.uuid4()),
            "state": 5,
            "status": "NOT_IMPLEMENTED",
            "error_code": "MIDI_NOT_IMPLEMENTED",
            "filename": filename,
        }

# ---------------------------------------------------------------------------
# 数据存储（内存，可加文件持久化）
# ---------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.uid = 200717
        self.letters = []      # {letter_id, content, material, reply_text, ...}
        self.legacy_letters = []  # read-only imported view; never used by send/reply
        self.midi_jobs = []    # {job_id, state, filename, created_at}
        self.settings = {}
        self.request_keys = {}

store = Store()


def _state_root() -> Path | None:
    configured = _os.environ.get("OLIVIA_LOCAL_DATA_ROOT", "")
    return Path(configured).expanduser().resolve() if configured else None


def _load_store_state() -> None:
    root = _state_root()
    if root is None:
        return
    try:
        loaded = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    needs_persist = False
    for name in ("letters", "legacy_letters", "midi_jobs"):
        value = loaded.get(name)
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            setattr(store, name, value)
            if name == "letters":
                for item in value:
                    item["reply_mode"] = _exact_reply_mode(
                        item.get("reply_mode", ReplyMode.TEXT_LETTER.value)
                    )
                    if item.get("letter_status") == "PROCESSING":
                        item["letter_status"] = "FAILED"
                        item["error_code"] = "LLM_INTERRUPTED"
                        needs_persist = True
                    if item.get("media_status") == "PROCESSING":
                        item["media_status"] = "QUEUED"
                        needs_persist = True
    if isinstance(loaded.get("settings"), dict):
        store.settings = loaded["settings"]
    if isinstance(loaded.get("request_keys"), dict):
        store.request_keys = loaded["request_keys"]
    if needs_persist:
        _persist_store_state()


def _persist_store_state() -> None:
    root = _state_root()
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "letters": store.letters,
        "legacy_letters": store.legacy_letters,
        "midi_jobs": store.midi_jobs,
        "settings": store.settings,
        "request_keys": store.request_keys,
    }
    temporary = root / ".state.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(root / "state.json")


_load_store_state()
memory_adapter: MemoryPort = create_memory_adapter()
private_world_committer: PrivateWorldDeliveryCommitter | None = None
private_world_port: PrivateWorldPort = NullPrivateWorldPort()
_private_world_path = Path(_os.environ.get("OLIVIA_PRIVATE_WORLD_DB", ""))
if _private_world_path.is_absolute():
    try:
        _private_world_ledger = SQLitePrivateWorldLedger(_private_world_path)
        private_world_port = _private_world_ledger
        private_world_committer = PrivateWorldDeliveryCommitter(_private_world_ledger)
    except (OSError, ValueError, RuntimeError):
        pass
letters_adapter = LetterAdapter(
    memory_port=memory_adapter,
    private_world_port=private_world_port,
)
emotion_triage = LetterEmotionTriage(letters_adapter.gateway)
media_semaphore = asyncio.Semaphore(1)
media_tasks: set[asyncio.Task] = set()
reply_tasks: set[asyncio.Task] = set()
reply_jobs: dict[str, asyncio.Task] = {}


def _persist_media_state() -> None:
    _persist_store_state()
music_adapter = MusicAdapter()
reply_engine = ReplyOrchestrator(
    _LetterGateway(letters_adapter),
    timeout_seconds=LLM_CONFIG.timeout_seconds,
)
reply_pipeline = ReplyPipeline(
    reply_engine,
    reviewer=NullReviewer(),
    rewriter=UnavailableRewriter(),
)

# ---------------------------------------------------------------------------
# 响应工具
# ---------------------------------------------------------------------------
def ok(data=None):
    return contract.ok(data)

def err(code, msg, data=None):
    payload = dict(data or {})
    status = payload.pop("status", "FAILED")
    error_code = payload.pop("error_code", msg)
    return contract.error(
        code,
        error_code,
        msg,
        status=status,
        details=payload or None,
    )


def not_implemented(error_code: str = "ROUTE_NOT_IMPLEMENTED"):
    return contract.not_implemented(error_code)


HTTP_STATUS_BY_CODE = {
    0: 200,
    400: 400,
    403: 403,
    404: 404,
    405: 405,
    409: 409,
    415: 415,
    500: 500,
    501: 501,
    503: 503,
}


def response_http_status(result: dict) -> int:
    """Translate the local API result code into the actual HTTP status."""
    return HTTP_STATUS_BY_CODE.get(result.get("code"), 500)


def fake_jwt():
    # 本地伪造 token（客户端不校验，服务器自己验证）
    return "toy__local." + uuid.uuid4().hex

def letter_to_out(l):
    published = l.get("reply_not_before", 0.0) <= time.time()
    return {
        "letter_id": l["letter_id"],
        "summary": (l.get("reply_text") or l.get("content") or "")[:50],
        "letter_status": l.get("letter_status", 4) if published else "PENDING",
        "audit_status": l.get("audit_status", 2),
        "reply_type": 1 if published and l.get("reply_text") else 0,
        "reply_mode": _wire_reply_mode(l.get("reply_mode")) if published else "text",
        "reply_mode_exact": (
            _exact_reply_mode(l.get("reply_mode"))
            if published
            else ReplyMode.TEXT_LETTER.value
        ),
        "triage": l.get("triage", {"status": "unavailable"}),
        "is_read": l.get("is_read", 1),
        "created_at": l.get("created_at", int(time.time())),
    }

# ---------------------------------------------------------------------------
# B02 fixture boundary：被 Git 忽略的 real_*.json 捕获文件永不载入服务。
# ---------------------------------------------------------------------------
def _load_json(fname):
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), fname)
    try:
        with open(p, encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        _safe_log('optional_fixture_unavailable', filename=fname, error_type=type(e).__name__)
        return None

# B02 only serves the committed, synthetic fixture.  Other capture files may
# exist beside another checkout, but they are never loaded into this server.
MUSIC_FIXTURE = _load_json('contracts/music_fixture.json') or {}


def _offline_media(value):
    """Remove embedded media URLs before returning local fixture data."""
    if isinstance(value, dict):
        return {key: _offline_media(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_offline_media(item) for item in value]
    if isinstance(value, str) and value.startswith(('http://', 'https://')):
        return ''
    return value

# ---------------------------------------------------------------------------
# 路由处理
# ---------------------------------------------------------------------------
_MEDIA_NAME = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4$")


def _media_root() -> Path | None:
    configured = _os.environ.get("OLIVIA_LOCAL_DATA_ROOT", "")
    return (Path(configured).expanduser().resolve() / "media") if configured else None


async def _media_handler(request: web.Request) -> web.StreamResponse:
    if request.method not in {"GET", "HEAD"}:
        return web.json_response({"status": "FAILED", "error_code": "METHOD_NOT_ALLOWED"}, status=405)
    name = request.path.rsplit("/", 1)[-1]
    root = _media_root()
    if root is None or not _MEDIA_NAME.fullmatch(name):
        return web.json_response({"status": "FAILED", "error_code": "MEDIA_NOT_FOUND"}, status=404)
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return web.json_response({"status": "FAILED", "error_code": "MEDIA_NOT_FOUND"}, status=404)
    if not target.is_file():
        return web.json_response({"status": "FAILED", "error_code": "MEDIA_NOT_FOUND"}, status=404)
    return web.FileResponse(target, headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"})


async def handler(request: web.Request):
    if request.path.startswith("/toy/media/"):
        return await _media_handler(request)
    path = request.path  # /toy/xxx
    method = request.method
    origin = request.headers.get('Origin', '')
    if origin and not origin_allowed(origin):
        _safe_log('cors_denied', method=method, path=path)
        return web.json_response(
            err(403, 'CORS_ORIGIN_DENIED', {'status': 'FAILED', 'error_code': 'CORS_ORIGIN_DENIED'}),
            status=403,
            headers=CORS_HEADERS(request),
        )
    if method == "OPTIONS":
        _safe_log('cors_preflight', method=method, path=path, origin_allowed=True)
        return web.Response(status=204, headers=CORS_HEADERS(request))

    body = {}
    body_error = None
    if request.can_read_body:
        try:
            raw = await request.read()
            if raw:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    body_error = err(
                        400,
                        "INVALID_BODY",
                        {"status": "FAILED", "error_code": "INVALID_BODY"},
                    )
        except (UnicodeDecodeError, json.JSONDecodeError):
            body_error = err(
                400,
                "INVALID_JSON",
                {"status": "FAILED", "error_code": "INVALID_JSON"},
            )
    query = dict(request.query)

    header_idempotency = request.headers.get("Idempotency-Key") or request.headers.get("X-Request-ID")
    if header_idempotency and isinstance(body, dict) and not any(
        body.get(name) for name in ("idempotency_key", "idempotencyKey", "request_id", "requestId")
    ):
        body["idempotency_key"] = header_idempotency

    _safe_log('request', method=method, path=path, body_present=bool(body))

    if body_error is not None:
        result = body_error
    else:
        try:
            result = await route(
                method,
                path,
                body,
                query,
                defer_reply=(method == "POST" and path.rstrip("/") == "/toy/letter/send"),
            )
        except Exception as e:
            code = _diagnostic_code('ROUTE', e)
            _safe_log('route_failure', method=method, path=path, error_code=code)
            result = err(500, 'INTERNAL_ERROR', {'status': 'FAILED', 'error_code': code})

    return web.json_response(
        result,
        status=response_http_status(result),
        headers=CORS_HEADERS(request),
    )

TRUSTED_FRONTEND_ORIGINS = frozenset({
    'https://toy-cnbeta01.olivia.miyoushe.com',
})
ALLOWED_HEADERS = ', '.join((
    'Content-Type',
    'X-Requested-With',
    'Authorization',
    'X-Bundle_Id',
    'X-Client_Type',
    'X-Device_Id',
    'X-Device_Model',
    'X-Language',
    'X-Lifecycle_Id',
    'X-Level',
    'X-Pkg_Version',
    'X-Platform',
    'X-Sys_Version',
    'X-Token',
    'X-Uid',
))


def origin_allowed(origin: str) -> bool:
    return origin in TRUSTED_FRONTEND_ORIGINS or bool(
        _re.fullmatch(r'https?://(?:localhost|127\.0\.0\.1)(?::\d+)?', origin or '')
    )


def cors_headers(origin: str) -> dict:
    headers = {
        'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
        'Access-Control-Allow-Headers': ALLOWED_HEADERS,
        'Access-Control-Max-Age': '86400',
    }
    if origin_allowed(origin):
        headers.update({
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Credentials': 'true',
            'Vary': 'Origin',
        })
    return headers


def CORS_HEADERS(request):
    return cors_headers(request.headers.get('Origin', ''))


def _asr_health() -> tuple[dict, dict]:
    """Return sanitized ASR config/status without probing the network."""

    try:
        config = AsrConfig.from_env()
        provider = create_provider(config)
        if isinstance(provider, NemotronProvider):
            native_status = provider.status()
        else:
            native_status = {
                "provider": "none",
                "status": "unavailable",
                "ready": False,
                "reason": "ASR_NOT_PROBED",
                "network_called": False,
                "verified": False,
            }
        return config.to_dict(include_paths=False), native_status
    except AsrError as exc:
        return {
            "provider": "none",
            "language": "auto",
            "error": exc.code,
        }, {
            "provider": "none",
            "status": "unavailable",
            "ready": False,
            "reason": exc.code,
            "network_called": False,
            "verified": False,
        }
    except Exception:
        return {
            "provider": "none",
            "language": "auto",
            "error": "ASR_CONFIG_INVALID",
        }, {
            "provider": "none",
            "status": "unavailable",
            "ready": False,
            "reason": "ASR_CONFIG_INVALID",
            "network_called": False,
            "verified": False,
        }


def _health_result(profile: str = contract.HEALTH_PROFILE_CORE) -> dict:
    profile_spec = contract.PROFILES.get(profile)
    if profile_spec is None:
        return err(
            400,
            "INVALID_PROFILE",
            {"status": "FAILED", "error_code": "INVALID_PROFILE"},
        )

    document = contract.contract_document()
    asr_config, native_asr_status = _asr_health()
    document["capabilities"]["text.input.fallback"].update(
        {
            "status": "available",
            "provider": "text-fallback",
            "probe": "in-process",
            "is_asr": False,
        }
    )
    document["capabilities"]["native.asr"].update(
        {
            "status": native_asr_status.get("status", "unavailable"),
            "provider": native_asr_status.get("provider", "none"),
            "reason_code": native_asr_status.get("reason", "ASR_NOT_PROBED"),
            "probe": "local-filesystem-only",
        }
    )
    llm_key_present = api_key_configured(LLM_CONFIG)
    try:
        memory_info = dict(memory_adapter.status())
    except Exception:
        memory_info = {
            "status": "unavailable",
            "enabled": False,
            "provider": "none",
            "storage": "none",
            "network_called": False,
        }
    memory_status = str(memory_info.get("status", "unavailable"))
    memory_capability_status = "available" if memory_status == "available" else "unavailable"
    for capability in ("memory.local", "memory.legacy", "memory.conversation"):
        document["capabilities"][capability].update(
            {
                "status": memory_capability_status,
                "provider": memory_info.get("provider", "none")
                if memory_status == "available"
                else "none",
                "probe": "in-process" if memory_status == "available" else "not-run",
            }
        )
    llm_ready = (
        LLM_CONFIG.feature_enabled
        and LLM_CONFIG.provider == "mock"
    ) or (
        LLM_CONFIG.feature_enabled
        and LLM_CONFIG.provider == "openai_compatible"
        and bool(LLM_CONFIG.base_url)
        and bool(LLM_CONFIG.model)
        and (not LLM_CONFIG.requires_api_key or llm_key_present)
    )
    if LLM_CONFIG.provider == "mock" and LLM_CONFIG.feature_enabled:
        llm_status = "available"
        llm_profile_status = "HEALTHY"
    elif llm_ready:
        llm_status = "degraded"
        llm_profile_status = "DEGRADED"
    else:
        llm_status = "unavailable"
        llm_profile_status = "UNAVAILABLE"
    document["capabilities"]["llm.gateway"].update(
        {
            "status": llm_status,
            "provider": LLM_CONFIG.provider if llm_ready else "none",
            "probe": "not-run",
        }
    )
    document["capabilities"]["letters.send"].update(
        {
            "status": llm_status,
            "provider": LLM_CONFIG.provider if llm_ready else "none",
            "probe": "not-run",
        }
    )
    document["capabilities"]["llm.streaming"].update(
        {
            "status": "available" if llm_ready and LLM_CONFIG.stream else "unavailable",
            "provider": LLM_CONFIG.provider if llm_ready and LLM_CONFIG.stream else "none",
            "probe": "internal-events-only",
        }
    )
    required = profile_spec["required_capabilities"]
    required_states = {
        name: document["capabilities"].get(name, {}).get("status", "unavailable")
        for name in required
    }
    healthy = all(state == "available" for state in required_states.values())
    if profile == contract.HEALTH_PROFILE_CORE:
        profile_status = "HEALTHY" if healthy else "FAILED"
    elif profile == contract.HEALTH_PROFILE_LLM:
        profile_status = llm_profile_status
    elif profile == contract.HEALTH_PROFILE_ASR:
        profile_status = "HEALTHY" if native_asr_status.get("status") == "available" else "UNAVAILABLE"
    else:
        profile_status = "HEALTHY" if memory_status == "available" else "UNAVAILABLE"
    return ok(
        {
            "schema_version": contract.SCHEMA_VERSION,
            "contract_version": contract.CONTRACT_VERSION,
            "profile": profile,
            "status": profile_status,
            "providers": {
                "local_http": {
                    "status": "available",
                    "provider": "aiohttp",
                    "probe": "in-process",
                },
                "letter_reply": {
                    "status": llm_status,
                    "provider": LLM_CONFIG.provider if llm_ready else "none",
                    "probe": "not-run",
                },
                "llm_gateway": {
                    "status": llm_status,
                    "config": LLM_CONFIG.public_dict(api_key_configured=llm_key_present),
                    "persona": letters_adapter.public_persona_status(),
                    "probe": "not-run",
                    "network_called": False,
                },
                "memory": memory_info,
                "music_catalog": {
                    "status": "available",
                    "provider": "sanitized-local-fixture",
                    "probe": "in-process",
                },
                "native_realtime": {
                    "status": "unavailable",
                    "provider": "none",
                    "probe": "not-implemented",
                },
                "asr": {
                    "status": native_asr_status.get("status", "unavailable"),
                    "provider": native_asr_status.get("provider", "none"),
                    "reason": native_asr_status.get("reason", "ASR_NOT_PROBED"),
                    "config": asr_config,
                    "probe": "local-filesystem-only",
                    "network_called": native_asr_status.get("network_called", False),
                },
                "text_input_fallback": {
                    "status": "available",
                    "provider": "text-fallback",
                    "is_asr": False,
                    "probe": "in-process",
                },
            },
            "required_checks": required_states,
            "capabilities": document["capabilities"],
            "routes": document["routes"],
            "privacy": document["privacy"],
        }
    )


def _request_value(body: dict, query: dict, *names: str):
    for name in names:
        value = query.get(name)
        if value not in (None, ""):
            return value
    for name in names:
        value = body.get(name)
        if value not in (None, ""):
            return value
    return None


def _missing_field(field: str) -> dict:
    return err(
        400,
        "MISSING_FIELD",
        {
            "status": "FAILED",
            "error_code": "MISSING_FIELD",
            "field": field,
        },
    )


def _invalid_field_type(field: str, expected: str) -> dict:
    return err(
        400,
        "INVALID_FIELD_TYPE",
        {
            "status": "FAILED",
            "error_code": "INVALID_FIELD_TYPE",
            "field": field,
            "expected": expected,
        },
    )


def _letter_collection(scope: str):
    if scope == "legacy":
        if getattr(memory_adapter, "enabled", False) and hasattr(memory_adapter, "list_legacy"):
            try:
                return list(getattr(memory_adapter, "list_legacy")())
            except Exception:
                _safe_log("memory_read_skipped", domain="legacy_letters")
        return store.legacy_letters
    return store.letters


def _bind_memory_adapter(adapter: MemoryPort) -> None:
    """Use the one local adapter for legacy retrieval without enabling chat retention."""

    global memory_adapter
    memory_adapter = adapter
    letters_adapter.memory_port = adapter
    letters_adapter.memory_prompt_builder = MemoryPromptBuilder(adapter)


def _legacy_import_adapter() -> MemoryPort:
    if getattr(memory_adapter, "enabled", False):
        return memory_adapter
    adapter = create_memory_adapter(allow_legacy_create=True)
    if getattr(adapter, "enabled", False):
        _bind_memory_adapter(adapter)
    return adapter


def _legacy_records(body: dict) -> list[LegacyLetter] | None:
    if body.get("mode") != "read_only":
        return None
    letters = body.get("letters")
    if not isinstance(letters, list):
        return None
    records: list[LegacyLetter] = []
    for item in letters:
        if not isinstance(item, dict) or "source_record_id" not in item:
            return None
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            return None
        records.append(
            LegacyLetter(
                content=item.get("content", ""),
                source_record_id=item["source_record_id"],
                source=item.get("source", "local-import"),
                occurred_at=item.get("occurred_at", item.get("created_at")),
                metadata=metadata,
            )
        )
    return records


def _letter_list_payload(scope: str) -> dict:
    letters = _letter_collection(scope)
    return {
        "list": [letter_to_out(letter) for letter in letters],
        "total": len(letters),
        "has_more": False,
        "next_cursor": 0,
        "remaining_today": 99 if scope == "current" else 0,
        "scope": scope,
        "source": "read-only-legacy" if scope == "legacy" else "local-memory",
        "read_only": scope == "legacy",
    }


def _public_llm_error(code: str | None) -> tuple[str, bool]:
    if code in {"REPLY_QUALITY_BLOCKED", "REWRITE_FAILED"}:
        return "REPLY_QUALITY_BLOCKED", False
    if code in {"LLM_TIMEOUT", "PROVIDER_TIMEOUT"}:
        return "LLM_TIMEOUT", True
    if code == "LLM_INTERRUPTED":
        return "LLM_INTERRUPTED", True
    if code in {"LLM_PROVIDER_REJECTED", "PROVIDER_REJECTED"}:
        return "LLM_PROVIDER_REJECTED", False
    if code in {"LLM_PROTOCOL_ERROR", "PROVIDER_PROTOCOL"}:
        return "LLM_PROTOCOL_ERROR", False
    return "LLM_UNAVAILABLE", True


def _schedule_text_reply_delay(letter: dict, reply_mode: str) -> None:
    """Record a publication deadline without blocking provider generation."""

    if (
        _exact_reply_mode(reply_mode) != ReplyMode.TEXT_LETTER.value
        or _os.environ.get("OLIVIA_REPLY_DELAY_ENABLED", "0").casefold()
        not in {"1", "true", "yes", "on"}
    ):
        letter["reply_delay_minutes"] = 0.0
        letter["reply_not_before"] = 0.0
        return
    try:
        minimum = float(_os.environ.get("OLIVIA_REPLY_DELAY_MINUTES_MIN", "5"))
        maximum = float(_os.environ.get("OLIVIA_REPLY_DELAY_MINUTES_MAX", "10"))
    except ValueError:
        minimum, maximum = 5.0, 10.0
    minimum, maximum = max(0.0, minimum), max(minimum, maximum)
    delay = random.uniform(minimum, maximum)
    letter["reply_delay_minutes"] = round(delay, 3)
    letter["reply_not_before"] = time.time() + delay * 60.0


def _send_result_for_letter(letter: dict) -> dict:
    if letter.get("letter_status") in {"PENDING", "PROCESSING"}:
        return ok(
            {
                "letter_id": letter["letter_id"],
                "letterId": letter["letter_id"],
                "status": "PENDING",
            }
        )
    if letter.get("reply_not_before", 0.0) > time.time():
        return ok({"letter_id": letter["letter_id"], "letterId": letter["letter_id"], "status": "PENDING", "reply_not_before": letter["reply_not_before"]})
    if letter.get("letter_status") == "COMPLETED" and letter.get("reply_text"):
        return ok(
            {
                "letter_id": letter["letter_id"],
                "letterId": letter["letter_id"],
                "status": "COMPLETED",
            }
        )
    error_code, retryable = _public_llm_error(letter.get("error_code"))
    return err(
        503,
        error_code,
        {
            "letter_id": letter["letter_id"],
            "status": "FAILED",
            "error_code": error_code,
            "retryable": retryable,
        },
    )


def _recent_active_duplicate(
    content: str,
    material: dict,
    *,
    now: float | None = None,
) -> dict | None:
    current_time = time.time() if now is None else now
    for letter in store.letters:
        created_at = letter.get("created_at")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            continue
        age = current_time - float(created_at)
        if age < 0 or age > LETTER_RETRY_DEDUP_SECONDS:
            continue
        if letter.get("letter_status") not in {"PENDING", "PROCESSING", "COMPLETED"}:
            continue
        if letter.get("content") == content and letter.get("material", {}) == material:
            return letter
    return None


async def route(method, path, body, query, *, defer_reply: bool = False):
    p = path.rstrip("/")

    spec = contract.route_spec(p)
    if spec is None:
        _safe_log('unimplemented_route', method=method, path=p)
        return not_implemented()
    if method not in spec["methods"]:
        return err(
            405,
            "METHOD_NOT_ALLOWED",
            {
                "status": "FAILED",
                "error_code": "METHOD_NOT_ALLOWED",
                "allowed_methods": spec["methods"],
            },
        )
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return err(400, "INVALID_BODY", {
            "status": "FAILED",
            "error_code": "INVALID_BODY",
        })
    if query is None:
        query = {}
    if p == "/health":
        return _health_result(query.get("profile", contract.HEALTH_PROFILE_CORE))
    if spec["state"] == "not_implemented" and p != "/toy/midi/generate":
        return not_implemented(spec["error_code"] or "ROUTE_NOT_IMPLEMENTED")

    # ---- 认证 ----
    if p == "/toy/signIn" or p == "/toy/getUserInfo":
        return ok({
            "uid": store.uid,
            "status": 0,
            "model_gateway_token": fake_jwt(),
            "userInfo": {
                "uid": store.uid,
                "nickname": "玩家",
                "isNewUser": False,
                "isNewDevice": False,
                "performanceModes": [{"type": "Solo", "displayName": "独奏"}],
                "musicMenus": [],
            },
        })

    # ---- 偏好调查（跳过）----
    if p == "/toy/getPreferenceSurvey":
        return ok({"questions": []})
    if p == "/toy/submitPreferenceSurvey":
        return not_implemented("PREFERENCE_SURVEY_NOT_IMPLEMENTED")

    # ---- 信件 ----
    if p == "/toy/letter/list":
        scope = query.get("scope", "current")
        if scope not in {"current", "legacy"}:
            return err(400, "INVALID_SCOPE", {
                "status": "FAILED",
                "error_code": "INVALID_SCOPE",
                "allowed_scopes": ["current", "legacy"],
            })
        return ok(_letter_list_payload(scope))
    if p == "/toy/letter/unread_count":
        scope = query.get("scope", "current")
        if scope not in {"current", "legacy"}:
            return err(400, "INVALID_SCOPE", {
                "status": "FAILED",
                "error_code": "INVALID_SCOPE",
                "allowed_scopes": ["current", "legacy"],
            })
        letters = _letter_collection(scope)
        unread = sum(1 for letter in letters if not letter.get("is_read"))
        return ok({
            "unread_count": unread,
            "scope": scope,
            "read_only": scope == "legacy",
        })
    if p == "/toy/letter/detail":
        lid = _request_value(body, query, "letter_id", "letterId")
        if lid is None:
            return _missing_field("letter_id")
        scope = query.get("scope", "current")
        if scope not in {"current", "legacy"}:
            return err(400, "INVALID_SCOPE", {
                "status": "FAILED",
                "error_code": "INVALID_SCOPE",
                "allowed_scopes": ["current", "legacy"],
            })
        letters = _letter_collection(scope)
        l = next((x for x in letters if x["letter_id"] == lid), None)
        if not l:
            return err(404, "LETTER_NOT_FOUND", {
                "status": "FAILED",
                "error_code": "LETTER_NOT_FOUND",
            })
        if scope == "current":
            l["is_read"] = 1
        reply_published = l.get("reply_not_before", 0.0) <= time.time()
        reply_text = l.get("reply_text", "") if reply_published else ""
        error_code, retryable = _public_llm_error(l.get("error_code"))
        return ok({
            "letter_id": l["letter_id"],
            "letter_status": l.get("letter_status", 4),
            "error_code": error_code if l.get("letter_status") == "FAILED" else None,
            "retryable": retryable if l.get("letter_status") == "FAILED" else False,
            "audit_status": l.get("audit_status", 2),
            "content": l.get("content", ""),
            "material": l.get("material", {}),
            "reply_type": 1 if reply_text else 0,
            "reply_text": reply_text,
            "reply_content": reply_text,
            "reply_video_url": l.get("reply_video_url", ""),
            "reply_mode": (
                _wire_reply_mode(l.get("reply_mode"))
                if reply_published
                else "text"
            ),
            "reply_mode_exact": (
                _exact_reply_mode(l.get("reply_mode"))
                if reply_published
                else ReplyMode.TEXT_LETTER.value
            ),
            "triage": l.get("triage", {"status": "unavailable"}),
            "media_status": l.get("media_status", "NOT_REQUESTED"),
            "media_error_code": l.get("media_error_code"),
            "is_read": 1 if l.get("is_read") else 0,
            "replied_at": l.get("replied_at"),
            "created_at": l.get("created_at", int(time.time())),
            "scope": scope,
            "read_only": scope == "legacy",
        })
    if p == "/toy/letter/legacy/import":
        records = _legacy_records(body)
        if records is None:
            return err(400, "INVALID_BODY", {
                "status": "FAILED",
                "error_code": "INVALID_BODY",
            })
        adapter = _legacy_import_adapter()
        if not getattr(adapter, "enabled", False):
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
            })
        try:
            result = adapter.import_legacy_records(records, atomic=True)
        except Exception:
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
            })
        payload = result.to_dict()
        payload.update({"read_only": True, "scope": "legacy"})
        if result.rolled_back:
            return err(400, "INVALID_CONTENT", {
                "status": "FAILED",
                "error_code": "INVALID_CONTENT",
                **payload,
            })
        return ok(payload)
    if p == "/toy/letter/send":
        if query.get("scope", "current") == "legacy":
            return err(403, "READ_ONLY_SCOPE", {
                "status": "FAILED",
                "error_code": "READ_ONLY_SCOPE",
                "scope": "legacy",
            })
        if "content" not in body:
            return _missing_field("content")
        content = body.get("content")
        material = body.get("material", {})
        if "material" in body and not isinstance(material, dict):
            return _invalid_field_type("material", "object")
        if not isinstance(content, str) or not content.strip():
            return err(400, 'INVALID_CONTENT', {'status': 'FAILED', 'error_code': 'INVALID_CONTENT'})
        duration = material.get("music_duration_seconds", 118)
        if isinstance(duration, bool) or duration not in {90, 118}:
            return err(400, "MUSIC_DURATION_INVALID", {"status": "FAILED", "error_code": "MUSIC_DURATION_INVALID", "allowed": [90, 118]})
        if len(content) > 10000:
            return err(400, 'CONTENT_TOO_LONG', {
                'status': 'FAILED',
                'error_code': 'CONTENT_TOO_LONG',
                'max_length': 10000,
            })
        idempotency_key = _request_value(
            body,
            query,
            "idempotency_key",
            "idempotencyKey",
            "request_id",
            "requestId",
        )
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
                return err(400, "INVALID_IDEMPOTENCY_KEY", {
                    "status": "FAILED",
                    "error_code": "INVALID_IDEMPOTENCY_KEY",
                })
            previous_id = store.request_keys.get(idempotency_key)
            previous = next(
                (item for item in store.letters if item["letter_id"] == previous_id),
                None,
            )
            if previous is not None:
                if (
                    previous.get("content") != content
                    or previous.get("material", {}) != material
                ):
                    return err(409, "IDEMPOTENCY_CONFLICT", {
                        "status": "FAILED",
                        "error_code": "IDEMPOTENCY_CONFLICT",
                    })
                if previous.get("letter_status") not in {"FAILED", "CANCELED"}:
                    return _send_result_for_letter(previous)
        else:
            previous = _recent_active_duplicate(content, material)
            if previous is not None:
                return _send_result_for_letter(previous)
        lid = str(uuid.uuid4())
        letter = {
            "letter_id": lid,
            "content": content,
            "material": material,
            "letter_status": "PENDING",
            "audit_status": 2,
            "is_read": 1,
            "created_at": int(time.time()),
            "reply_text": "",
            "reply_mode": ReplyMode.TEXT_LETTER.value,
            "triage": {"status": "pending"},
            "music_duration_seconds": duration,
        }
        store.letters.insert(0, letter)
        if idempotency_key is not None:
            store.request_keys[idempotency_key] = lid
        _persist_store_state()
        if defer_reply:
            _schedule_reply_job(lid, content, idempotency_key=idempotency_key)
            return _send_result_for_letter(letter)
        completed = await generate_reply(lid, content, idempotency_key=idempotency_key)
        if not completed:
            return _send_result_for_letter(letter)
        return _send_result_for_letter(letter)
    if p == "/toy/letter/resend":
        return not_implemented("LETTER_RESEND_NOT_IMPLEMENTED")
    if p == "/toy/letter/share":
        return not_implemented("LETTER_SHARE_NOT_IMPLEMENTED")

    # ---- 音乐库 ----
    if p == "/toy/getMusicTypeInfo":
        music_type = MUSIC_FIXTURE.get("music_type")
        if isinstance(music_type, dict):
            return ok(_offline_media(music_type))
        return ok({
            "performance_modes": [],
            "music_styles": [],
            "source": "empty",
        })
    if p == "/toy/searchSongs":
        style = query.get("style_type", "Classical")
        songs = MUSIC_FIXTURE.get("songs", [])
        if not isinstance(songs, list):
            songs = []
        filtered = [song for song in songs if song.get("style_type") == style]
        return ok({
            "next_cursor": 0,
            "has_more": False,
            "total": len(filtered),
            "list": _offline_media(filtered),
            "source": "fixture" if filtered else "empty",
        })
    if p == "/toy/searchPlaylist":
        return ok({"total": 0, "next_cursor": "0", "has_more": False, "list": [], "source": "empty"})
    if p == "/toy/searchUserSongs":
        return ok({"next_cursor": 0, "has_more": False, "list": [], "source": "empty"})
    if p == "/toy/searchPerformances":
        return ok({"next_cursor": 0, "has_more": False, "list": [], "source": "empty"})
    if p == "/toy/getSongStats":
        return ok({"source": "empty", "stats": {}})
    if p == "/toy/addPerformance" or p == "/toy/editPerformance" or p == "/toy/delPerformance" \
       or p == "/toy/addToPlaylist" or p == "/toy/delFromPlaylist" or p == "/toy/deleteUserSong":
        return not_implemented("MUSIC_WRITE_NOT_IMPLEMENTED")

    # ---- MIDI 生成 ----
    if p == "/toy/genObjectUploadUrl":
        return not_implemented("MIDI_UPLOAD_NOT_IMPLEMENTED")
    if p == "/toy/midi/generate":
        midi_url = body.get("midiUrl", "")
        filename = body.get("filename", "")
        job = music_adapter.submit(midi_url, filename)
        job["created_at"] = int(time.time())
        store.midi_jobs.append(job)
        return err(501, 'MIDI_NOT_IMPLEMENTED', {
            'job_id': job['job_id'],
            'status': job['status'],
            'error_code': job['error_code'],
        })
    if p == "/toy/midi/listJobs":
        return ok({
            "total": len(store.midi_jobs),
            "next_cursor": 0, "has_more": False,
            "list": [{"job_id": j["job_id"], "state": j["state"],
                      "status": j.get("status", "FAILED"),
                      "filename": j.get("filename", ""), "created_at": j.get("created_at")}
                     for j in store.midi_jobs],
        })
    if p == "/toy/midi/batchGetResult":
        ids = query.getall("job_ids", []) if hasattr(query, 'getall') else query.get("job_ids", [])
        if isinstance(ids, str):
            ids = [ids]
        results = []
        for j in store.midi_jobs:
            if not ids or j["job_id"] in ids:
                results.append({"job_id": j["job_id"], "state": j["state"], "status": j.get("status", "FAILED")})
        return ok({"results": results, "generated_today": len(store.midi_jobs), "daily_limit": 0})
    if p == "/toy/midi/getGenerateResult":
        job_id = query.get("jobId") or query.get("job_id", "")
        j = next((x for x in store.midi_jobs if x["job_id"] == job_id), None)
        if not j:
            return err(404, 'MIDI_JOB_NOT_FOUND', {'jobId': job_id, 'status': 'FAILED'})
        return ok({"jobId": job_id, "state": j["state"], "status": j.get("status", "FAILED"), "info": {"videoUrls": []}})
    if p == "/toy/midi/cancelGenerate":
        job_id = body.get('jobId') or query.get('jobId') or query.get('job_id', '')
        j = next((x for x in store.midi_jobs if x["job_id"] == job_id), None)
        if not j:
            return err(404, 'MIDI_JOB_NOT_FOUND', {
                'job_id': job_id,
                'status': 'FAILED',
                'error_code': 'MIDI_JOB_NOT_FOUND',
            })
        if j and j.get('status') not in {
            'COMPLETED', 'FAILED', 'CANCELED', 'NOT_IMPLEMENTED'
        }:
            j.update({'state': 4, 'status': 'CANCELED', 'error_code': None})
        return ok({'job_id': job_id, 'status': j.get('status', 'CANCELED')})
    if p == "/toy/midi/deleteJob":
        job_id = body.get('jobId') or query.get('jobId') or query.get('job_id', '')
        if not any(x.get('job_id') == job_id for x in store.midi_jobs):
            return err(404, 'MIDI_JOB_NOT_FOUND', {
                'job_id': job_id,
                'status': 'FAILED',
                'error_code': 'MIDI_JOB_NOT_FOUND',
            })
        store.midi_jobs[:] = [j for j in store.midi_jobs if j.get('job_id') != job_id]
        return ok({'job_id': job_id, 'status': 'DELETED'})
    if p == "/toy/midi/importShareCode":
        return not_implemented("MIDI_IMPORT_NOT_IMPLEMENTED")

    # ---- 其他 ----
    if p == "/toy/editProfile":
        return not_implemented("PROFILE_EDIT_NOT_IMPLEMENTED")
    if p == "/toy/createFeedback":
        return not_implemented("FEEDBACK_NOT_IMPLEMENTED")
    if p == "/toy/generateShareToken":
        return not_implemented("SHARE_TOKEN_NOT_IMPLEMENTED")

    _safe_log('unimplemented_route', method=method, path=p)
    return not_implemented()

async def _render_media_job(letter_id: str, content: str, reply_text: str, reply_mode: str) -> None:
    """Render one media reply at a time and persist a relative artifact path."""

    letter = next((item for item in store.letters if item["letter_id"] == letter_id), None)
    if letter is None:
        return
    async with media_semaphore:
        letter["media_status"] = "PROCESSING"
        _persist_media_state()
        data_root = Path(_os.environ.get("OLIVIA_LOCAL_DATA_ROOT", ""))
        output_dir = data_root / "media" if data_root.is_absolute() else None
        if output_dir is None:
            letter["media_status"] = "UNAVAILABLE_DATA_ROOT_NOT_CONFIGURED"
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{letter_id}.mp4"
        try:
            from datetime import datetime

            hour = datetime.now().hour
            scene_key = "morning" if 5 <= hour < 10 else "day" if 10 <= hour < 17 else "dusk" if 17 <= hour < 20 else "night"
            normal_scene_value = _os.environ.get(f"OLIVIA_SCENE_{scene_key.upper()}", "")
            normal_scene = Path(normal_scene_value).expanduser() if normal_scene_value else None
            if normal_scene is None or not normal_scene.is_file():
                raise ReplyMediaError("ORDINARY_SCENE_NOT_CONFIGURED")
            tts_config = Path(_os.environ.get("OLIVIA_TTS_CONFIG", ""))
            visual_config = Path(_os.environ.get("OLIVIA_VISUAL_CONFIG", ""))
            worker = Path(_os.environ.get("OLIVIA_LIVETALKING_WORKER", ""))
            if reply_mode == "musical_video":
                await asyncio.to_thread(render_musical_reply,
                    content,
                    reply_text,
                    output_path,
                    normal_video_path=output_dir / f"{letter_id}-spoken.mp4",
                    song_video_path=output_dir / f"{letter_id}-song.mp4",
                    normal_scene_path=normal_scene,
                    tts_config_path=tts_config,
                    visual_config_path=visual_config,
                    worker_path=worker,
                    performance_video_path=Path(_os.environ.get("OLIVIA_MUSIC_PERFORMANCE_BASE", "")),
                    duration_seconds=int(letter.get("music_duration_seconds", 118)),
                )
            else:
                await asyncio.to_thread(render_reply_video,
                    reply_text,
                    output_path,
                    tts_config_path=tts_config,
                    visual_config_path=visual_config,
                    worker_path=worker,
                    scene_path=normal_scene,
                    latentsync_python_path=Path(_os.environ.get("OLIVIA_LATENTSYNC_PYTHON", "")),
                    latentsync_root=Path(_os.environ.get("OLIVIA_LATENTSYNC_ROOT", "")),
                    adaptive_delivery=True,
                )
            letter["reply_video_url"] = f"http://127.0.0.1:{PORT}/toy/media/{output_path.name}"
            letter["media_status"] = "COMPLETED"
            _persist_media_state()
        except (ReplyMediaError, MusicReplyError, ValueError, OSError) as exc:
            letter["media_status"] = "UNAVAILABLE"
            letter["media_error_code"] = str(exc)[:80] or "MEDIA_PROVIDER_UNAVAILABLE"
            _persist_media_state()


def _schedule_media_job(letter_id: str, content: str, reply_text: str, reply_mode: str) -> None:
    task = asyncio.create_task(_render_media_job(letter_id, content, reply_text, reply_mode))
    media_tasks.add(task)
    task.add_done_callback(media_tasks.discard)


async def _run_reply_job(
    letter_id: str,
    content: str,
    *,
    idempotency_key: str | None,
) -> bool:
    try:
        return await generate_reply(
            letter_id,
            content,
            idempotency_key=idempotency_key,
        )
    except Exception:
        letter = next(
            (item for item in store.letters if item["letter_id"] == letter_id),
            None,
        )
        if letter is not None and letter.get("letter_status") in {
            "PENDING",
            "PROCESSING",
        }:
            letter["letter_status"] = "FAILED"
            letter["error_code"] = "LLM_UNAVAILABLE"
            _persist_store_state()
        _safe_log("letter_failed", error_code="LLM_UNAVAILABLE")
        return False


def _schedule_reply_job(
    letter_id: str,
    content: str,
    *,
    idempotency_key: str | None,
) -> None:
    active = reply_jobs.get(letter_id)
    if active is not None and not active.done():
        return
    task = asyncio.create_task(
        _run_reply_job(
            letter_id,
            content,
            idempotency_key=idempotency_key,
        )
    )
    reply_tasks.add(task)
    reply_jobs[letter_id] = task

    def discard(completed: asyncio.Task) -> None:
        reply_tasks.discard(completed)
        if reply_jobs.get(letter_id) is completed:
            reply_jobs.pop(letter_id, None)

    task.add_done_callback(discard)


def _idempotency_key_for_letter(letter_id: str) -> str | None:
    return next(
        (
            key
            for key, mapped_letter_id in store.request_keys.items()
            if mapped_letter_id == letter_id
        ),
        None,
    )


def _schedule_pending_reply_jobs() -> int:
    scheduled = 0
    for letter in tuple(store.letters):
        if letter.get("letter_status") != "PENDING":
            continue
        letter_id = str(letter.get("letter_id", ""))
        content = letter.get("content")
        if not letter_id or not isinstance(content, str) or not content.strip():
            continue
        if letter_id in reply_jobs and not reply_jobs[letter_id].done():
            continue
        _schedule_reply_job(
            letter_id,
            content,
            idempotency_key=_idempotency_key_for_letter(letter_id),
        )
        scheduled += 1
    return scheduled


async def _start_reply_tasks(_app: web.Application) -> None:
    _schedule_pending_reply_jobs()


async def _stop_reply_tasks(_app: web.Application) -> None:
    tasks = tuple(reply_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def install_reply_task_lifecycle(app: web.Application) -> None:
    """Resume durable pending replies and stop owned tasks with the HTTP app."""

    app.on_startup.append(_start_reply_tasks)
    app.on_cleanup.append(_stop_reply_tasks)


def _prepare_private_world_delivery(letter: dict, canonical_text: str) -> None:
    if letter.get("reply_text") == canonical_text and letter.get(
        "private_world_delivery_id"
    ):
        return
    revision = max(0, int(letter.get("reply_revision", 0))) + 1
    letter_id = str(letter["letter_id"])
    delivery_id = f"{letter_id}:{revision}"
    semantic_digest = hashlib.sha256(letter_id.encode("utf-8")).hexdigest()
    letter["reply_revision"] = revision
    letter["private_world_delivery_id"] = delivery_id
    letter["private_world_status"] = "PENDING"
    letter["private_world_occurred_at"] = datetime.now(timezone.utc).isoformat()
    letter["private_world_event_kind"] = ReducerEventKind.CANONICAL_REPLY_DELIVERED.value
    letter["private_world_semantic_key"] = f"canonical.{semantic_digest}"


def _commit_private_world_letter(letter: dict) -> bool:
    if letter.get("private_world_status") != "PENDING":
        return False
    if private_world_committer is None:
        letter["private_world_error_code"] = "PRIVATE_WORLD_UNAVAILABLE"
        return False
    try:
        delivery = DeliveryEvent(
            delivery_id=str(letter["private_world_delivery_id"]),
            kind=ReducerEventKind(str(letter["private_world_event_kind"])),
            occurred_at=datetime.fromisoformat(str(letter["private_world_occurred_at"])),
            semantic_key=str(letter["private_world_semantic_key"]),
        )
        status = private_world_committer.commit(delivery)
    except (KeyError, TypeError, ValueError):
        letter["private_world_error_code"] = "PRIVATE_WORLD_EVENT_INVALID"
        return False
    if status in {DeliveryStatus.COMMITTED, DeliveryStatus.DUPLICATE}:
        letter["private_world_status"] = "COMMITTED"
        letter.pop("private_world_error_code", None)
        return True
    letter["private_world_error_code"] = "PRIVATE_WORLD_UNAVAILABLE"
    return False


def recover_pending_private_world() -> int:
    recovered = 0
    for letter in store.letters:
        if (
            letter.get("letter_status") == "COMPLETED"
            and letter.get("reply_text")
            and _commit_private_world_letter(letter)
        ):
            recovered += 1
    if recovered:
        _persist_store_state()
    return recovered


async def generate_reply(letter_id, content, *, idempotency_key=None):
    """Run one routed current-letter reply to its canonical terminal state."""

    letter = next(
        (item for item in store.letters if item["letter_id"] == letter_id),
        None,
    )
    if letter is None:
        return False

    letter["letter_status"] = "PROCESSING"
    _persist_store_state()
    decision = await emotion_triage.classify(content)
    exact_mode = _exact_reply_mode(decision.reply_mode)
    letter["triage"] = decision.to_dict()
    letter["reply_mode"] = exact_mode
    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
    }:
        letter["media_status"] = "UNAVAILABLE_THIRD_PARTY_NOT_INSTALLED"
    _schedule_text_reply_delay(letter, exact_mode)
    _persist_store_state()

    try:
        request = ReplyRequest(
            content=content,
            request_id=f"letter-reply:{letter_id}",
            idempotency_key=(
                f"{idempotency_key}:{letter_id}" if idempotency_key else None
            ),
            max_input_chars=LLM_CONFIG.max_input_chars,
        )
        result = await asyncio.wait_for(
            reply_pipeline.run(
                request,
                letters_adapter.build_reply_context(ReplyMode(exact_mode)),
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_INTERRUPTED"
        _persist_store_state()
        _safe_log("letter_cancelled")
        raise
    except asyncio.TimeoutError:
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_TIMEOUT"
        _persist_store_state()
        _safe_log("letter_failed", error_code="LLM_TIMEOUT")
        return False
    except (ValueError, RuntimeError):
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_UNAVAILABLE"
        _persist_store_state()
        _safe_log("letter_failed", error_code="LLM_UNAVAILABLE")
        return False

    if result.quality_status is not None:
        letter["quality_status"] = result.quality_status
        letter["quality_violation_codes"] = list(result.violation_codes)
    if result.state is not ReplyState.COMPLETED:
        public_code, _retryable = _public_llm_error(result.error_code)
        letter["letter_status"] = "FAILED"
        letter["error_code"] = public_code
        _persist_store_state()
        _safe_log("letter_failed", error_code=public_code)
        return False

    _prepare_private_world_delivery(letter, result.text)
    letter["reply_text"] = result.text
    letter["letter_status"] = "COMPLETED"
    _persist_store_state()
    _commit_private_world_letter(letter)
    _persist_store_state()

    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
    }:
        letter["media_status"] = "PENDING"
        _schedule_media_job(letter_id, content, result.text, exact_mode)

    letters_adapter.remember_conversation(content, result.text)
    _safe_log("letter_completed", reply_mode=exact_mode)
    return True

if __name__ == "__main__":
    recover_pending_private_world()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    install_reply_task_lifecycle(app)
    _safe_log('server_start', host='127.0.0.1', port=PORT)
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None)
