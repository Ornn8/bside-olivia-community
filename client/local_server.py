# Olivia 本地 toy API 兼容层（仅本地运行）
# 运行: python local_server.py   (监听 127.0.0.1:8899)
# 前端 patch: getClientConfig 的 toyApiUrl -> http://127.0.0.1:8899
#
# 本地适配点（按配置接入模型）:
#   letter_adapter.reply(content)  -> 本地 LLM 生成回信
#   music_adapter.generate(midi)   -> 本地音乐模型生成演奏
import asyncio
from collections import deque
from contextvars import ContextVar
import json
import sys
import os as _os
import re as _re
import random
import itertools
import sqlite3
import time
import uuid
import hashlib
import inspect
import threading
import copy
from concurrent.futures import ThreadPoolExecutor
import tempfile as _tempfile
import webbrowser as _webbrowser
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Callable, Mapping

from aiohttp import web

import http_contract as contract
from asr.config import AsrConfig
from asr.errors import AsrError
from asr.provider import NemotronProvider, create_provider
from llm_gateway import (
    Gateway,
    GatewayConfig,
    ManagedLLMConfig,
    GatewayDelta,
    GatewayError,
    GatewayRequestScope,
    GatewayResponse,
    ProviderTimeout,
    ProviderUnavailable,
    UnconfiguredAdapter,
    api_key_configured,
    create_gateway,
    load_gateway_config,
    supports_scoped_reasoning,
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
from letter_triage import (
    LetterEmotionTriage,
    TriageResult,
    _current_music_performance,
    _voice_reply_configured,
    _singing_video_configured,
)
from runtime.media.media_paths import configured_media_path
from runtime.media.local_song_library import LocalSongLibrary, LocalSongError
from music_reply import (
    _persist_provider_failure,
    MusicReplyError,
    require_breeze_hardware,
    render_musical_reply,
    video_reply_dependency_status,
    video_reply_source_url,
)
from runtime.reply.reply_media import ReplyMediaError, render_reply_video, render_reply_audio
from runtime.reply.reply_delivery import (
    build_ordinary_video_llm_content,
    ordinary_video_reply_length_ok,
)
from voice_direction import (
    VoiceDirectionError,
    TextOnlyVoicePlan,
)
from runtime.video_reply_settings import (
    VideoReplySettingsError,
    VideoReplySettingsStore,
    receive_eligibility_from_letter,
)
from conversation_memory_port import ConversationMemoryPort
from conversation_memory_runtime import (
    retry_exhausted_conversation_memory,
    conversation_memory_reply_readiness_status,
    conversation_memory_runtime_status,
    ensure_conversation_memory_runtime,
    stop_conversation_memory_runtime,
)
from local_memory import (
    create_conversation_memory_adapter,
    create_memory_adapter,
    load_memory_config,
)
from memory_port import LegacyLetter, MemoryPort, NullMemoryPort
from memory_prompt import MemoryPromptBuilder
from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from private_world_port import NullPrivateWorldPort, PrivateWorldPort, PrivateWorldSnapshot
from runtime.memory.private_world_delivery import (
    DeliveryEvent,
    DeliveryStatus,
    PrivateWorldDeliveryCommitter,
)
from runtime.memory.private_world_relationship import (
    PrivateWorldRelationshipCommitter,
    RelationshipFactCommand,
    RelationshipFactStatus,
)
from private_world_candidate import (
    CandidateDeliveryStatus,
    GatewayPrivateWorldCandidateAnalyzer,
    PrivateWorldCandidateAnalyzer,
    PrivateWorldCandidateRequest,
    PrivateWorldCandidateRuntime,
    create_private_world_candidate_runtime,
    deliver_private_world_candidate,
)
from private_world_candidates import SQLitePrivateWorldCandidateStore
from private_world_service import PrivateWorldCommandService
from runtime.memory.private_world_projection import project_private_world
from runtime.private_world.daily_life import DailyLifeStore
from runtime.private_world.daily_life_runtime import DailyLifeRuntime, life_persona
from runtime.memory.private_world_runtime import (
    PrivateWorldRuntime,
    create_private_world_runtime,
    resolve_private_world_database,
)

from runtime.imports.official_letters import collect_default_official_text_replies
from runtime.imports.letter_backup import export_letters as export_letter_backup, import_letters as import_letter_backup, is_backup as is_letter_backup, personal_chat_letters
from runtime.imports.offline_letter_pairs import (
    OFFLINE_LETTER_PAIR_PROVENANCE_KEY,
    OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY,
    apply_offline_letter_pair_recovery_to_adapter,
    is_published_offline_letter_pair,
    offline_letter_pair_exchanges,
    plan_offline_letter_pair_recovery_with_adapter,
)
from runtime.imports.historical_memory import (
    HistoricalExchange,
    HistoricalMigrationResult,
    HistoricalRelationshipError,
    apply_historical_private_world,
    assess_historical_relationship,
    exchanges_from_legacy_payload,
    historical_relationship_command_id,
    migrate_historical_exchanges,
)
from runtime.reply.reply_context import (
    ReplyContext,
    ReplyMode,
    TrustedTime,
    TrustedWorldFact,
    WorldFactKind,
)
from runtime.reply.reply_pipeline import (
    ReplyPipeline, UnavailableRewriter, current_turn_interpretation_enabled,
    _runtime_current_turn_interpreter,
)
from runtime.reply.reply_model_quality import (
    create_model_quality_ports,
    resolve_model_quality_config,
)
from runtime.reply.reply_reviewer import NullReviewer


_PUBLIC_MUSIC_REPLY_STRUCTURE = (
    "normal_video_then_official_transition_then_song_video"
)
_PUBLIC_SONG_EMOTIONS = frozenset(
    {
        "quiet_longing",
        "gentle_reassurance",
        "restrained_sadness",
        "warm_gratitude",
        "soft_reconciliation",
        "calm_affection",
    }
)


def _sanitized_music_render_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    if value.get("audio_provider") == "breeze_tts2":
        result["audio_provider"] = "breeze_tts2"
    structure = value.get("reply_structure")
    if isinstance(structure, str) and structure in {_PUBLIC_MUSIC_REPLY_STRUCTURE, "singing_only", "voice_then_singing"}:
        result["reply_structure"] = structure
    emotion = value.get("song_emotion")
    if isinstance(emotion, str) and emotion in _PUBLIC_SONG_EMOTIONS:
        result["song_emotion"] = emotion
    transition = value.get("transition_duration_seconds")
    if type(transition) in {int, float} and float(transition) == 8.0:
        result["transition_seconds"] = 8.0
    return result


PORT = int(_os.environ.get("OLIVIA_PORT", "8899"))
VIDEO_REPLY_MUSIC_DURATION_SECONDS = 110
LLM_TIMEOUT_SECONDS = 30
LETTER_RETRY_DEDUP_SECONDS = 60
MEMORY_READY_REPLY_TIMEOUT_SECONDS = 120.0

_official_import_progress_lock = threading.Lock()
_local_import_task: asyncio.Task | None = None
_local_import_result: dict | None = None
_history_relationship_queue = None
_history_relationship_task = None


def _start_history_relationships(*, retry=False):
    global _history_relationship_task
    queue = _history_relationship_queue
    if queue is None:
        return
    if retry:
        queue.retry()
    if _history_relationship_task is not None and not _history_relationship_task.done():
        return
    async def work():
        try:
            from runtime.imports.relationship_batches import archive_exchanges
            rows = await asyncio.to_thread(_legacy_import_adapter().list_legacy)
            queue.enqueue(archive_exchanges(rows))
            if private_world_command_service is not None and _llm_runtime_ready():
                await queue.run(gateway=letters_adapter.gateway,
                    persona_policy=letters_adapter.get_persona_policy(),
                    command_service=private_world_command_service,
                    snapshot=private_world_port.snapshot)
                state = queue.status()
                if state.get('status') == 'FAILED':
                    from runtime.diagnostics.support_bundle import project_history_relationship_failure
                    event = project_history_relationship_failure(state)
                    _safe_log(event.pop('event'), **event)
        except asyncio.CancelledError:
            raise
        except Exception:
            _safe_log('history_relationship_failed', error_code='HISTORY_RELATIONSHIP_FAILED')
    _history_relationship_task = asyncio.create_task(work())


def _history_relationship_status():
    if _history_relationship_queue is None:
        return {'status':'UNAVAILABLE','processed':0,'total':0,'batch_size':5}
    result = _history_relationship_queue.status()
    if _history_relationship_task is not None and not _history_relationship_task.done():
        result['status'] = 'RUNNING'
    return result


def _history_relationship_diagnostic_snapshot():
    """Read failure metadata from the durable queue even after a restart."""
    if _history_relationship_queue is None:
        return ()
    try:
        from runtime.diagnostics.support_bundle import project_history_relationship_failure
        event = project_history_relationship_failure(_history_relationship_queue.status())
        return (event,) if event else ()
    except Exception:
        return ()  # A broken queue must not prevent export of other diagnostics.


_history_memory_admin_gate = threading.Lock()
_history_import_operations: set[asyncio.Task] = set()


def _local_import_snapshot():
    if _local_import_task is not None and not _local_import_task.done():
        return ok({**_official_import_progress_snapshot(), "status": "RUNNING"})
    return _local_import_result or ok({"status": "IDLE"})


async def _run_local_import(*, originals_only=False):
    global _local_import_result
    try:
        deadline = asyncio.get_running_loop().time() + MEMORY_READY_REPLY_TIMEOUT_SECONDS
        while not originals_only and getattr(conversation_memory_adapter.status(), "reason_code", None) == "MEM0_INITIALIZING":
            _update_official_import_progress(
                status="RUNNING", stage="memory_wait", total=0, processed=0,
            )
            if asyncio.get_running_loop().time() >= deadline:
                _local_import_result = err(503, "OFFICIAL_HISTORY_MEMORY_WAIT_TIMEOUT", {
                    "status": "UNAVAILABLE",
                    "error_code": "OFFICIAL_HISTORY_MEMORY_WAIT_TIMEOUT",
                    "retryable": True,
                })
                return
            await asyncio.sleep(0.5)
        _update_official_import_progress(status="RUNNING", stage="preflight", total=0, processed=0)
        _local_import_result = await route(
            "POST", "/toy/letter/legacy/local-import", {"originals_only": originals_only}, {},
            companion_confirmed=True, _local_import_worker=True,
        )
    except asyncio.CancelledError:
        _local_import_result = err(503, "OFFLINE_HISTORY_INTERRUPTED")
        raise
    except Exception:
        _local_import_result = err(503, "OFFLINE_HISTORY_IMPORT_FAILED")
    finally:
        result = (_local_import_result or {}).get("data", {})
        fields = {"status": result.get("status", "FAILED")}
        for value in (result.get("error_code"), (result.get("memory_migration") or {}).get("error_code")):
            if isinstance(value, str) and _RUNTIME_DIAGNOSTIC_CODE_RE.fullmatch(value):
                fields["error_code"] = value
        _safe_log("local_history_import_result", **fields)

_official_import_progress: dict[str, object] = {
    "status": "IDLE",
    "stage": "idle",
    "total": 0,
    "processed": 0,
    "imported": 0,
    "skipped": 0,
    "last_updated_at": datetime.now(timezone.utc).isoformat(),
    "retryable": False,
}


def _official_import_progress_snapshot() -> dict[str, object]:
    with _official_import_progress_lock:
        return dict(_official_import_progress)


def _update_official_import_progress(**changes: object) -> None:
    with _official_import_progress_lock:
        _official_import_progress.update(changes)
        _official_import_progress["last_updated_at"] = datetime.now(
            timezone.utc
        ).isoformat()


def _collect_official_import_with_progress() -> dict[str, object]:
    collector = collect_default_official_text_replies

    def on_progress(value: Mapping[str, object]) -> None:
        _update_official_import_progress(
            status="RUNNING",
            stage=str(value.get("stage") or "listing"),
            total=int(value.get("total") or 0),
            processed=int(value.get("processed") or 0),
            retryable=False,
        )

    if "on_progress" in inspect.signature(collector).parameters:
        return collector(on_progress=on_progress)
    return collector()


def _exact_reply_mode(value: object) -> str:
    """Normalize legacy wire values without losing the new internal mode."""

    if isinstance(value, ReplyMode):
        return value.value
    normalized = str(value or "").strip().lower()
    if normalized in {"text", ReplyMode.TEXT_LETTER.value}:
        return ReplyMode.TEXT_LETTER.value
    if normalized in {"voice_reply", "singing_video", "voice_song_video"}:
        return normalized
    if normalized in {
        "video",
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
        "voice_reply", "singing_video", "voice_song_video",
    }:
        # The product has one video format: spoken reply plus music. Preserve
        # old persisted/wire values by upgrading them to that canonical mode.
        return ReplyMode.MUSICAL_VIDEO.value
    return ReplyMode.TEXT_LETTER.value


def _wire_reply_mode(value: object) -> str:
    exact = _exact_reply_mode(value)
    return "text" if exact == ReplyMode.TEXT_LETTER.value else "video"


_RUNTIME_DIAGNOSTIC_EVENTS: deque[tuple[int, dict[str, object]]] = deque(maxlen=160)
# Plain request lines are kept apart: a few minutes of polling used to fill the
# whole export ring and push out the failure records a support bundle needs.
_RUNTIME_REQUEST_EVENTS: deque[tuple[int, dict[str, object]]] = deque(maxlen=40)
_RUNTIME_EVENT_SEQUENCE = itertools.count()
_RUNTIME_DIAGNOSTIC_EVENT_RE = _re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUNTIME_DIAGNOSTIC_CODE_RE = _re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_RUNTIME_DIAGNOSTIC_REPLY_MODES = frozenset(
    {"text", "video", "text_letter", "normal_video", "music_video", "live", "voice_reply", "singing_video", "voice_song_video"}
)


def _runtime_diagnostic_record(event: object, fields: Mapping[str, object]) -> dict[str, object] | None:
    """Project one log event into the bounded, path-free export ring."""

    if not isinstance(event, str) or not _RUNTIME_DIAGNOSTIC_EVENT_RE.fullmatch(event):
        return None
    if event == 'daily_life_failed':
        from runtime.diagnostics.failure_context import project_daily_life_failure
        return {'event': event, **project_daily_life_failure(fields)}
    if event in {'personal_chat_transport_closed', 'personal_chat_transport_state', 'personal_chat_exchange_cancelled',
                 'personal_chat_decision_normalized', 'personal_chat_decision_warning'}:
        from runtime.diagnostics.support_bundle import _project_tail_record
        return _project_tail_record({'event': event, **fields}, runtime=True)
    if event == 'history_relationship_failed':
        from runtime.diagnostics.support_bundle import project_history_relationship_failure
        return project_history_relationship_failure({**fields, 'status': 'FAILED'})
    record: dict[str, object] = {"event": event}
    from runtime.diagnostics.failure_context import project_failure_context
    record.update(project_failure_context(fields))
    for name in ("status", "error_code"):
        value = fields.get(name)
        if isinstance(value, str) and _RUNTIME_DIAGNOSTIC_CODE_RE.fullmatch(value):
            record[name] = value
    method = fields.get("method")
    if method in {"GET", "HEAD", "OPTIONS", "POST"}:
        record["method"] = method
    reply_mode = fields.get("reply_mode")
    if reply_mode in _RUNTIME_DIAGNOSTIC_REPLY_MODES:
        record["reply_mode"] = reply_mode
    return record


def runtime_diagnostic_event_snapshot() -> tuple[dict[str, object], ...]:
    """Return a detached, bounded projection for support export only."""

    merged = sorted([*_RUNTIME_DIAGNOSTIC_EVENTS, *_RUNTIME_REQUEST_EVENTS], key=lambda item: item[0])
    return tuple(dict(record) for _, record in merged)


def _safe_log(event: str, **fields) -> None:
    """Emit structured diagnostics without request bodies, URLs or user data."""
    record = {"event": event, **fields}
    projected = _runtime_diagnostic_record(event, fields)
    if projected is not None:
        item = (next(_RUNTIME_EVENT_SEQUENCE), projected)
        (_RUNTIME_REQUEST_EVENTS if event in {"request", "cors_preflight"} else _RUNTIME_DIAGNOSTIC_EVENTS).append(item)
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))


from runtime.diagnostics.failure_context import set_failure_logger
set_failure_logger(_safe_log)


def _diagnostic_code(prefix: str, exc: Exception) -> str:
    name = type(exc).__name__.upper()
    name = _re.sub(r"[^A-Z0-9]+", "_", name).strip("_") or "ERROR"
    return f"{prefix}_{name}"[:64]

LLM_CONFIG: GatewayConfig = load_gateway_config()
LLM_TIMEOUT_SECONDS = LLM_CONFIG.timeout_seconds
LLM_CFG = LLM_CONFIG.public_dict()
LLM_CFG["persona_file"] = LLM_CONFIG.persona_file


def _letter_reply_timeout_seconds(config: GatewayConfig) -> float:
    if supports_scoped_reasoning(config):
        return config.reasoning_timeout_seconds
    return config.timeout_seconds


def _reply_writer_unavailable() -> bool:
    """True when the letter writer itself has no provider or key.

    The account key alone is not enough: a 1.x user could hold one while
    replies still pointed at a retired provider, and every letter then failed.
    """

    gateway = letters_adapter.gateway
    if isinstance(gateway, UnconfiguredAdapter):
        return True
    key = getattr(gateway, "_key", None)
    if not callable(key) or not getattr(getattr(gateway, "config", None), "requires_api_key", False):
        return False
    try:
        return not key()
    except Exception:
        return True


def apply_runtime_llm_config(
    managed: ManagedLLMConfig,
    api_key: str | None,
) -> None:
    """Atomically switch future reply requests to freshly saved local settings."""

    global LLM_CONFIG, LLM_TIMEOUT_SECONDS, LLM_CFG, reply_pipeline
    key_env = "OLIVIA_LLM_RUNTIME_KEY_CONFIGURED"
    candidate = replace(
        LLM_CONFIG,
        provider=managed.provider,
        base_url=managed.base_url,
        model=managed.model,
        api_key_env=key_env,
        api_style="chat_completions",
        stream=True,
        timeout_seconds=180.0,
        max_retries=managed.max_retries,
        requires_api_key=managed.requires_api_key,
    )
    try:
        gateway = create_gateway(
            candidate,
            key_resolver=lambda key=api_key: key,
        )
        quality_adapter = SimpleNamespace(
            config=candidate,
            gateway=gateway,
            persona_v2_path=letters_adapter.persona_v2_path,
        )
        quality_orchestrator = SimpleNamespace(
            gateway=SimpleNamespace(adapter=quality_adapter)
        )
        reviewer, rewriter = create_model_quality_ports(
            quality_orchestrator,
            gateway_factory=lambda config, key=api_key: create_gateway(
                config,
                key_resolver=lambda: key,
            ),
        )
        replacement_pipeline = ReplyPipeline(
            reply_engine,
            reviewer=reviewer or NullReviewer(),
            rewriter=rewriter or UnavailableRewriter(),
            discover_runtime_ports=False,
            current_turn_interpreter=_runtime_current_turn_interpreter(quality_orchestrator),
            recovery_root=_conversation_state_root() or _local_data_root(),
        )
    except Exception:
        raise
    letters_adapter.replace_runtime(candidate, gateway)
    if isinstance(
        private_world_candidate_analyzer,
        GatewayPrivateWorldCandidateAnalyzer,
    ):
        private_world_candidate_analyzer.gateway = gateway
        private_world_candidate_analyzer.timeout_seconds = candidate.timeout_seconds
    emotion_triage.gateway = gateway
    reply_engine.timeout_seconds = candidate.timeout_seconds
    reply_pipeline = replacement_pipeline
    LLM_CONFIG = candidate
    LLM_TIMEOUT_SECONDS = candidate.timeout_seconds
    LLM_CFG = candidate.public_dict()
    LLM_CFG["persona_file"] = candidate.persona_file
    if not api_key:
        _os.environ.pop(key_env, None)
    else:
        _os.environ[key_env] = "1"
    memory_environment = dict(_os.environ)
    # An explicit settings save replaces the shared provider for both replies
    # and memory. Startup environment values must not shadow this new binding.
    memory_environment.update({
        "OLIVIA_MEMORY_LLM_BASE_URL": candidate.base_url,
        "OLIVIA_MEMORY_LLM_MODEL": candidate.model,
        "OLIVIA_MEMORY_LLM_API_KEY_ENV": key_env,
        key_env: api_key or "",
    })
    replacement = create_conversation_memory_adapter(
        _memory_config,
        environ=memory_environment,
        llm_fallback={
            "base_url": candidate.base_url,
            "model": candidate.model,
            "api_key_env": key_env,
            "provider_options": candidate.provider_options,
        },
        defer_initialization=True,
    )
    reconfigure = getattr(conversation_memory_adapter, "reconfigure_from", None)
    if callable(reconfigure) and reconfigure(replacement):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            _start_conversation_memory_initialization(loop)


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


_CURRENT_LETTER_MEMORY_SOURCE: ContextVar[str | None] = ContextVar(
    "current_letter_memory_source",
    default=None,
)
_CURRENT_LETTER_RECEIPT: ContextVar[datetime | None] = ContextVar("current_letter_receipt", default=None)


class _LetterGateway(Gateway):
    """Bridge the legacy sync facade to the async reply orchestrator."""

    def __init__(self, adapter: "LetterAdapter") -> None:
        self.adapter = adapter
        self.stream_enabled = bool(adapter.config.stream)

    def timeout_seconds_for_scope(
        self,
        scope: GatewayRequestScope | None,
        *,
        default: float,
    ) -> float:
        from runtime.memory.recall_check import RECALL_CHECK_TIMEOUT_SECONDS
        timeout = default
        resolve = getattr(self.adapter.gateway, 'timeout_seconds_for_scope', None)
        if scope is not None and callable(resolve):
            timeout = resolve(scope, default=default)
        config = self.adapter.config
        check_enabled = (config.persona_v2_enabled and
                         config.provider in {'openai_compatible', 'openai'})
        return timeout + (RECALL_CHECK_TIMEOUT_SECONDS if check_enabled else 0)

    async def complete(self, messages, *, request_id=None) -> GatewayResponse:
        return await self._complete(messages, request_id=request_id, scope=None)

    async def complete_scoped(
        self,
        messages,
        *,
        request_id=None,
        scope: GatewayRequestScope,
    ) -> GatewayResponse:
        return await self._complete(messages, request_id=request_id, scope=scope)

    async def _complete(self, messages, *, request_id, scope) -> GatewayResponse:
        content = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        try:
            text = await asyncio.to_thread(
                self.adapter.reply,
                content,
                "",
                request_id=request_id,
                gateway_scope=scope,
            )
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
        async for delta in self._stream(messages, request_id=request_id, scope=None):
            yield delta

    async def stream_scoped(
        self,
        messages,
        *,
        request_id=None,
        scope: GatewayRequestScope,
    ):
        async for delta in self._stream(messages, request_id=request_id, scope=scope):
            yield delta

    async def _stream(self, messages, *, request_id, scope):
        content = next(
            (
                message.get("content", "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        persona = load_persona(self.adapter.persona_v2_path).snapshot if self.adapter.config.persona_v2_enabled else None
        built_messages = await asyncio.to_thread(self.adapter._messages, content, persona_snapshot=persona)
        from runtime.memory.history_selection import select_history_messages as prepare_recall_messages
        from runtime.reply.reply_pipeline import _assembled_life_projection
        built_messages = await prepare_recall_messages(
            built_messages, self.adapter.gateway,
            max_input_chars=self.adapter.config.max_input_chars, request_id=request_id,
            persona_snapshot=persona, persona_mode='text_letter',
            persona_development=(_assembled_life_projection(built_messages) or {}).get('character_development'),
        )
        stream = (
            self.adapter.gateway.stream_scoped(
                built_messages,
                request_id=request_id,
                scope=scope,
            )
            if scope is not None
            else self.adapter.gateway.stream(built_messages, request_id=request_id)
        )
        async for delta in stream:
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
        conversation_memory: ConversationMemoryPort | None = None,
        private_world_port: PrivateWorldPort | None = None,
        daily_life: DailyLifeRuntime | None = None,
        recent_letters: Callable[[], list[dict]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        runtime_config = config or LLM_CONFIG
        try:
            runtime_gateway = create_gateway(runtime_config)
        except GatewayError:
            runtime_gateway = UnconfiguredAdapter()
        self._runtime = (runtime_config, runtime_gateway)
        self.memory_port: MemoryPort = memory_port or NullMemoryPort()
        self.conversation_memory = conversation_memory
        self.daily_life = daily_life
        self.recent_letters = recent_letters
        self.private_world_port: PrivateWorldPort = (
            private_world_port or NullPrivateWorldPort()
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        persona_path = Path(runtime_config.persona_file)
        if not persona_path.is_absolute():
            persona_path = Path(__file__).resolve().parent / persona_path
        persona_config_path = Path(runtime_config.persona_config)
        if not persona_config_path.is_absolute():
            persona_config_path = Path(__file__).resolve().parent / persona_config_path
        persona_evidence_path = Path(runtime_config.persona_evidence_file)
        if not persona_evidence_path.is_absolute():
            persona_evidence_path = Path(__file__).resolve().parent / persona_evidence_path
        persona_v2_path = Path(runtime_config.persona_v2_file)
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
            feature_enabled=runtime_config.feature_enabled,
        )
        self.memory_prompt_builder = (
            MemoryPromptBuilder(self.memory_port)
            if conversation_memory is None
            else MemoryPromptBuilder(
                self.memory_port,
                conversation_memory=conversation_memory,
                max_results=100, max_tokens=300000, conversation_budget=100000,
            )
        )

    @property
    def config(self) -> GatewayConfig:
        return self._runtime[0]

    @config.setter
    def config(self, value: GatewayConfig) -> None:
        self._runtime = (value, self._runtime[1])

    @property
    def gateway(self) -> Gateway:
        return self._runtime[1]

    @gateway.setter
    def gateway(self, value: Gateway) -> None:
        self._runtime = (self._runtime[0], value)

    def replace_runtime(self, config: GatewayConfig, gateway: Gateway) -> None:
        self._runtime = (config, gateway)

    def get_system_prompt(self) -> str:
        return self.persona_provider.snapshot().system_prompt

    def get_persona_policy(self) -> str:
        """Return the authoritative public policy without private-world evidence."""

        if not self.config.persona_v2_enabled:
            return self.get_system_prompt()
        loaded = load_persona(self.persona_v2_path)
        context = ReplyContext.create(
            ReplyMode.TEXT_LETTER,
            trusted_time=TrustedTime(self._now()),
        )
        return assemble_persona(
            loaded.snapshot,
            context,
            user_input="historical relationship migration policy",
            max_units=self.config.max_input_chars,
            selected_declaration_ids=(),
        ).system_content

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

    def _messages(self, content: str, context: str = "", *, persona_snapshot=None) -> tuple[dict[str, str], ...]:
        if self.config.persona_v2_enabled:
            return self._persona_v2_messages(content, context, persona_snapshot=persona_snapshot)
        return self._legacy_messages(content, context)

    def _legacy_messages(
        self, content: str, context: str = "", *, max_input_chars=None
    ) -> tuple[dict[str, str], ...]:
        user_content = content
        if context:
            user_content = content + "\n\n" + context
        snapshot = self.persona_provider.snapshot()
        budget = self.config.max_input_chars if max_input_chars is None else max_input_chars
        remaining = max(
            0,
            budget - len(snapshot.system_prompt) - len(user_content) - 2,
        )
        if remaining:
            memory_context = self._build_memory_prompt(
                content,
                max_chars=min(
                    remaining,
                    self._memory_context_limit(),
                ),
            )
            if memory_context.text:
                user_content = user_content + "\n\n" + memory_context.text
        return self.persona_provider.messages_for(
            user_content,
            max_chars=budget,
        )

    def _persona_v2_messages(
        self, content: str, context: str = "", *, persona_snapshot=None
    ) -> tuple[dict[str, str], ...]:
        user_content = content + ("\n\n" + context if context else "")
        return self.reply_context_messages(content, mode=ReplyMode.TEXT_LETTER, user_input=user_content,
                                           persona_snapshot=persona_snapshot)

    def reply_context_messages(self, content, *, mode, max_input_chars=None, user_input=None,
                               life_fragments=None, as_of=None, persona_snapshot=None):
        from runtime.reply.reply_pipeline import assemble_reply_messages
        if not self.config.persona_v2_enabled:
            messages = self._legacy_messages(content, max_input_chars=max_input_chars)
            if persona_snapshot is not None and messages[0]['content'] != persona_snapshot.system_prompt:
                raise ValueError('PROACTIVE_PERSONA_CHANGED')
            return messages
        snapshot = persona_snapshot if persona_snapshot is not None else load_persona(self.persona_v2_path).snapshot
        context = self.build_reply_context(mode, as_of=as_of,
            **({'future_im_enabled': True} if mode is ReplyMode.FUTURE_IM else {}))
        messages, _ = assemble_reply_messages(self, snapshot, context, content,
            max_input_chars=self.config.max_input_chars if max_input_chars is None else max_input_chars,
            user_input=user_input, life_fragments=life_fragments)
        return messages

    async def prepare_daily_life_fragments(self, content: str, *, now) -> tuple[UntrustedFragment, ...]:
        if self.daily_life is None:
            return ()
        from runtime.reply.jev_questions import configured_questions
        from runtime.reply.world_context_selection import select_world_context, selection_dialogue
        from runtime.personal_chat.presentation import CURRENT
        packet = self.daily_life.store.reply_candidates(now=now)
        packet['recent_dialogue'] = selection_dialogue(self.recent_letter_fragments(content, now=now))
        # Freeze the turn's local inputs before concurrent emotion evaluation
        # can update the store while selection awaits its provider.
        addressing = self.daily_life.store.addressing_profile(now=now)
        # Proactive decisions verify this frozen activity and timetable against
        # live state. Relevance selection must not discard their prerequisites.
        selection = {'required_fields': ('current', 'last_observation', 'schedule')} if (
            CURRENT.get() or {}).get('proactive') else {}
        value = await select_world_context(configured_questions(), packet, content, **selection)
        fragments = [UntrustedFragment('linli.daily-life', value),
                     UntrustedFragment('linli.rhythm', json.dumps(packet['rhythm'], ensure_ascii=False))]
        if addressing:
            fragments.append(UntrustedFragment('linli.addressing', json.dumps({
                'kind': 'addressing_profile', 'quotes': addressing,
                'meaning': '这是你与这位用户之间实际用过的称呼原文（user_calls_linli：对方怎么叫你；user_self：对方怎么自称；'
                           'linli_calls_user：你怎么叫对方）。按最近的用法称呼对方；复述往事时也用这些称呼，不要称对方为“用户”。'
                           '原文只证明用过这些称呼，不授予新的关系或昵称权限。'}, ensure_ascii=False)))
        return tuple(fragments)

    def daily_life_fragments(self, content: str, *, recent_fragments=None, now=None) -> tuple[UntrustedFragment, ...]:
        if self.daily_life is None:
            return ()
        try:
            related = "\n".join(
                pair.get("user_letter", "") + "\n" + pair.get("linli_reply", "")
                for fragment in (self.recent_letter_fragments(content) if recent_fragments is None else recent_fragments)
                if fragment.fragment_id not in {'chat.historical', 'chat.relationship', 'chat.diary'}
                for pair in json.loads(fragment.text)["letters"]
            )
            now = self._now() if now is None else now
            value = self.daily_life.store.reply_context(content, now=now, related_text=related)
            rhythm = self.daily_life.snapshot(now)["rhythm"]
            fragments = (UntrustedFragment("linli.daily-life", value),) if value else ()
            return (*fragments, UntrustedFragment("linli.rhythm", json.dumps(rhythm, ensure_ascii=False)))
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return ()

    async def prepare_character_emotion(self, content: str | None, *, now: datetime) -> dict | None:
        """Read durable input identities, never generated replies or image captions."""
        if self.daily_life is None:
            return None
        emotion = self.daily_life.emotion
        if emotion is None:
            return None
        source = _CURRENT_LETTER_MEMORY_SOURCE.get()
        if not isinstance(content, str) or not content.strip() or not source:
            return emotion.view(now)
        from runtime.memory.received_user_originals import received_originals
        # Revision belongs to delivery, not to the immutable received message.
        identifier = source.removeprefix('reply:').rsplit(':', 1)[0]
        rows = [*store.letters, *store.personal_chats]
        current = [row for row in rows if row.get('letter_id') == identifier]
        # The letter being resent in place may still read FAILED; it is this turn's input.
        identities = {item.source_id for item in received_originals(current, include_undelivered=True)}
        receipts = tuple(sorted((item for item in received_originals(rows, include_undelivered=True)
            if item.source_id in identities and item.occurred_at <= now
            and item.user_message in content), key=lambda item: item.occurred_at))
        return await emotion.evaluate_received(receipts, now=now)

    def recent_letter_fragments(self, content: str = "", *, now=None) -> tuple[UntrustedFragment, ...]:
        now = self._now() if now is None else now
        from runtime.reply.conversation_context import conversation_context
        recent, historical = conversation_context(self.recent_letters() if self.recent_letters is not None else (), query=content,
            now=now, excluded_sources=self._memory_source_exclusions())
        fragments = tuple(UntrustedFragment(name, text) for name, text in
                     (('chat.recent', recent), ('chat.historical', historical)) if text)
        from runtime.memory.history_continuity import companion_view
        builder = companion_view(getattr(self, 'memory_prompt_builder', None))
        if builder is not None:
            relationship = builder.relationship_context(as_of=now, exclude_source_ids=self._memory_source_exclusions())
            if json.loads(relationship).get('records'):
                fragments += (UntrustedFragment('chat.relationship', relationship),)
        diary = getattr(self, 'diary', None)
        if diary is not None:
            try:
                written = diary.context(now)
            except (OSError, ValueError, sqlite3.Error):
                written = None
            if written:
                fragments += (UntrustedFragment('chat.diary', written),)
        return fragments

    @staticmethod
    def _memory_source_exclusions() -> tuple[str, ...]:
        source_id = _CURRENT_LETTER_MEMORY_SOURCE.get()
        return (source_id,) if source_id else ()

    def _build_memory_prompt(self, content: str, *, max_chars: int):
        excluded = self._memory_source_exclusions()
        if excluded:
            return self.memory_prompt_builder.build(
                content,
                max_chars=max_chars,
                exclude_source_ids=excluded,
            )
        return self.memory_prompt_builder.build(content, max_chars=max_chars)

    def _memory_context_limit(self) -> int:
        """Use the request capacity rather than a separate cost-saving memory cap."""
        if getattr(getattr(self.conversation_memory, 'config', None), 'context_max_chars', None) == 0:
            return 0
        return self.config.max_input_chars

    def build_reply_context(self, mode: ReplyMode, *, future_im_enabled: bool = False, as_of=None) -> ReplyContext:
        world_state_available = not isinstance(self.private_world_port, NullPrivateWorldPort)
        try:
            private_snapshot = self.private_world_port.snapshot()
            if not isinstance(private_snapshot, PrivateWorldSnapshot):
                private_snapshot = PrivateWorldSnapshot()
                world_state_available = False
        except Exception:
            private_snapshot = PrivateWorldSnapshot()
            world_state_available = False
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
            future_im_enabled=future_im_enabled,
            trusted_time=TrustedTime(self._now() if as_of is None else as_of),
            world_facts=facts,
            private_behavior=projected.behavior,
            world_state_available=world_state_available,
        )

    def remember_conversation(self, content: str, reply: str) -> None:
        """Write new-chat memory only when the opt-in profile is enabled."""

        if self.conversation_memory is not None:
            try:
                conversation_status = self.conversation_memory.status().status
            except Exception:
                conversation_status = "unavailable"
            if conversation_status != "disabled":
                # Canonical Mem0 delivery is owned by the durable outbox started
                # by MemoryPromptBuilder; never add a second direct write here.
                return
        if not getattr(self.memory_port, "conversation_enabled", False):
            return
        try:
            self.memory_port.remember_conversation(
                f"User sent a new letter: {str(content)[:4000]}",
                facts=(f"Assistant completed a reply: {str(reply)[:4000]}",),
            )
        except Exception:
            _safe_log("memory_write_skipped", reason="optional_backend_unavailable")

    def reply(
        self,
        content: str,
        context: str = "",
        *,
        request_id: str | None = None,
        gateway_scope: GatewayRequestScope | None = None,
    ) -> str:
        config, gateway = self._runtime
        try:
            persona = load_persona(self.persona_v2_path).snapshot if config.persona_v2_enabled else None
            messages = self._messages(content, context, persona_snapshot=persona)
            async def complete_reply():
                from runtime.memory.history_selection import select_history_messages as prepare_recall_messages
                from runtime.reply.reply_pipeline import _assembled_life_projection
                prepared = await prepare_recall_messages(
                    messages, gateway, max_input_chars=config.max_input_chars,
                    request_id=request_id,
                    persona_snapshot=persona, persona_mode='text_letter',
                    persona_development=(_assembled_life_projection(messages) or {}).get('character_development'),
                )
                completion = (
                    gateway.complete_scoped(
                        prepared,
                        request_id=request_id,
                        scope=gateway_scope,
                    )
                    if gateway_scope is not None
                    else gateway.complete(prepared, request_id=request_id)
                )
                return (await completion).text
            return asyncio.run(complete_reply())
        except GatewayError as exc:
            code = "LLM_TIMEOUT" if isinstance(exc, ProviderTimeout) else "LLM_UNAVAILABLE"
            if exc.code in {'PROVIDER_QUOTA_EXHAUSTED', 'PROVIDER_USAGE_PENDING', 'PROVIDER_REQUEST_DUPLICATE', 'PROVIDER_AUTH_FAILED'}:
                code = _public_llm_error(exc.code)[0]
            if exc.code == "PROVIDER_REJECTED":
                code = "LLM_PROVIDER_REJECTED"
            elif exc.code == "PROVIDER_PROTOCOL":
                code = "LLM_PROTOCOL_ERROR"
            _safe_log("llm_failure", provider=config.provider, error_code=code)
            raise LLMError(code) from None
        except (ValueError, RuntimeError):
            _safe_log("llm_failure", provider=config.provider, error_code="LLM_UNAVAILABLE")
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
        self.personal_chats = []  # acknowledged IM exchanges; not native inbox letters
        self.personal_chat_cursors = {}  # transport metadata, never native UI settings
        self.legacy_letters = []  # read-only imported view; never used by send/reply
        self.midi_jobs = []    # {job_id, state, filename, created_at}
        self.settings = {}
        self.request_keys = {}
        self.letter_maintenance = {}  # reversible mailbox display/time overrides

store = Store()
_store_state_error_code: str | None = None


class StoreStateUnavailable(RuntimeError):
    code = "STORE_STATE_UNAVAILABLE"


_CURRENT_STORE_CAPABILITIES = frozenset(
    {"letters.read", "letters.unread", "letters.send"}
)


def _require_store_state_available() -> None:
    if _store_state_error_code is not None:
        raise StoreStateUnavailable(_store_state_error_code)


def _local_data_root(environment: Mapping[str, str] | None = None) -> Path | None:
    configured = configured_media_path(
        _os.environ if environment is None else environment,
        "OLIVIA_LOCAL_DATA_ROOT",
    )
    return configured.resolve(strict=False) if configured is not None else None


def _default_offline_letter_pair_source() -> Path | None:
    """Find the local backup beside the original client selected at install time."""

    data_root = _local_data_root()
    if data_root is None:
        return None
    marker_path = data_root.parent / ".olivia-full-patch.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        official_source = marker.get("official_source")
        if not isinstance(official_source, str) or not official_source.strip():
            return None
        official_root = Path(official_source).expanduser()
        if not official_root.is_absolute():
            return None
        official_root = official_root.resolve(strict=True)
        source = (official_root / "letter_pairs.json").resolve(strict=True)
        source.relative_to(official_root)
        return source if source.is_file() else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _conversation_state_root() -> Path | None:
    """Return the validated Mem0-owned canonical state root, when selected."""

    adapter = globals().get("conversation_memory_adapter")
    if adapter is None:
        adapter = getattr(globals().get("letters_adapter"), "conversation_memory", None)
    config = getattr(adapter, "config", None)
    outbox_root = getattr(config, "outbox_data_root", None)
    if isinstance(outbox_root, Path) and outbox_root.is_absolute():
        return outbox_root
    data_root = getattr(config, "data_root", None)
    if not isinstance(data_root, Path) or not data_root.is_absolute():
        return None
    memory_root = (
        data_root.parent if data_root.name.casefold() == "mem0" else data_root
    )
    return (
        memory_root.parent
        if memory_root.name.casefold() == "memory"
        else memory_root
    )


def _state_root() -> Path | None:
    configured = _conversation_state_root()
    if configured is not None:
        return configured
    return _local_data_root()


def _mark_media_not_requested(letter: dict) -> None:
    if _exact_reply_mode(letter.get("reply_mode")) not in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
        "voice_reply", "singing_video", "voice_song_video",
    }:
        return
    letter["media_status"] = "NOT_REQUESTED"
    letter.pop("media_error_code", None)
    letter["media_retryable"] = False


def _read_store_state(path: Path) -> dict:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or "letters" not in loaded:
        raise ValueError("store state must be an object")
    normalized: dict[str, object] = {}
    for name in ("letters", "legacy_letters", "midi_jobs", "personal_chats"):
        value = loaded.get(name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValueError("store state contains an invalid collection")
        normalized[name] = value
    for name in ("settings", "request_keys", "personal_chat_cursors", "letter_maintenance"):
        value = loaded.get(name, {})
        if not isinstance(value, dict):
            raise ValueError("store state contains invalid metadata")
        normalized[name] = value
    return normalized


def _fsync_store_directory(root: Path) -> None:
    if _os.name == "nt":
        return
    descriptor = _os.open(root, _os.O_RDONLY)
    try:
        _os.fsync(descriptor)
    finally:
        _os.close(descriptor)


def _atomic_write_store_file(path: Path, serialized: str) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = _tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        handle = _os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor = None
        with handle:
            handle.write(serialized)
            handle.flush()
            _os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                _os.replace(temporary, path)
                break
            except OSError as exc:
                # A concurrent outbox reader on Windows can deny replacement
                # briefly. Keep the existing file intact while retrying.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except (OSError, UnicodeError):
        if descriptor is not None:
            try:
                _os.close(descriptor)
            except OSError:
                pass
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise StoreStateUnavailable(StoreStateUnavailable.code) from None
    try:
        _fsync_store_directory(path.parent)
    except OSError:
        _safe_log(
            "store_directory_sync_failed",
            error_code="STORE_STATE_DURABILITY_UNCERTAIN",
        )


def _has_saved_video_order(letter) -> bool:
    root = _local_data_root(_os.environ)
    lid = str(letter.get('letter_id', ''))
    if root is None or not _re.fullmatch(r'[A-Za-z0-9_-]{1,80}', lid):
        return False
    if letter.get('reply_video_enabled', letter.get('reply_mode') != 'voice_reply') is not True:
        return False
    path = root / 'media' / (lid + '-remote-order.private.json')
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        submission = saved['submission']
        return (isinstance(saved['fingerprint'], str) and bool(_re.fullmatch('[a-f0-9]{64}', saved['fingerprint']))
                and submission['kind'] in {'video', 'lipsync', 'cover_video', 'original_video'}
                and isinstance(submission['request_id'], str)
                and bool(_re.fullmatch('[A-Za-z0-9_-]{8,80}', submission['request_id'])))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _load_store_state() -> None:
    global _store_state_error_code
    root = _state_root()
    if root is None:
        return
    recovered_from_backup = False
    try:
        loaded = _read_store_state(root / "state.json")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as primary_error:
        try:
            loaded = _read_store_state(root / "state.json.bak")
            recovered_from_backup = True
        except FileNotFoundError:
            if isinstance(primary_error, FileNotFoundError):
                _store_state_error_code = None
                return
            _store_state_error_code = StoreStateUnavailable.code
            return
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            _store_state_error_code = StoreStateUnavailable.code
            return
    _store_state_error_code = None
    needs_persist = False
    for name in ("letters", "legacy_letters", "midi_jobs", "personal_chats"):
        value = loaded.get(name)
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            setattr(store, name, value)
            if name == "letters":
                for item in value:
                    if (
                        item.get("reply_not_before", 0)
                        and _os.environ.get("OLIVIA_REPLY_DELAY_ENABLED", "0").casefold()
                        not in {"1", "true", "yes", "on"}
                    ):
                        item["reply_not_before"] = 0.0
                        item["reply_delay_minutes"] = 0.0
                        needs_persist = True
                    item["reply_mode"] = _exact_reply_mode(
                        item.get("reply_mode", ReplyMode.TEXT_LETTER.value)
                    )
                    if item.get("letter_status") == "PROCESSING":
                        item["letter_status"] = "FAILED"
                        item["error_code"] = "LLM_INTERRUPTED"
                        _mark_media_not_requested(item)
                        needs_persist = True
                    if item.get("media_status") == "PROCESSING":
                        from runtime.remote_pipeline import enabled as remote_enabled
                        if remote_enabled(_os.environ) and _has_saved_video_order(item):
                            item.update(media_status='QUEUED', media_error_code=None, media_retryable=False)
                        else:
                            item.update(media_status="UNAVAILABLE", media_error_code="MEDIA_JOB_INTERRUPTED", media_retryable=True)
                        needs_persist = True
    if isinstance(loaded.get("settings"), dict):
        store.settings = loaded["settings"]
    if isinstance(loaded.get("request_keys"), dict):
        store.request_keys = loaded["request_keys"]
    if isinstance(loaded.get("personal_chat_cursors"), dict):
        store.personal_chat_cursors = loaded["personal_chat_cursors"]
    store.letter_maintenance = loaded.get("letter_maintenance", {})
    if needs_persist or recovered_from_backup:
        try:
            _persist_store_state()
        except StoreStateUnavailable:
            _safe_log("store_primary_repair_failed", error_code=StoreStateUnavailable.code)


_store_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="olivia-state-writer")


def _submit_store_state():
    _require_store_state_available()
    root = _state_root()
    if root is None:
        return None
    payload = {
        "letters": store.letters,
        "personal_chats": store.personal_chats,
        "personal_chat_cursors": store.personal_chat_cursors,
        "legacy_letters": store.legacy_letters,
        "midi_jobs": store.midi_jobs,
        "settings": store.settings,
        "request_keys": store.request_keys,
        "letter_maintenance": getattr(store, "letter_maintenance", {}),
    }
    # Capture mutable state on the caller thread; serialize/write snapshots in
    # submission order so a slow older save cannot overwrite a newer one.
    return _store_writer.submit(_write_store_snapshot, root, copy.deepcopy(payload))


def _write_store_snapshot(root, payload):
    global _store_state_error_code
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        _store_state_error_code = StoreStateUnavailable.code
        raise StoreStateUnavailable(StoreStateUnavailable.code) from None
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        _atomic_write_store_file(root / "state.json", serialized)
    except StoreStateUnavailable:
        _store_state_error_code = StoreStateUnavailable.code
        raise
    _store_state_error_code = None
    try:
        _atomic_write_store_file(root / "state.json.bak", serialized)
    except StoreStateUnavailable:
        _safe_log("store_backup_failed", error_code=StoreStateUnavailable.code)


def _persist_store_state() -> None:
    future = _submit_store_state()
    if future is not None:
        future.result()


async def _persist_store_state_async() -> None:
    pending = _submit_store_state()
    if pending is None:
        return
    future = asyncio.wrap_future(pending)
    try:
        await asyncio.shield(future)
    except asyncio.CancelledError:
        # A cancelled handler must not abandon a pending SENDING reservation.
        await asyncio.shield(future)
        raise


_memory_config = load_memory_config()
_archive_memory_config = (
    replace(
        _memory_config,
        enabled=False,
        provider="sqlite",
        config_error=None,
    )
    if _memory_config.provider == "mem0"
    else _memory_config
)
memory_adapter: MemoryPort = create_memory_adapter(_archive_memory_config, live_archive=True)
conversation_memory_adapter: ConversationMemoryPort = (
    create_conversation_memory_adapter(
        _memory_config,
        llm_fallback={
            "base_url": LLM_CONFIG.base_url,
            "model": LLM_CONFIG.model,
            "api_key_env": LLM_CONFIG.api_key_env,
            "provider_options": LLM_CONFIG.provider_options,
        },
        defer_initialization=True,
    )
)


def _create_video_reply_settings_store() -> VideoReplySettingsStore:
    try:
        root = _state_root()
        if root is None:
            return VideoReplySettingsStore.unavailable()
        return VideoReplySettingsStore.initialize(root)
    except (OSError, RuntimeError, TypeError, ValueError, VideoReplySettingsError):
        return VideoReplySettingsStore.unavailable()


video_reply_settings_store = _create_video_reply_settings_store()
_reply_route_previews: dict[str, tuple] = {}
_route_decision_cache: dict[str, tuple] = {}


async def _classify_managed_route(content: str, routes: dict[str, bool]) -> TriageResult:
    if video_reply_settings_store.saved_tier() == "text":
        return TriageResult("normal", "text_letter", "text_tier_selected", "completed", False)
    from runtime.reply.jev_questions import configured_questions
    from runtime.reply.companion_decision import ERROR_CODES
    port = configured_questions()
    if port is not None:
        from runtime.reply.letter_route_preview import classify
        try:
            return await classify(port, content)
        except (ValueError, RuntimeError) as exc:
            code = str(exc) if str(exc) in ERROR_CODES else 'JEV_UNAVAILABLE'
            return TriageResult('unknown', 'text_letter', code, 'unavailable', True)
    import copy
    from letter_triage import routing_context_from_environment
    ready = await asyncio.to_thread(_route_readiness)
    router = copy.copy(emotion_triage)
    base_context = getattr(router, "routing_context", None) or routing_context_from_environment(getattr(router, "environ", None))
    router.routing_context = replace(base_context,
        voice_reply_available=ready["voice_reply"], musical_video_available=ready["singing_video"],
        route_availability=ready, automatic_routes=tuple(key for key, enabled in routes.items() if enabled))
    import hashlib
    payload = {"content": content, "context": router.routing_context.to_model_dict(),
               "videos": video_reply_settings_store.videos_snapshot()}
    cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    now = time.monotonic()
    for key, value in list(_route_decision_cache.items()):
        if now - value[0] >= 300:
            _route_decision_cache.pop(key, None)
    gateway = getattr(router, "gateway", None)
    cached = _route_decision_cache.get(cache_key)
    if gateway is not None and cached is not None and cached[1] is gateway:
        return replace(cached[2], llm_called=False)
    result = await router.classify(content)
    if gateway is not None and result.status == "completed":
        if len(_route_decision_cache) >= 128:
            _route_decision_cache.pop(next(iter(_route_decision_cache)))
        _route_decision_cache[cache_key] = (time.monotonic(), gateway, result)
    return result


def _route_readiness(videos=None, *, cover=False, diagnostic=None) -> dict[str, bool]:
    environment = dict(_os.environ)
    from runtime.media.music_reply import musical_reply_configured
    if videos is None:
        videos = video_reply_settings_store.videos_snapshot()
        if video_reply_settings_store.saved_tier() == "video":
            videos["voice_reply"] = False
    from runtime.remote_pipeline import enabled as remote_enabled, capabilities as remote_capabilities
    if diagnostic is not None:
        diagnostic.clear()
        diagnostic['backend'] = 'remote' if remote_enabled(environment) else 'local'
    if remote_enabled(environment):
        try:
            kinds = set(remote_capabilities(environment)['kinds'])
        except Exception as exc:
            kinds = set()
            if diagnostic is not None:
                from runtime.cloud_service import CloudError
                allowed = {'GPU_NOT_CONFIGURED', 'GPU_TLS_FAILED', 'GPU_CONNECTION_TIMEOUT',
                           'GPU_CONNECT_FAILED', 'GPU_CONNECTION_FAILED', 'GPU_AUTH_FAILED',
                           'GPU_RESPONSE_INVALID', 'GPU_REQUEST_FAILED', 'GPU_QUEUE_FULL'}
                diagnostic['error_code'] = exc.code if isinstance(exc, CloudError) and exc.code in allowed else 'GPU_REQUEST_FAILED'
        voice = 'tts' in kinds
        song_audio = ('cover' if cover else 'original') in kinds
        speech_video = 'video' in kinds
        song = song_audio and 'lipsync' in kinds and (not cover or 'separate' in kinds)
        return {'voice_reply': speech_video if videos['voice_reply'] else voice,
                'singing_video': song if videos['singing_video'] else song_audio,
                'voice_song_video': song and speech_video if videos['voice_song_video'] else voice and song_audio}
    voice = _voice_reply_configured(environment)
    from runtime.media.ace_cover import cover_configured
    from runtime.media.original_song import original_configured
    song_audio = cover_configured(environment) if cover else original_configured(environment)
    latent = configured_media_path(environment, "OLIVIA_LATENTSYNC_ROOT")
    latent_python = configured_media_path(environment, "OLIVIA_LATENTSYNC_PYTHON")
    performance = _current_music_performance(environment)
    song = bool(song_audio and performance and performance.is_file() and latent and latent_python
                and latent_python.is_file() and all((latent / name).is_file() for name in
                    ("scripts/inference.py", "configs/unet/stage2_efficient.yaml", "checkpoints/latentsync_unet.pt")))
    separator = configured_media_path(environment, "OLIVIA_ROFORMER_PYTHON") or configured_media_path(environment, "OLIVIA_ROFORMER_EXE")
    song = bool(song and (not cover or (separator and separator.is_file() and all(
        configured_media_path(environment, key) is not None and configured_media_path(environment, key).is_file()
        for key in ("OLIVIA_ROFORMER_MODEL_PATH", "OLIVIA_ROFORMER_CONFIG_PATH")))))
    from runtime.reply.reply_media import assemble_latentsync_video_delivery
    try:
        if not voice or _local_data_root(environment) is None:
            raise ReplyMediaError("COMPLETE_VIDEO_CONFIG_UNAVAILABLE")
        assemble_latentsync_video_delivery(configured_media_path(environment, "OLIVIA_TTS_CONFIG"), _local_data_root(environment), environment)
        latent_root = configured_media_path(environment, "OLIVIA_LATENTSYNC_ROOT")
        speech_paths = [configured_media_path(environment, name) for name in ("OLIVIA_ORDINARY_ACTION_BASE", "OLIVIA_LATENTSYNC_PYTHON")]
        if latent_root is not None:
            speech_paths += [latent_root / name for name in ("scripts/inference.py", "configs/unet/stage2_efficient.yaml", "checkpoints/latentsync_unet.pt")]
        speech_video = voice and latent_root is not None and all(path is not None and path.is_file() for path in speech_paths)
    except (ReplyMediaError, TypeError, ValueError, OSError):
        speech_video = False
    return {"voice_reply": speech_video if videos["voice_reply"] else voice,
            "singing_video": song if videos["singing_video"] else song_audio,
            "voice_song_video": bool(song and speech_video and configured_media_path(environment, "OLIVIA_OFFICIAL_REPLY_REFERENCE")
                                     and configured_media_path(environment, "OLIVIA_OFFICIAL_REPLY_REFERENCE").is_file())
                                  if videos["voice_song_video"] else voice and song_audio}


def _open_video_capability_source(capability: object, source: object) -> bool:
    url = video_reply_source_url(capability, source)
    if url is None:
        return False
    try:
        return bool(_webbrowser.open(url, new=2, autoraise=True))
    except (OSError, RuntimeError):
        return False


def _video_reply_dependencies_ready() -> bool:
    environment = MappingProxyType(dict(_os.environ))
    try:
        from runtime.remote_pipeline import enabled as remote_enabled
        if remote_enabled(environment):
            return any(_route_readiness().values())
        from runtime.media.ace_cover import cover_configured
        from runtime.media.original_song import original_configured
        return _voice_reply_configured(environment) or cover_configured(environment) or original_configured(environment)
    except Exception:
        return False


private_world_runtime: PrivateWorldRuntime = create_private_world_runtime(
    user_id=_memory_config.user_id,
)
private_world_port: PrivateWorldPort = private_world_runtime.port
private_world_committer: PrivateWorldDeliveryCommitter | None = (
    private_world_runtime.committer
)
private_world_relationship_committer: PrivateWorldRelationshipCommitter | None = (
    private_world_runtime.relationship_committer
)
private_world_command_service: PrivateWorldCommandService | None = (
    PrivateWorldCommandService(private_world_runtime.port)
    if private_world_runtime.status == "available"
    else None
)
letters_adapter = LetterAdapter(
    memory_port=memory_adapter,
    conversation_memory=conversation_memory_adapter,
    private_world_port=private_world_port,
    recent_letters=lambda: [*store.letters, *(row for row in store.personal_chats if row.get("delivery_status") == "DELIVERED")],
)


def _create_daily_life_runtime() -> DailyLifeRuntime | None:
    try:
        from runtime.private_world.student_world import shanghai_weather
        path, _reason, enabled = resolve_private_world_database(user_id=_memory_config.user_id)
        if not enabled or path is None:
            _safe_log('daily_life_failed', failure_stage='initialization', endpoint='daily_life',
                      error_code='DAILY_LIFE_DISABLED' if not enabled else 'DAILY_LIFE_UNAVAILABLE',
                      recorded_at_ms=int(time.time() * 1000))
            return None
        return DailyLifeRuntime(
            DailyLifeStore(path.with_name("daily_life.sqlite3")),
            lambda: letters_adapter.gateway,
            lambda: life_persona(letters_adapter.persona_v2_path),
            emotion_persona=lambda: life_persona(letters_adapter.persona_v2_path, include_emotion_traits=True),
            relationship=lambda: letters_adapter.private_world_port.snapshot(),
            weather_provider=shanghai_weather,
            dialogue_rows=lambda: [*store.letters, *store.personal_chats],
        )
    except (OSError, RuntimeError, ValueError, TypeError, ImportError, sqlite3.Error) as exc:
        from runtime.diagnostics.failure_context import daily_life_failure
        _safe_log('daily_life_failed', recorded_at_ms=int(time.time() * 1000),
                  **daily_life_failure(exc, 'initialization'))
        return None


daily_life_runtime = _create_daily_life_runtime()
letters_adapter.daily_life = daily_life_runtime


def _create_diary_store():
    try:
        from runtime.diary.diary import DiaryStore
        path, _reason, enabled = resolve_private_world_database(user_id=_memory_config.user_id)
        if not enabled or path is None:
            return None
        return DiaryStore(path.with_name("diary.sqlite3"))
    except (OSError, RuntimeError, ValueError, TypeError, ImportError, sqlite3.Error):
        _safe_log('diary_unavailable', failure_stage='initialization')
        return None


diary_store = _create_diary_store()
letters_adapter.diary = diary_store
if diary_store is not None and getattr(letters_adapter, 'memory_prompt_builder', None) is not None:
    # Recall uses her diary as a dated index into the originals.
    letters_adapter.memory_prompt_builder.diary = diary_store


def _create_candidate_runtime() -> PrivateWorldCandidateRuntime:
    try:
        database_path, _reason, _enabled = resolve_private_world_database()
    except (OSError, RuntimeError, ValueError):
        database_path = None
    gateway_ready = (
        not isinstance(letters_adapter.gateway, UnconfiguredAdapter)
        and (
            not LLM_CONFIG.requires_api_key
            or api_key_configured(LLM_CONFIG)
        )
    )
    return create_private_world_candidate_runtime(
        letters_adapter.gateway,
        database_path=database_path,
        gateway_ready=gateway_ready,
        environ=_os.environ,
    )


private_world_candidate_runtime = _create_candidate_runtime()
private_world_candidate_analyzer: PrivateWorldCandidateAnalyzer = (
    private_world_candidate_runtime.analyzer
)
private_world_candidate_store: SQLitePrivateWorldCandidateStore | None = (
    private_world_candidate_runtime.store
)
OFFICIAL_HISTORY_PUBLISH_STATUS_KEY = "official_history_publish_status"
OFFICIAL_HISTORY_PUBLISH_STATUS_COMPLETED = "completed_v1"
OFFICIAL_HISTORY_MEMORY_SEMANTICS_KEY = "official_history_memory_semantics"
OFFICIAL_HISTORY_MEMORY_SEMANTICS_VERSION = "actor_split_first_person_v2"


def _official_history_memory_available() -> bool:
    try:
        status = conversation_memory_adapter.status()
    except Exception:
        return False
    return bool(
        status.status == "available"
        and status.enabled is True
        and status.provider == "mem0"
    )


def _missing_memory_component() -> str | None:
    import re

    status = conversation_memory_adapter.status()
    code = getattr(status, 'reason_code', None)
    if status.status == 'unavailable' and (code in {
        'MEM0_EMBEDDING_CACHE_UNAVAILABLE', 'MEM0_IMPORT_FAILED',
    } or isinstance(code, str) and re.fullmatch(
        r'MEM0_INIT_IMPORT_(?:APP|MEM0|VECTOR_STORE|EMBEDDING|LLM_CLIENT|SQLITE)_(?:IMPORT|MODULE_MISSING)', code
    )):
        return code
    return None


def _conversation_memory_ready_for_reply() -> bool:
    bootstrap = getattr(
        letters_adapter.memory_prompt_builder,
        "conversation_runtime_status",
        None,
    )
    if not isinstance(bootstrap, Mapping):
        return False
    if bootstrap.get("status") == "disabled":
        return bootstrap.get("provider") == "none"
    if bootstrap.get("status") not in {"available", "degraded"}:
        return False
    runtime = conversation_memory_reply_readiness_status()
    if (
        runtime.enabled is True
        and runtime.worker_running is True
        and runtime.status == "degraded"
        and runtime.reason_code == "MEMORY_ADMIN_PAUSED"
    ):
        return True
    return (
        runtime.enabled is True
        and runtime.worker_running is True
        and (runtime.status == "available" or runtime.reply_ready)
    )


def _official_history_private_world_available() -> bool:
    if private_world_command_service is None:
        return False
    try:
        return isinstance(private_world_port.snapshot(), PrivateWorldSnapshot)
    except Exception:
        return False


def _llm_runtime_ready(config: GatewayConfig | None = None) -> bool:
    runtime_config = config or LLM_CONFIG
    key_present = api_key_configured(runtime_config)
    return bool(
        runtime_config.feature_enabled
        and (
            runtime_config.provider == "mock"
            or (
                runtime_config.provider == "openai_compatible"
                and runtime_config.base_url
                and runtime_config.model
                and (not runtime_config.requires_api_key or key_present)
            )
        )
    )


def _official_history_llm_required() -> bool:
    # Preflight cannot identify the incoming corpus before collection, so
    # unrelated relationship state cannot prove a matching audit.
    return True


def _official_history_preflight_error() -> str | None:
    if not _official_history_memory_available():
        return "OFFICIAL_HISTORY_MEMORY_UNAVAILABLE"
    if not _official_history_private_world_available():
        return "PRIVATE_WORLD_HISTORY_UNAVAILABLE"
    if _official_history_llm_required() and not _llm_runtime_ready():
        return "OFFICIAL_HISTORY_LLM_UNAVAILABLE"
    return None


async def _migrate_historical_history(
    full_exchanges: tuple[HistoricalExchange, ...],
    *,
    skip_source_record_ids: frozenset[str] = frozenset(),
) -> HistoricalMigrationResult:
    pending_memory_exchanges = tuple(
        exchange
        for exchange in full_exchanges
        if exchange.source_record_id not in skip_source_record_ids
    )
    result = await asyncio.to_thread(
        migrate_historical_exchanges,
        pending_memory_exchanges,
        memory=conversation_memory_adapter,
        user_id=_memory_config.user_id,
        require_persisted=True,
        on_progress=lambda processed, total: _update_official_import_progress(
            status="RUNNING",
            stage="memory",
            total=total,
            processed=processed,
            retryable=False,
        ),
    )
    if result.status != "completed":
        _safe_log("history_memory_failed", status="FAILED", error_code=result.error_code or "MEM0_WRITE_FAILED")
    if result.status != "completed" or not full_exchanges:
        return result
    try:
        memory_status = conversation_memory_adapter.status().status
    except Exception:
        memory_status = "unavailable"
    if memory_status == "disabled":
        return replace(result, private_world_status="skipped_memory_disabled")
    if private_world_command_service is None:
        return replace(
            result,
            status="partial",
            private_world_status="unavailable",
            error_code="PRIVATE_WORLD_HISTORY_UNAVAILABLE",
        )
    failure_code = "PRIVATE_WORLD_HISTORY_LOOKUP_FAILED"
    try:
        command_id = historical_relationship_command_id(full_exchanges)
        existing = await asyncio.to_thread(
            private_world_command_service.lookup_command,
            command_id,
        )
        if existing is not None:
            return replace(
                result,
                private_world_status="already_initialized",
            )
        _update_official_import_progress(
            status="RUNNING",
            stage="relationship",
            total=len(full_exchanges),
            processed=len(full_exchanges),
            retryable=False,
        )
        failure_code = "PRIVATE_WORLD_HISTORY_PREPARE_FAILED"
        assessment = await assess_historical_relationship(
            full_exchanges,
            gateway=letters_adapter.gateway,
            persona_policy=letters_adapter.get_persona_policy(),
        )
        failure_code = "PRIVATE_WORLD_HISTORY_WRITE_FAILED"
        private_world_status = await asyncio.to_thread(
            apply_historical_private_world,
            full_exchanges,
            assessment=assessment,
            command_service=private_world_command_service,
        )
    except Exception as exc:
        if isinstance(exc, HistoricalRelationshipError):
            failure_code = exc.code
        _safe_log("history_relationship_failed", status="FAILED", error_code=failure_code)
        return replace(
            result,
            status="partial",
            private_world_status="unavailable",
            error_code=failure_code,
        )
    return replace(result, private_world_status=private_world_status)


async def _migrate_official_history(
    payload: Mapping[str, object],
    *,
    skip_source_record_ids: frozenset[str] = frozenset(),
) -> HistoricalMigrationResult:
    return await _migrate_historical_history(
        exchanges_from_legacy_payload(payload),
        skip_source_record_ids=skip_source_record_ids,
    )
# A file-only Mem0 profile owns the same canonical state root on restart; load
# only after the validated conversation adapter has selected that root.
_load_store_state()
emotion_triage = LetterEmotionTriage(letters_adapter.gateway)
media_semaphore = asyncio.Semaphore(1)
# Remote video waits own a separate lane; GPU exclusivity stays on the server.
video_media_semaphore = asyncio.Semaphore(1)
media_tasks: set[asyncio.Task] = set()
reply_tasks: set[asyncio.Task] = set()
private_world_candidate_tasks: set[asyncio.Task] = set()
daily_life_tasks: dict[str, asyncio.Task] = {}
reply_jobs: dict[str, asyncio.Task] = {}
media_jobs: dict[str, asyncio.Task] = {}


def _persist_media_state() -> None:
    _persist_store_state()


async def _voice_plan_for_letter(
    letter: dict,
    reply_text: str,
    *,
    mode: ReplyMode = ReplyMode.SPOKEN_VIDEO,
) -> TextOnlyVoicePlan:
    """Use frozen text directly; legacy director state never blocks synthesis."""

    return TextOnlyVoicePlan(reply_text)


async def _music_voice_plan_for_letter(
    letter: dict,
    reply_text: str,
) -> TextOnlyVoicePlan:
    """Musical reply speech uses the same text-only synthesis as ordinary replies."""

    return await _voice_plan_for_letter(letter, reply_text)


music_adapter = MusicAdapter()
reply_engine = ReplyOrchestrator(
    _LetterGateway(letters_adapter),
    timeout_seconds=LLM_CONFIG.timeout_seconds,
)
reply_pipeline = ReplyPipeline(
    reply_engine,
    reviewer=NullReviewer(),
    rewriter=UnavailableRewriter(),
    recovery_root=_conversation_state_root() or _local_data_root(),
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
    if code == 409 and error_code in {
        "IDEMPOTENCY_CONFLICT", "LETTER_IN_PROGRESS", "LETTER_RESEND_NOT_ALLOWED"
    }:
        _safe_log("letter_request_rejected", error_code=error_code)
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
    410: 410,
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
    failed = l.get("letter_status") in {"FAILED", "CANCELED", "CANCELLED"}
    metadata = l.get("metadata")
    imported_history = (
        isinstance(metadata, dict)
        and metadata.get("import_kind") == "official_text_reply"
    ) or is_published_offline_letter_pair(metadata) or is_letter_backup(metadata)
    summary = (
        l.get("content")
        if imported_history
        else l.get("reply_text") or l.get("content")
    )
    summary = summary or ""
    return {
        "letter_id": l["letter_id"],
        "origin": l.get("origin", "user"),
        "title": l.get("title", ""),
        "reply_allowed": l.get("origin") != "proactive",
        "summary": summary[:50],
        "letter_status": (
            l.get("letter_status", 4)
            if published or failed
            else "PENDING"
        ),
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
_MEDIA_NAME = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:mp4|wav|png)$")


def _media_root() -> Path | None:
    configured = _local_data_root()
    return (configured / "media").resolve() if configured is not None else None


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
    return web.FileResponse(target, headers={"Content-Type": {'.png':'image/png','.wav':'audio/wav','.mp4':'video/mp4'}[target.suffix], "Cache-Control": "no-store", **CORS_HEADERS(request)})


async def handler(request: web.Request):
    if request.path.startswith(('/toy/wardrobe/images/', '/toy/images/')):
        if request.method not in {'GET','HEAD'}:
            return web.Response(status=405)
        if request.headers.get('Origin') and not origin_allowed(request.headers['Origin']):
            return web.Response(status=403)
        from runtime.image_assets import ensure_image, _catalog
        from runtime.cloud_service import CloudError
        try:
            parts=request.path.split('/')
            if request.path.startswith('/toy/wardrobe/images/') and len(parts)==5:
                kind, asset_id='wardrobe',parts[-1]
            elif request.path.startswith('/toy/images/') and len(parts)==5:
                kind, asset_id=parts[-2:]
            else:
                return web.Response(status=404)
            target=await ensure_image(_local_data_root(),kind,asset_id)
            return web.FileResponse(target,headers={'Content-Type':_catalog()[kind][asset_id]['content_type'],
                'Cache-Control':'private, no-cache',**CORS_HEADERS(request)})
        except CloudError as exc:
            return web.Response(status=404 if exc.code in {'IMAGE_ASSET_NOT_FOUND','STICKER_PACK_NOT_INSTALLED'} else 503)
    if request.path.startswith("/toy/local-songs/media/"):
        if request.method not in {"GET", "HEAD"}:
            return web.Response(status=405)
        if request.headers.get('Origin') and not origin_allowed(request.headers['Origin']):
            return web.Response(status=403)
        try:
            root = _local_data_root()
            if root is None:
                return web.Response(status=503)
            library = LocalSongLibrary(root, _os.environ)
            name = request.path.rsplit('/', 1)[-1]
            target = library.media_path(name.removesuffix('.mp4').removesuffix('.wav'))
            return web.FileResponse(target, headers={"Content-Type": "audio/wav" if target.suffix == ".wav" else "video/mp4", **CORS_HEADERS(request)})
        except LocalSongError:
            return web.Response(status=404)
    if request.path.startswith("/toy/media/"):
        return await _media_handler(request)
    path = request.path  # /toy/xxx
    canonical_path = contract.canonical_route_path(path)
    if canonical_path.startswith("/toy/"):
        canonical_path = canonical_path.rstrip("/")
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

    if canonical_path == "/toy/cover/upload":
        if method != "POST" or request.headers.get("X-Olivia-Companion-Action") != "confirmed":
            return web.json_response(err(403, "COMPANION_CONFIRMATION_REQUIRED", {}), status=403, headers=CORS_HEADERS(request))
        from runtime.media.cover_upload import MAX_UPLOAD_BYTES, validate_upload
        root = _local_data_root()
        if root is None:
            return web.json_response(err(503, "COVER_STORAGE_UNAVAILABLE", {}), status=503, headers=CORS_HEADERS(request))
        source_id = uuid.uuid4().hex
        directory = root / "cover-inputs" / source_id
        directory.mkdir(parents=True, exist_ok=False)
        raw, audio = directory / "upload.bin", directory / "source.wav"
        try:
            count = 0
            with raw.open("wb") as stream:
                async for chunk in request.content.iter_chunked(65536):
                    count += len(chunk)
                    if count > MAX_UPLOAD_BYTES:
                        raise ValueError("COVER_UPLOAD_TOO_LARGE")
                    stream.write(chunk)
            duration = await asyncio.to_thread(validate_upload, raw, audio, _os.environ)
            from runtime.media.ace_cover import cover_paths
            from runtime.remote_pipeline import enabled as remote_enabled
            cloud_lyrics = remote_enabled(_os.environ)
            asr = None if cloud_lyrics else cover_paths(_os.environ)["asr_model"]
            return web.json_response(ok({"source_id": source_id, "duration_seconds": duration,
                                         "lyrics_on_server": cloud_lyrics,
                                         "asr_available": bool(asr and asr.is_file())}), headers=CORS_HEADERS(request))
        except (ValueError, OSError) as exc:
            raw.unlink(missing_ok=True)
            audio.unlink(missing_ok=True)
            code = str(exc) if str(exc) in {"COVER_FFMPEG_UNAVAILABLE", "COVER_UPLOAD_TOO_LARGE"} else "COVER_UPLOAD_INVALID"
            return web.json_response(err(400, code, {"error_code": code}), status=400, headers=CORS_HEADERS(request))

    body = {}
    body_error = None
    if request.can_read_body:
        try:
            if canonical_path == "/toy/letter/backup/import" or canonical_path.startswith("/toy/letter/maintenance/"):
                from runtime.imports.letter_backup import MAX_BYTES
                raw = bytearray()
                async for chunk in request.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES + 1024:
                        return web.json_response(err(400, "LETTER_BACKUP_TOO_LARGE"), status=400, headers=CORS_HEADERS(request))
            else:
                raw = await request.read()
            if raw:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    body_error = err(
                        400,
                        "INVALID_BODY",
                        {"status": "FAILED", "error_code": "INVALID_BODY", "retryable": False},
                    )
        except (UnicodeDecodeError, json.JSONDecodeError):
            body_error = err(
                400,
                "INVALID_JSON",
                {"status": "FAILED", "error_code": "INVALID_JSON", "retryable": False},
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
                defer_reply=(
                    method == "POST"
                    and canonical_path in {"/toy/letter/send", "/toy/letter/resend"}
                ),
                companion_confirmed=(
                    request.headers.get("X-Olivia-Companion-Action") == "confirmed"
                ),
            )
        except StoreStateUnavailable:
            _load_store_state()
            _safe_log(
                "route_failure",
                method=method,
                path=path,
                error_code=StoreStateUnavailable.code,
            )
            result = err(
                503,
                StoreStateUnavailable.code,
                {
                    "status": "FAILED",
                    "error_code": StoreStateUnavailable.code,
                    "retryable": False,
                },
            )
        except Exception as e:
            code = _diagnostic_code('ROUTE', e)
            _safe_log('route_failure', method=method, path=path, error_code=code)
            if canonical_path == "/toy/letter/route-preview":
                from runtime.diagnostics.failure_context import exception_context
                _safe_log("reply_route_preview_exception", status="FAILED",
                          error_code="REPLY_ROUTE_INTERNAL_ERROR", **exception_context(e, "internal"))
            result = err(500, 'INTERNAL_ERROR', {'status': 'FAILED', 'error_code': code})

    if canonical_path == "/toy/letter/route-preview":
        code = (result.get("data") or {}).get("error_code")
        fields = {"status": "COMPLETED" if result.get("code") == 0 else "FAILED"}
        if isinstance(code, str) and _RUNTIME_DIAGNOSTIC_CODE_RE.fullmatch(code):
            fields["error_code"] = code
        _safe_log("reply_route_preview_result", **fields)
    return web.json_response(
        result,
        status=response_http_status(result),
        headers=CORS_HEADERS(request),
    )

TRUSTED_FRONTEND_ORIGINS = frozenset({
    'https://olivia.local',
    'https://toy-cnbeta01.olivia.miyoushe.com',
})
ALLOWED_HEADERS = ', '.join((
    'Content-Type',
    'X-Olivia-Companion-Action',
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


def _startup_health_result() -> dict:
    """Core readiness without optional filesystem, database, or provider probes."""
    required = contract.PROFILES[contract.HEALTH_PROFILE_CORE]["required_capabilities"]
    states = {name: contract.CAPABILITIES.get(name, {}).get("status", "unavailable") for name in required}
    identity = _os.environ.get("OLIVIA_BACKEND_ID", "legacy")
    return ok({
        "schema_version": contract.HTTP_ENVELOPE_SCHEMA_VERSION,
        "contract_version": contract.HTTP_ENVELOPE_CONTRACT_VERSION,
        "backend_id": identity if _re.fullmatch(r"[0-9A-Za-z.+-]{1,160}", identity) else "invalid",
        "profile": contract.HEALTH_PROFILE_CORE,
        "status": "HEALTHY" if all(state == "available" for state in states.values()) else "FAILED",
        "required_checks": states,
    })


def _health_result(profile: str = contract.HEALTH_PROFILE_CORE) -> dict:
    profile_spec = contract.PROFILES.get(profile)
    if profile_spec is None:
        return err(
            400,
            "INVALID_PROFILE",
            {"status": "FAILED", "error_code": "INVALID_PROFILE"},
        )

    document = contract.contract_document()
    setting_snapshot = video_reply_settings_store.snapshot()
    setting_capability = document["capabilities"].get("settings.video_reply", {})
    if setting_snapshot.state == "available":
        setting_capability.update(
            {
                "status": "available",
                "provider": "local-atomic-state",
                "probe": "startup",
            }
        )
    else:
        setting_capability.update(
            {
                "status": "unavailable",
                "provider": "none",
                "reason_code": setting_snapshot.reason_code,
                "probe": "startup",
            }
        )
    document["capabilities"]["settings.video_reply"] = setting_capability
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
    try:
        conversation_info = conversation_memory_adapter.status().to_dict()
    except Exception:
        conversation_info = {
            "status": "unavailable",
            "enabled": False,
            "provider": "none",
            "storage": "none",
            "reason_code": "MEM0_STATUS_FAILED",
        }
    memory_prompt_builder = letters_adapter.memory_prompt_builder
    runtime_info = getattr(
        memory_prompt_builder,
        "conversation_runtime_status",
        None,
    )
    memory_lifecycle = (
        getattr(memory_prompt_builder, "memory_lifecycle", None)
        if getattr(memory_prompt_builder, "conversation_memory", None)
        is conversation_memory_adapter
        else None
    )
    lifecycle_unavailable = False
    try:
        if memory_lifecycle is not None and memory_lifecycle.is_paused():
            conversation_info["lifecycle"] = "paused"
            conversation_info["status"] = "degraded"
            conversation_info["reason_code"] = "MEMORY_ADMIN_PAUSED"
    except Exception:
        if memory_lifecycle is not None:
            lifecycle_unavailable = True
            conversation_info["lifecycle"] = "unavailable"
            conversation_info["status"] = "unavailable"
            conversation_info["reason_code"] = "MEMORY_ADMIN_AUDIT_UNAVAILABLE"
    if conversation_info.get("status") != "disabled" and not lifecycle_unavailable:
        try:
            live_runtime_info = conversation_memory_runtime_status().to_dict()
        except Exception:
            live_runtime_info = None
        if isinstance(live_runtime_info, dict):
            live_status = live_runtime_info.get("status")
            startup_status = (
                runtime_info.get("status") if isinstance(runtime_info, dict) else None
            )
            if live_status != "disabled" or startup_status in {"available", "disabled"}:
                runtime_info = live_runtime_info
            if live_status == "disabled" and startup_status == "available":
                runtime_info = {
                    **live_runtime_info,
                    "status": "unavailable",
                    "reason_code": "MEMORY_OUTBOX_RUNTIME_UNAVAILABLE",
                }
    if (isinstance(runtime_info, dict)
            and runtime_info.get('reason_code') == 'MEM0_INITIALIZING'
            and conversation_info.get('status') == 'unavailable'
            and conversation_info.get('reason_code') not in {None, 'MEM0_INITIALIZING'}):
        runtime_info = {**runtime_info, 'status': 'unavailable',
                        'reason_code': conversation_info['reason_code']}
    if (
        conversation_info.get("status") != "disabled"
        and not lifecycle_unavailable
        and isinstance(runtime_info, dict)
    ):
        runtime_status = str(runtime_info.get("status", "unavailable"))
        if runtime_status not in {"available", "degraded", "unavailable", "disabled"}:
            runtime_status = "unavailable"
        conversation_info["runtime"] = dict(runtime_info)
        if runtime_status != "available":
            conversation_info["status"] = (
                "unavailable" if runtime_status == "disabled" else runtime_status
            )
            runtime_reason = runtime_info.get("reason_code")
            if isinstance(runtime_reason, str):
                conversation_info["reason_code"] = runtime_reason
    conversation_selected = conversation_info.get("status") != "disabled"
    if (
        conversation_info.get("status") == "disabled"
        and memory_info.get("conversation_enabled") is True
    ):
        # SQLite remains the conversation owner when Mem0 is not selected.
        conversation_info = dict(memory_info)
    memory_info["conversation"] = conversation_info
    archive_status = str(memory_info.get("status", "unavailable"))
    conversation_status = str(conversation_info.get("status", "unavailable"))
    memory_status = conversation_status if conversation_selected else archive_status
    capability_specs = {
        "memory.local": (memory_status, "local"),
        "memory.legacy": (archive_status, "archive"),
        "memory.conversation": (conversation_status, "conversation"),
    }
    for capability, (status, _domain) in capability_specs.items():
        document["capabilities"][capability].update(
            {
                "status": status,
                "provider": (
                    conversation_info.get("provider", "none")
                    if capability == "memory.conversation"
                    else memory_info.get("provider", "none")
                )
                if status in {"available", "degraded"}
                else "none",
                "probe": "in-process" if status in {"available", "degraded"} else "not-run",
            }
        )
    llm_ready = _llm_runtime_ready()
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
    raw_backend_id = _os.environ.get("OLIVIA_BACKEND_ID", "legacy")
    backend_id = (
        raw_backend_id
        if _re.fullmatch(r"[0-9A-Za-z.+-]{1,160}", raw_backend_id)
        else "invalid"
    )
    return ok(
        {
            "schema_version": contract.HTTP_ENVELOPE_SCHEMA_VERSION,
            "contract_version": contract.HTTP_ENVELOPE_CONTRACT_VERSION,
            "backend_id": backend_id,
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
                "private_world": private_world_runtime.public_status(),
                "private_world_candidates": (
                    private_world_candidate_runtime.public_status()
                ),
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
            "retryable": False,
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


def _mark_superseded_failed_retries() -> None:
    completed = tuple(
        letter
        for letter in store.letters
        if letter.get("letter_status") == "COMPLETED" and letter.get("reply_text")
    )
    changed = False
    for failed in store.letters:
        if failed.get("letter_status") != "FAILED" or failed.get("superseded_by"):
            continue
        failed_at = failed.get("created_at")
        if isinstance(failed_at, bool) or not isinstance(failed_at, (int, float)):
            continue
        replacements = []
        for candidate in completed:
            candidate_at = candidate.get("created_at")
            if isinstance(candidate_at, bool) or not isinstance(candidate_at, (int, float)):
                continue
            retry_delay = float(candidate_at) - float(failed_at)
            if retry_delay < 0 or retry_delay > LETTER_RETRY_DEDUP_SECONDS:
                continue
            if candidate.get("content") != failed.get("content"):
                continue
            if candidate.get("material", {}) != failed.get("material", {}):
                continue
            replacements.append(candidate)
        if not replacements:
            continue
        replacement = min(replacements, key=lambda item: float(item["created_at"]))
        failed["superseded_by"] = replacement["letter_id"]
        changed = True
    if changed:
        _persist_store_state()


def _legacy_letter_collection(*, strict: bool = False) -> list[dict]:
    loaded = list(store.legacy_letters)
    adapter = memory_adapter
    if not getattr(adapter, "enabled", False):
        try:
            adapter = _legacy_import_adapter()
        except Exception:
            if strict:
                raise
            _safe_log("memory_read_skipped", domain="legacy_letters")
    if getattr(adapter, "enabled", False) and hasattr(adapter, "list_legacy"):
        try:
            archived = list(getattr(adapter, "list_legacy")())
            seen = {
                (item.get("source_record_id") or item.get("letter_id"))
                for item in archived
                if isinstance(item, Mapping)
            }
            return archived + [
                item
                for item in loaded
                if not isinstance(item, Mapping)
                or (item.get("source_record_id") or item.get("letter_id")) not in seen
            ]
        except sqlite3.OperationalError as exc:
            if "no such table: legacy_letters" in str(exc):
                return loaded
            if strict:
                raise
            _safe_log("memory_read_skipped", domain="legacy_letters")
        except Exception:
            if strict:
                raise
            _safe_log("memory_read_skipped", domain="legacy_letters")
    return loaded


def _official_account_conflicts(payload: Mapping[str, object]) -> bool:
    account_id = payload.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        return True
    incoming = account_id.strip()
    existing: set[str] = set()
    for letter in _legacy_letter_collection(strict=True):
        metadata = letter.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        if metadata.get("import_kind") != "official_text_reply":
            continue
        stored = metadata.get("official_account_id")
        if isinstance(stored, str) and stored.strip():
            existing.add(stored.strip())
    return bool(existing and existing != {incoming})


def _mailbox_created_at(letter: Mapping[str, object]) -> float:
    value = letter.get("created_at")
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _official_history_mailbox_projection(*, strict: bool = False) -> list[dict]:
    projected: list[dict] = []
    for letter in _legacy_letter_collection(strict=strict):
        metadata = letter.get("metadata")
        offline_pair = is_published_offline_letter_pair(metadata)
        official_history_completed = bool(
            isinstance(metadata, Mapping)
            and metadata.get("import_kind") == "official_text_reply"
            and metadata.get(OFFICIAL_HISTORY_PUBLISH_STATUS_KEY)
            == OFFICIAL_HISTORY_PUBLISH_STATUS_COMPLETED
        )
        backup = is_letter_backup(metadata)
        if not official_history_completed and not offline_pair and not backup:
            continue
        projected.append(
            {
                **letter,
                **(
                    {"created_at": None, "replied_at": None}
                    if offline_pair
                    else {}
                ),
                **({"created_at": (_mailbox_created_at(metadata["backup_record"])
                                  if metadata["backup_record"]["created_at"] is not None else None),
                    "replied_at": metadata["backup_record"]["replied_at"],
                    "title": metadata["backup_record"]["title"],
                    "origin": metadata["backup_record"]["origin"],
                    "reply_mode": "text_letter"} if backup else {}),
                "letter_status": "COMPLETED",
                "is_read": 1,
                "read_only": True,
            }
        )
    return projected


def _letter_collection(scope: str, *, strict: bool = False, maintenance: bool = True):
    if scope == "legacy":
        return _legacy_letter_collection()
    if maintenance:
        _mark_superseded_failed_retries()
    current = [letter for letter in store.letters if not letter.get("superseded_by")
               and (letter.get("origin") != "proactive" or letter.get("letter_status") == "COMPLETED")]
    rows = [*current, *_official_history_mailbox_projection(strict=strict)]
    if maintenance:
        from runtime.imports.letter_maintenance import project
        rows = project(rows, getattr(store, "letter_maintenance", {}))
    return sorted(
        rows,
        key=_mailbox_sort_key,
        reverse=True,
    )


def _mailbox_sort_key(letter: Mapping[str, object]) -> tuple:
    if '_maintenance_order' in letter:
        return (_mailbox_created_at(letter), 'backup', -letter['_maintenance_order'])
    metadata = letter.get("metadata")
    if is_letter_backup(metadata):
        return (_mailbox_created_at(letter), 'backup', -int(metadata.get('import_position', 0)))
    if is_published_offline_letter_pair(metadata):
        provenance = metadata[OFFLINE_LETTER_PAIR_PROVENANCE_KEY]
        return (0.0, str(provenance["source_sha256"]), -provenance["source_index"])
    return (_mailbox_created_at(letter), str(letter.get("letter_id", "")), 0)


def _bind_memory_adapter(adapter: MemoryPort) -> None:
    """Use the one local adapter for legacy retrieval without enabling chat retention."""

    global memory_adapter
    memory_adapter = adapter
    letters_adapter.memory_port = adapter
    conversation_memory = letters_adapter.conversation_memory
    letters_adapter.memory_prompt_builder = (
        MemoryPromptBuilder(adapter)
        if conversation_memory is None
        else MemoryPromptBuilder(
            adapter,
            conversation_memory=conversation_memory,
            max_results=100, max_tokens=300000, conversation_budget=100000,
        )
    )


def _legacy_import_adapter() -> MemoryPort:
    if getattr(memory_adapter, "enabled", False) and not getattr(
        memory_adapter,
        "read_only",
        False,
    ):
        return memory_adapter
    archive_config = load_memory_config()
    if archive_config.provider == "mem0":
        archive_config = replace(
            archive_config,
            enabled=False,
            provider="sqlite",
            config_error=None,
        )
    adapter = create_memory_adapter(
        archive_config,
        allow_legacy_create=True,
    )
    if getattr(adapter, "enabled", False):
        _bind_memory_adapter(adapter)
    return adapter


def _existing_legacy_source_record_ids() -> frozenset[str] | None:
    if not getattr(memory_adapter, "enabled", False):
        return frozenset()
    try:
        list_legacy = getattr(memory_adapter, "list_legacy", None)
        if callable(list_legacy):
            records = list_legacy()
        else:
            exported = memory_adapter.export_records(domains=("legacy",))
            records = exported.get("legacy")
    except sqlite3.OperationalError as exc:
        if "no such table: legacy_letters" not in str(exc):
            return None
        records = store.legacy_letters
    except Exception:
        return None
    if not isinstance(records, list):
        return None
    source_ids: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            return None
        source_id = record.get("source_record_id")
        if not isinstance(source_id, str) or not source_id:
            return None
        metadata = record.get("metadata")
        if (
            isinstance(metadata, Mapping)
            and metadata.get(OFFICIAL_HISTORY_PUBLISH_STATUS_KEY)
            == OFFICIAL_HISTORY_PUBLISH_STATUS_COMPLETED
            and metadata.get(OFFICIAL_HISTORY_MEMORY_SEMANTICS_KEY)
            == OFFICIAL_HISTORY_MEMORY_SEMANTICS_VERSION
        ):
            source_ids.add(source_id)
    return frozenset(source_ids)


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
        submitted_metadata = item.get("metadata", {})
        if not isinstance(submitted_metadata, dict):
            return None
        metadata = dict(submitted_metadata)
        metadata.pop(OFFICIAL_HISTORY_PUBLISH_STATUS_KEY, None)
        metadata.pop(OFFICIAL_HISTORY_MEMORY_SEMANTICS_KEY, None)
        metadata.pop(OFFLINE_LETTER_PAIR_PUBLISH_STATUS_KEY, None)
        metadata.pop(OFFLINE_LETTER_PAIR_PROVENANCE_KEY, None)
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
    if isinstance(code, str) and _re.fullmatch(r'JEV_[A-Z0-9_]{1,64}', code):
        return code, code in {'JEV_UNAVAILABLE', 'JEV_TIMEOUT', 'JEV_HTTP_429', 'JEV_HTTP_503'}
    for provider, public in (('PROVIDER_QUOTA_EXHAUSTED', 'LLM_QUOTA_EXHAUSTED'),
                             ('PROVIDER_USAGE_PENDING', 'LLM_USAGE_PENDING'),
                             ('PROVIDER_REQUEST_DUPLICATE', 'LLM_REQUEST_DUPLICATE'),
                             ('PROVIDER_AUTH_FAILED', 'LLM_AUTH_FAILED')):
        if code in {provider, public}:
            return public, False
    if code == "PERSONA_NOT_READY":
        return "PERSONA_NOT_READY", False
    from runtime.diagnostics.failure_context import REWRITE_ERROR_CODES
    if code in REWRITE_ERROR_CODES | {"REPLY_REWRITE_FAILED"}:
        return "REPLY_REWRITE_FAILED", False
    if code == "REPLY_QUALITY_BLOCKED":
        return "REPLY_QUALITY_BLOCKED", False
    if code == "LLM_REPLY_LENGTH_INVALID":
        return "LLM_REPLY_LENGTH_INVALID", False
    if code in {"LLM_TIMEOUT", "PROVIDER_TIMEOUT"}:
        return "LLM_TIMEOUT", True
    if code == "LLM_INTERRUPTED":
        return "LLM_INTERRUPTED", True
    if code == "MEMORY_UNAVAILABLE":
        return "MEMORY_UNAVAILABLE", True
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


def _reply_pipeline_timeout_seconds(exact_mode: str) -> float:
    """Cover recall preparation, generation, and existing quality-stage reserves."""
    from runtime.memory.recall_check import RECALL_CHECK_TIMEOUT_SECONDS

    max_reasoning = (
        exact_mode != ReplyMode.FUTURE_IM.value
        and supports_scoped_reasoning(LLM_CONFIG)
    )
    generation_timeout = (
        _letter_reply_timeout_seconds(LLM_CONFIG)
        if max_reasoning
        else float(LLM_TIMEOUT_SECONDS)
    )
    quality_config = resolve_model_quality_config(
        LLM_CONFIG,
        environ=_os.environ,
    )
    quality_timeout = (
        quality_config.reasoning_timeout_seconds
        if exact_mode == ReplyMode.TEXT_LETTER.value
        and quality_config.reasoning_timeout_seconds is not None
        else quality_config.timeout_seconds
    )
    is_letter = exact_mode == ReplyMode.TEXT_LETTER.value
    semantic_mode = is_letter or exact_mode == ReplyMode.FUTURE_IM.value
    # Retain the existing reserve even when optional semantic ports are disabled.
    quality_budget = (7.0 if is_letter else 3.0) * quality_timeout
    reviewer = getattr(reply_pipeline, 'reviewer', None)
    if semantic_mode and reviewer is not None and not isinstance(reviewer, NullReviewer):
        review_timeout = getattr(getattr(getattr(reviewer, 'adapter', None), 'config', None),
                                 'timeout_seconds', quality_config.timeout_seconds)
        review_reasoning = getattr(getattr(reviewer, '_transport', None),
                                   'reasoning_timeout_seconds', quality_config.reasoning_timeout_seconds)
        # Five layers each allow two attempts. Ordinary review runs all layers
        # concurrently (2 timeout windows); scoped review has only two slots
        # (conservative 6 windows). Letters can then perform one adjudication.
        review_windows = 2.0
        if is_letter and review_reasoning is not None:
            review_timeout, review_windows = review_reasoning, 6.0
        if is_letter:
            review_windows += 1.0
        rewriter = getattr(reply_pipeline, 'rewriter', None)
        rewrite_timeout = getattr(rewriter, 'timeout_seconds', quality_config.timeout_seconds)
        rewrite_reasoning = getattr(rewriter, 'reasoning_timeout_seconds', quality_config.reasoning_timeout_seconds)
        if is_letter and rewrite_reasoning is not None:
            rewrite_timeout = rewrite_reasoning
        # Initial review, at most one rewrite, then a complete fresh review.
        quality_budget = max(quality_budget, 2 * review_windows * review_timeout + rewrite_timeout)
    interpretation_budget = 0.0
    interpreter = getattr(reply_pipeline, 'current_turn_interpreter', None)
    if semantic_mode and (interpreter is not None or current_turn_interpretation_enabled()):
        interpretation_budget = getattr(interpreter, 'timeout_seconds',
            quality_config.reasoning_timeout_seconds or quality_config.timeout_seconds)
    return RECALL_CHECK_TIMEOUT_SECONDS + generation_timeout + quality_budget + interpretation_budget + 5.0


def _send_result_for_letter(letter: dict) -> dict:
    letter_status = letter.get("letter_status")
    if letter_status in {"PENDING", "PROCESSING"}:
        return ok(
            {
                "letter_id": letter["letter_id"],
                "letterId": letter["letter_id"],
                "status": "PENDING",
            }
        )
    if letter_status in {"CANCELED", "CANCELLED"}:
        return ok(
            {
                "letter_id": letter["letter_id"],
                "letterId": letter["letter_id"],
                "status": letter_status,
            }
        )
    if letter_status == "COMPLETED":
        from original_client_letter_contract import _photo_pending
        if _photo_pending(letter):
            return ok({'letter_id': letter['letter_id'], 'letterId': letter['letter_id'], 'status': 'PENDING'})
        if letter.get("reply_not_before", 0.0) > time.time():
            return ok({"letter_id": letter["letter_id"], "letterId": letter["letter_id"], "status": "PENDING", "reply_not_before": letter["reply_not_before"]})
        if letter.get("reply_text"):
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
        status = letter.get("letter_status")
        if status not in {"PENDING", "PROCESSING", "COMPLETED"}:
            continue
        if (
            status == "COMPLETED"
            and letter.get("reply_not_before", 0.0) <= current_time
        ):
            continue
        if letter.get("content") == content and letter.get("material", {}) == material:
            return letter
    return None


_proactive_busy = False
_proactive_task = None
_proactive_reason = 'disabled'


def _proactive_settings() -> dict:
    from runtime.reply.proactive_letters import settings, read_json
    root = _state_root()
    return settings(read_json(root / 'proactive/settings.json') if root is not None else {})


def _refresh_proactive_context(*, pending_draft=None) -> dict:
    from runtime.reply.proactive_letters import make_context, write_json
    from runtime.reply.proactive_runtime import enabled, live_profile
    rows = [row for row in store.letters if row is not pending_draft]
    if enabled():
        rows += [row for row in store.personal_chats if row is not pending_draft]
    context = make_context(rows, now=time.time())
    try:
        world = ((daily_life_runtime.store.snapshot(datetime.fromtimestamp(time.time(), timezone.utc)) if enabled()
                  else daily_life_runtime.store.exchange_state()) if daily_life_runtime is not None else {})
        context = make_context(rows, now=time.time(), world=world,
                               profile=live_profile(sys.modules[__name__]) if enabled() else None)
        from runtime.personal_chat.contact_invitation import candidate, preview_configured
        invitation = (candidate(rows, private_world_port.snapshot(), time.time())
                      if preview_configured(_state_root()) else None)
        if invitation:
            context['candidates'].insert(0, invitation)
        context['candidates'][:0] = _diary_due_candidates(rows, context)
        root = _state_root()
        if root is not None:
            write_json(root / 'proactive/context.json', context)
    except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
        context['blocked'] = True
        _safe_log('proactive_context_unavailable')
    return context


def _diary_due_candidates(rows, context) -> list:
    """A remembered day (anniversary, the user's exam, a promised date) is a reason to reach out."""
    if diary_store is None:
        return []
    from datetime import timedelta
    from runtime.diary.diary import due_facts, SHANGHAI as DIARY_ZONE
    now = datetime.now(timezone.utc)
    latest = max((row for row in rows if row.get('origin') != 'proactive' and row.get('content')
                  and (row.get('letter_status') == 'COMPLETED' or row.get('delivery_status') == 'DELIVERED')),
                 key=lambda row: str(row.get('life_received_at') or row.get('created_at') or ''), default=None)
    if latest is None:
        return []
    try:
        facts = due_facts(diary_store, now)
    except (OSError, ValueError, sqlite3.Error):
        return []
    start = datetime.combine(now.astimezone(DIARY_ZONE).date(), datetime.min.time(), DIARY_ZONE)
    used = {row.get('proactive_candidate_id') for row in rows if row.get('origin') == 'proactive'}
    result = []
    for fact in facts:
        item = {'source_id': f"reply:{latest['letter_id']}:{latest.get('reply_revision', 1)}",
                'kind': 'diary_due', 'remembered': fact}
        item['id'] = hashlib.sha256(json.dumps({'kind': 'diary_due', 'fact': fact, 'day': start.date().isoformat()},
                                               sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
        if item['id'] in used:
            continue
        item.update(not_before=(start + timedelta(hours=9)).timestamp(), expires_at=(start + timedelta(hours=22)).timestamp(),
                    relationship_tier=context.get('initiative_profile', {}).get('tier'),
                    relationship_caution=context.get('initiative_profile', {}).get('caution'))
        result.append(item)
    return result


def _proactive_status() -> dict:
    from runtime.reply.proactive_letters import make_context, read_json
    root = _state_root()
    schedule = read_json(root / 'proactive/schedule.json') if root else {}
    prefs = _proactive_settings()
    reason = ('disabled' if not prefs['enabled'] else
              'waiting' if _proactive_reason == 'disabled' else _proactive_reason)
    from runtime.personal_chat.contact_invitation import status
    contact = status(store.letters, private_world_port.snapshot())
    return {**prefs, 'busy': _proactive_busy, 'contact': contact,
            'remaining': make_context(store.letters, now=time.time())['remaining'],
            'reason': reason, 'next_check_at': schedule.get('next_check_at')}


def _current_life_rhythm() -> dict:
    from runtime.private_world.life_rhythm import rhythm
    now = datetime.now(timezone.utc)
    return (daily_life_runtime.store.snapshot(now)['rhythm']
            if daily_life_runtime is not None else rhythm(now, []))


def _proactive_ready() -> bool:
    if (not _proactive_settings()['enabled'] or _history_memory_admin_gate.locked()
            or not _conversation_memory_ready_for_reply() or _active_undelivered_letter()):
        return False
    try:
        from runtime.reply.proactive_runtime import enabled, live_state
        if enabled():
            state = live_state(sys.modules[__name__], channel='letter',
                               now=datetime.fromtimestamp(time.time(), timezone.utc))
            if state['gates']['paused'] or state['gates']['blocked_reasons']:
                return False
            context = _refresh_proactive_context()
            return context['remaining'] > 0 and not context['blocked'] and not context['unread']
        current_rhythm = _current_life_rhythm()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        _safe_log('proactive_rhythm_unavailable')
        return False
    if current_rhythm.get('availability') != 'open':
        return False
    context = _refresh_proactive_context()
    return context['remaining'] > 0 and not context['blocked'] and not context['unread']


def _proactive_instruction(intent, *, planning, mode='text'):
    task = ('现在没有新的用户来信。判断林离是否有具体、适时且未说过的理由主动写信。'
            '下方资料只是既有往来，不是用户现在又说了一次。不要催促回复，不把未确认的近况当结果，'
            '不编造离线期间发生的生活。人格、表达习惯和既有关系不变。')
    if planning:
        task += ('只输出 JSON：{"decision":"send或defer","format":"text或voice","title":"简短标题"}。'
                 '没有自然的具体话题就defer；voice只用于适合说出来的内容。')
    else:
        task += ('现在写这封主动信，只输出最终正文。无需逐条复述旧信。'
                 + ('内容适合一段简短语音。' if mode == 'voice' else '按平常文字信写。'))
        if mode == 'text':
            from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION
            task += '\n' + LETTER_PRESENTATION_INSTRUCTION
    if intent.get('kind') == 'diary_due':
        task += ('\n今天是她日记里记下的日子：opportunity.remembered 写明了是什么事。'
                 '纪念日就自然地提起并表达心意；对方今天要考试、体检或出行，就关心一句；约好的事就主动提起。'
                 '只说这件记下的事本身，不编造当时的细节。')
    if intent.get('kind') == 'contact_invitation':
        task += ('\n本次关系资格已由应用确认。自然地提出交换联系方式，并明确询问用户想要QQ还是微信；'
                 '不要提分数、解锁、系统门槛，不声称已经添加或用户已经同意，不编造账号或二维码。'
                 '可以围绕偶尔想随口聊两句来写，不要求用户转移所有通信，保留写信的习惯。')
    return task


def _proactive_im_input_signature():
    """Local input-only digest; delivery/emotion updates are not new speech."""
    inputs = []
    for row in [*store.letters, *store.personal_chats]:
        if row.get('origin') == 'proactive':
            continue
        identity = (row.get('letter_id'), row.get('input_revision', 0), row.get('content', ''),
                    sorted(str(key) for key in row.get('source_messages', {})))
        inputs.append(hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).digest())
    # Older pending rows may merge even when a newer row exists. Storage order
    # and background completion do not change the bounded signature.
    return hashlib.sha256(b''.join(sorted(inputs))).hexdigest()


async def _prepare_proactive_turn(intent: dict, *, now: datetime) -> dict:
    """Freeze one opportunity's optional expression state before any paid call."""
    from runtime.reply.proactive_runtime import enabled, live_state
    development = enabled()
    sources = [*store.letters, *store.personal_chats] if development else store.letters
    source_id = intent.get('previous_source_id', intent['source_id'])
    source = next((row for row in sources
                   if f"reply:{row.get('letter_id')}:{row.get('reply_revision', 1)}" == source_id), None)
    life_opportunity = development and intent.get('kind') in {'life_share', 'affection_checkin'}
    if source is None and not life_opportunity:
        raise ValueError('PROACTIVE_SOURCE_UNAVAILABLE')
    source = source or {}
    initiative_state = live_state(sys.modules[__name__], channel='letter', now=now) if development else None
    im_input_signature = _proactive_im_input_signature()
    intent = json.loads(json.dumps(intent))
    query = str(source.get('content', ''))
    packet = {'opportunity': intent, 'previous_user_letter': query,
              'previous_linli_letter': source.get('reply_text', ''),
              'now': now.isoformat()}
    packet_text = json.dumps(packet, ensure_ascii=False)
    budget = letters_adapter.config.max_input_chars
    # Estimate only static required Persona cost. This reads no prior messages,
    # memory lookup or model call. The shared context selection below happens
    # before planning, so even a deferred plan may pay for that one selection.
    if letters_adapter.config.persona_v2_enabled:
        persona = load_persona(letters_adapter.persona_v2_path).snapshot
        context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(now))
        baseline = assemble_persona(persona, context, user_input=query or '没有新的用户来信',
                                    max_units=budget, selected_declaration_ids=())
        from runtime.reply.fact_attribution import prepare_dialogue_messages
        projected = prepare_dialogue_messages(baseline.to_messages(), max_input_chars=budget)
        added = sum(len(m['content']) for m in projected) - len(baseline.system_content) - len(baseline.user_content)
        required = baseline.budget_report.required_units + 512 + max(0, added)
    else:
        persona = letters_adapter.persona_provider.snapshot()
        required = len(persona.system_prompt) + len(query) + 2
    task_reserve = max(len(_proactive_instruction(intent, planning=True)),
        *(len(_proactive_instruction(intent, planning=False, mode=mode)) for mode in ('text', 'voice')))
    world = None
    for fragment in letters_adapter.daily_life_fragments('', recent_fragments=(), now=now):
        if fragment.fragment_id == 'linli.daily-life':
            try:
                world = json.loads(fragment.text)
            except (TypeError, ValueError):
                pass
    try:
        emotion = await letters_adapter.prepare_character_emotion(None, now=now)
    except Exception:
        emotion = None
    from runtime.reply.character_emotion_context import render_expression_blocks
    common, adopted = render_expression_blocks(world, emotion,
        max_input_chars=budget - len(packet_text) - task_reserve - required)
    # One selection precedes planning. A deferred opportunity may therefore pay
    # this bounded selection call, but the body never selects or reads again.
    base_budget = budget - task_reserve - sum(map(len, common)) - len(packet_text)
    if base_budget < 1:
        raise ValueError('INPUT_TOO_LONG')
    assembled = await asyncio.to_thread(letters_adapter.reply_context_messages,
        query or '没有新的用户来信', mode=ReplyMode.TEXT_LETTER, max_input_chars=base_budget,
        life_fragments=(), as_of=now, persona_snapshot=persona)
    history = assembled[:-1] if assembled and assembled[-1].get('role') == 'user' else assembled
    from runtime.memory.history_selection import select_history_messages
    selected = await select_history_messages(
        (*history, {'role':'user', 'content':packet_text}), letters_adapter.gateway,
        max_input_chars=base_budget + len(packet_text), request_id='proactive:' + intent['id'] + ':context',
        memory_builder=letters_adapter.memory_prompt_builder, as_of=now,
        exclude_source_ids=letters_adapter._memory_source_exclusions(), current_user_text=None,
        persona_snapshot=persona if letters_adapter.config.persona_v2_enabled else None,
        persona_mode='text_letter', persona_development=(adopted['world'] or {}).get('character_development'))
    return {'intent': intent, 'query': query, 'previous_reply': packet['previous_linli_letter'],
            'as_of': now, 'packet': packet_text, 'common_blocks': common, 'adopted': adopted,
            'max_input_chars': budget, 'persona_snapshot': persona,
            'persona_v2': letters_adapter.config.persona_v2_enabled,
            'im_input_signature': im_input_signature, 'context_messages':tuple(selected[:-1]),
            'initiative_state': initiative_state, 'source_id': source_id,
            'life_opportunity': life_opportunity}


def _proactive_turn_current(intent, turn):
    if _proactive_im_input_signature() != turn['im_input_signature']:
        return False
    if intent.get('id') != turn['intent'].get('id') or intent.get('source_id') != turn['intent'].get('source_id'):
        return False
    if turn.get('initiative_state') is not None:
        from runtime.reply.proactive_runtime import live_state, state_binding
        try:
            live = live_state(sys.modules[__name__], channel='letter',
                              now=datetime.fromtimestamp(time.time(), timezone.utc))
            if state_binding(live) != state_binding(turn['initiative_state']):
                return False
        except RuntimeError:
            return False
        if turn.get('life_opportunity') and not turn['query']:
            return True
    return any(f"reply:{row.get('letter_id')}:{row.get('reply_revision', 1)}" == turn.get('source_id', intent.get('source_id'))
               and row.get('content', '') == turn['query'] and row.get('reply_text', '') == turn['previous_reply']
                for row in [*store.letters, *store.personal_chats])


def _proactive_opportunity_current(intent, *, pending_draft=None):
    """Recheck live publication eligibility without changing the frozen view."""
    from runtime.reply.proactive_letters import scan_pending
    if pending_draft is not None and (
            pending_draft.get('origin') != 'proactive'
            or pending_draft.get('letter_status') != 'PROCESSING'
            or pending_draft.get('proactive_candidate_id') != intent.get('id')):
        return False
    if not _proactive_settings()['enabled'] or _active_undelivered_letter(exclude_letter=pending_draft):
        return False
    from runtime.reply.proactive_runtime import enabled, live_state
    if enabled():
        try:
            state = live_state(sys.modules[__name__], channel='letter',
                now=datetime.fromtimestamp(time.time(), timezone.utc),
                exclude_id=pending_draft.get('letter_id') if pending_draft is not None else None)
            if state['gates']['paused'] or state['gates']['blocked_reasons']:
                return False
        except RuntimeError:
            return False
    context = _refresh_proactive_context(pending_draft=pending_draft)
    if context['blocked'] or context['unread'] or context['remaining'] <= 0:
        return False
    if enabled():
        stamp = time.time()
        return any(item.get('id') == intent.get('id') and item.get('source_id') == intent.get('source_id')
                   and item['not_before'] <= stamp < item['expires_at'] for item in context['candidates'])
    root = _state_root()
    current = scan_pending(root) if root is not None else {}
    return (current.get('id') == intent.get('id')
            and current.get('source_id') == intent.get('source_id'))


async def _proactive_complete(intent: dict, *, planning: bool, turn: dict, mode: str = 'text') -> str:
    """Two stages reuse one frozen view; neither treats old query as new input."""
    if not _proactive_turn_current(intent, turn):
        raise ValueError('PROACTIVE_SOURCE_UNAVAILABLE')
    if letters_adapter.config.persona_v2_enabled != turn['persona_v2']:
        raise ValueError('PROACTIVE_PERSONA_CHANGED')
    task = _proactive_instruction(turn['intent'], planning=planning, mode=mode)
    # Preserve the complete shared context, including native dialogue roles.
    # Only the current task changes: an opportunity is not a new user message.
    messages = turn['context_messages']
    gateway = letters_adapter.gateway
    request_id = 'proactive:' + intent['id'] + (':plan' if planning else ':body')
    # Shared blocks are appended after all recall/compaction. They cannot be
    # individually cropped or re-rendered differently in the body stage.
    messages = (*messages, *({'role': 'system', 'content': block} for block in turn['common_blocks']),
                {'role': 'user', 'content': turn['packet']})
    if turn.get('proactive_result') is not None:
        from runtime.reply.proactive_runtime import project_decision
        messages = project_decision(messages, turn['proactive_result'], max_input_chars=turn['max_input_chars'] - len(task))
    from runtime.reply.fact_attribution import finalize_reply_messages
    messages = finalize_reply_messages(messages, task,
        max_input_chars=turn['max_input_chars'])
    scope = GatewayRequestScope.PROACTIVE_PLANNING if planning else GatewayRequestScope.BACKGROUND_REASONING
    result = await asyncio.wait_for(gateway.complete_scoped(
        messages, request_id=request_id,
        scope=scope), timeout=gateway.timeout_seconds_for_scope(scope, default=180))
    text = result.text.strip()
    if not text or len(text) > 10000 or '<think' in text.lower() or '</think' in text.lower():
        raise ValueError('PROACTIVE_RESPONSE_INVALID')
    return text


async def _publish_proactive(intent: dict, plan: dict, *, turn: dict) -> None:
    global _proactive_busy
    if (not _proactive_ready() or not _proactive_turn_current(intent, turn)
            or not _proactive_opportunity_current(intent)):
        return
    if intent.get('kind') == 'contact_invitation':
        from runtime.personal_chat.contact_invitation import status, preview_configured
        if not preview_configured(_state_root()) or status(store.letters, private_world_port.snapshot())['state'] != 'eligible':
            return
    if not _history_memory_admin_gate.acquire(blocking=False):
        return
    _proactive_busy = True
    letter = None
    try:
        body = await _proactive_complete(intent, planning=False, mode=plan['format'], turn=turn)
        if intent.get('kind') == 'contact_invitation' and ('QQ' not in body.upper() or '微信' not in body):
            raise ValueError('PROACTIVE_CONTACT_INVITATION_INCOMPLETE')
        signature = None
        if plan['format'] == 'text':
            from runtime.reply.letter_presentation import split_signature
            body, signature = split_signature(body)
            if not body.strip():
                raise ValueError('PROACTIVE_RESPONSE_INVALID')
        if not _proactive_turn_current(intent, turn) or not _proactive_opportunity_current(intent):
            return
        if intent.get('kind') == 'contact_invitation':
            from runtime.personal_chat.contact_invitation import status
            if status(store.letters, private_world_port.snapshot())['state'] != 'eligible':
                return
        letter = {'letter_id': str(uuid.uuid4()), 'origin': 'proactive', 'content': '',
                  'title': plan['title'], 'material': {}, 'created_at': int(time.time()),
                  'letter_status': 'PROCESSING', 'is_read': 0, 'reply_text': '',
                  'proactive_candidate_id': intent['id'], 'reply_mode': 'text',
                  'proactive_kind': intent.get('kind'),
                  'media_status': 'NOT_REQUESTED', 'reply_video_enabled': False}
        if turn.get('proactive_result') is not None:
            from runtime.reply.proactive_runtime import record_decision
            letter['proactive_decision'] = record_decision(turn['proactive_result'])
        _prepare_private_world_delivery(letter, body)
        letter['reply_text'] = body
        letter['reply_signature'] = signature
        store.letters.append(letter)
        _persist_store_state()
        if plan['format'] == 'voice' and _proactive_settings()['allow_voice']:
            letter['reply_mode'] = 'voice_reply'
            try:
                await asyncio.wait_for(_render_media_job(letter['letter_id'], '', body, 'voice_reply'), timeout=300)
            except asyncio.TimeoutError:
                letter.update(media_status='FAILED', media_error_code='MEDIA_TIMEOUT', media_retryable=False)
            if letter.get('media_status') != 'COMPLETED':
                letter['reply_mode'] = 'text'
        if (not _proactive_turn_current(intent, turn)
                or not _proactive_opportunity_current(intent, pending_draft=letter)):
            letter['letter_status'] = 'CANCELED'
            _persist_store_state()
            return
        now = datetime.now(timezone.utc)
        letter.update(letter_status='COMPLETED', published_at=now.timestamp(),
                      created_at=int(now.timestamp()), replied_at=int(now.timestamp()),
                      private_world_occurred_at=now.isoformat(), daily_life_status='PENDING')
        from runtime.reply.character_emotion_context import freeze_expression_context, store_expression_context
        snapshot = freeze_expression_context('proactive:' + intent['id'] + ':body', turn['as_of'],
            world=turn['adopted']['world'], emotion=turn['adopted']['emotion'])
        store_expression_context(letter, snapshot, body)
        _persist_store_state()
        _commit_private_world_letter(letter)
        if letter.get('reply_audio_url') and letter.get('media_status') == 'COMPLETED':
            root = _local_data_root()
            if root is not None:
                _record_published_media(letter, reply_text=body, delivery_id=letter['private_world_delivery_id'],
                                        path=root / 'media' / (letter['letter_id'] + '.wav'),
                                        components=('speech',), presentation='audio')
        _persist_store_state()
        _schedule_daily_life_exchange(letter)
    finally:
        try:
            if letter is not None and letter.get('letter_status') == 'PROCESSING':
                letter.update(letter_status='FAILED', error_code='PROACTIVE_INTERRUPTED')
                _persist_store_state()
        finally:
            _proactive_busy = False
            _history_memory_admin_gate.release()


async def _jev_proactive_tick() -> None:
    """Development-only Jev contact decision; existing publication owns delivery."""
    global _proactive_reason
    from runtime.reply.proactive_letters import DAY, read_json, write_json, scan_pending
    from runtime.reply.proactive_runtime import contact_slot, packet, decide, record_decision
    root = _state_root()
    if root is None or not _proactive_settings()['enabled']:
        _proactive_reason = 'disabled'
        return
    if not _proactive_ready():
        _proactive_reason = 'waiting'
        return
    with contact_slot(sys.modules[__name__], 'letter') as acquired:
        if not acquired:
            return
        now = datetime.fromtimestamp(time.time(), timezone.utc)
        schedule = read_json(root / 'proactive/schedule.json')
        if now.timestamp() < schedule.get('next_check_at', 0):
            return
        attempts = [item for item in schedule.get('attempts', [])
                    if isinstance(item, dict) and isinstance(item.get('id'), str)
                    and type(item.get('at')) in (int, float) and item['at'] > now.timestamp() - DAY]
        from runtime.reply.proactive_runtime import live_profile
        profile = live_profile(sys.modules[__name__])
        if len(attempts) >= profile.letter_daily_limit * 3:
            _proactive_reason = 'waiting'
            return
        intent = scan_pending(root, excluded_ids={item['id'] for item in attempts})
        if not intent:
            _proactive_reason = 'no_opportunity'
            return
        attempts.append({'id': intent['id'], 'at': now.timestamp()})
        schedule = {'next_check_at': now.timestamp() + profile.letter_followup_delay, 'attempts': attempts}
        write_json(root / 'proactive/schedule.json', schedule)
        turn = await _prepare_proactive_turn(intent, now=now)
        state = turn['initiative_state']
        kinds = {'correspondence_followup': 'followup', 'shared_followup': 'shared_topic', 'diary_due': 'shared_topic',
                 'relationship_checkin': 'connection', 'affection_checkin': 'connection',
                 'contact_invitation': 'connection', 'life_share': 'daily_share'}
        offered = [{'id': intent['id'], 'kind': kinds[intent['kind']],
                    'description': json.dumps(intent, ensure_ascii=False, separators=(',', ':'))}]
        value = packet(channel='letter', now=now, profile=state['profile'], world=turn['adopted']['world'],
            rhythm=state['snapshot']['rhythm'], emotion=turn['adopted']['emotion'],
            messages=turn['context_messages'], opportunities=offered, rows=state['rows'],
            available_media=['text'], hard_gates=state['gates'])
        result = await decide(value)
        schedule['last_decision'] = record_decision(result)
        write_json(root / 'proactive/schedule.json', schedule)
        turn['proactive_result'] = result
        if result.decision['action'] != 'send':
            _proactive_reason = 'deferred'
            return
        if not _proactive_turn_current(intent, turn) or not _proactive_opportunity_current(intent):
            _proactive_reason = 'deferred'
            return
        _proactive_reason = 'writing'
        await _publish_proactive(intent, {'decision': 'send', 'format': 'text', 'title': '想和你说说话'}, turn=turn)
        _refresh_proactive_context()
        scan_pending(root)
        _proactive_reason = 'waiting'


async def _proactive_tick() -> None:
    global _proactive_reason
    from runtime.reply.proactive_runtime import enabled
    if enabled():
        await _jev_proactive_tick()
        return
    from runtime.reply.proactive_letters import DAY, read_json, write_json, scan_pending
    root = _state_root()
    if root is None or not _proactive_settings()['enabled']:
        _proactive_reason = 'disabled'
        return
    if not _proactive_ready():
        _proactive_reason = 'waiting'
        return
    schedule = read_json(root / 'proactive/schedule.json')
    now = time.time()
    intent = scan_pending(root)
    contact = intent.get('kind') == 'contact_invitation'
    if not contact and now < schedule.get('next_check_at', 0):
        return
    # A decision attempt counts even when the provider fails, defers, or the
    # process exits. Keep both limits on disk, independent of model/provider.
    attempts = [item for item in schedule.get('attempts', [])
                if isinstance(item, dict) and isinstance(item.get('id'), str)
                and type(item.get('at')) in (int, float) and item['at'] > now - DAY]
    contact_attempts = [item for item in attempts if item['id'] == 'contact-invitation-v1']
    ordinary_attempts = [item for item in attempts if item['id'] != 'contact-invitation-v1']
    if (contact and (len(contact_attempts) >= 3 or
                     any(now - item['at'] < 300 for item in contact_attempts))
            or not contact and len(ordinary_attempts) >= 3):
        _proactive_reason = 'waiting'
        return
    if not contact:
        intent = scan_pending(root, excluded_ids={item['id'] for item in attempts})
    if not intent:
        _proactive_reason = 'no_opportunity'
        return
    # Persist before provider calls; retries/restarts cannot turn five-minute
    # cheap checks into an unbounded sequence of paid decisions.
    attempts.append({'id': intent['id'], 'at': now})
    write_json(root / 'proactive/schedule.json', {'next_check_at': now + 3600, 'attempts': attempts})
    _proactive_reason = 'considering'
    turn = await _prepare_proactive_turn(intent, now=datetime.fromtimestamp(now, timezone.utc))
    # Eligibility already supplies a concrete reason. The model writes the
    # invitation in character, but does not postpone it or add media latency.
    plan = ({'decision': 'send', 'format': 'text', 'title': '想和你聊两句'} if contact
            else json.loads(await _proactive_complete(intent, planning=True, turn=turn)))
    if (not isinstance(plan, dict) or set(plan) != {'decision', 'format', 'title'}
            or plan['decision'] not in {'send', 'defer'} or plan['format'] not in {'text', 'voice'}
            or not isinstance(plan['title'], str) or not 1 <= len(plan['title']) <= 40):
        raise ValueError('PROACTIVE_PLAN_INVALID')
    # User activity during planning wins. Re-read the newly projected source,
    # not the background worker's stale decision.
    _refresh_proactive_context()
    current = scan_pending(root)
    if plan['decision'] == 'send' and current.get('id') == intent['id'] and _proactive_turn_current(current, turn) and _proactive_ready():
        _proactive_reason = 'writing'
        await _publish_proactive(current, plan, turn=turn)
        _refresh_proactive_context()
        scan_pending(root)
        _proactive_reason = 'waiting'
    else:
        _proactive_reason = 'deferred'


async def _diary_tick(now=None) -> None:
    from runtime.diary.diary import due_days, write_day, day_life
    from runtime.private_world.daily_life_runtime import life_persona
    if diary_store is None or not diary_store.enabled():
        return
    now = now or datetime.now(timezone.utc)
    rows = [*store.letters, *store.personal_chats]
    for day in due_days(diary_store, now)[:2]:
        try:
            outcome = await write_day(diary_store, letters_adapter.gateway, day, rows,
                persona=life_persona(letters_adapter.persona_v2_path),
                life=day_life(getattr(daily_life_runtime, 'store', None), day), now=now)
        except (GatewayError, asyncio.TimeoutError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            diary_store.failed(day, type(exc).__name__)
            _safe_log('diary_write_failed', error_code=getattr(exc, 'code', None) or type(exc).__name__)
            continue
        if outcome == 'empty':
            diary_store.skip(day, 'EMPTY_DAY')
        else:
            _safe_log('diary_written')


_memoir_state = {'running': False, 'done': 0, 'failed': 0, 'total': 0}


async def _write_memoirs(months) -> None:
    from runtime.diary.diary import write_month
    from runtime.private_world.daily_life_runtime import life_persona
    _memoir_state.update(running=True, done=0, failed=0, total=len(months))
    try:
        rows = [*store.letters, *store.personal_chats]
        for month in months:
            try:
                await write_month(diary_store, letters_adapter.gateway, month, rows,
                                  persona=life_persona(letters_adapter.persona_v2_path))
                _memoir_state['done'] += 1
            except (GatewayError, asyncio.TimeoutError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                _memoir_state['failed'] += 1
                _safe_log('diary_memoir_failed', error_code=getattr(exc, 'code', None) or type(exc).__name__)
    finally:
        _memoir_state['running'] = False


def _running_version() -> str:
    try:
        value = json.loads((Path(__file__).resolve().parent / 'installer' / 'release-version.json').read_text('utf-8'))['version']
        return value if isinstance(value, str) and len(value) <= 32 else 'unknown'
    except (OSError, ValueError, KeyError, TypeError):
        return 'unknown'


async def _improve_post(path, payload):
    from runtime.improve.upload import post_json
    await asyncio.to_thread(post_json, getattr(letters_adapter.config, 'base_url', ''), path, payload)


async def _improve_loop() -> None:
    from runtime.improve.upload import upload_once
    await asyncio.sleep(300)
    while True:
        root = _state_root()
        if root is not None:
            try:
                await upload_once(root, [*store.letters, *store.personal_chats], _improve_post,
                                  client_version=_running_version())
            except (OSError, RuntimeError, ValueError, TypeError):
                _safe_log('improve_upload_deferred')
        await asyncio.sleep(1800)


async def _vector_index_loop() -> None:
    """Embed earlier exchanges in the background so recall can match by meaning."""
    await asyncio.sleep(180)
    while True:
        indexed = 0
        index = getattr(conversation_memory_adapter, 'index_original_vectors', None)
        if callable(index):
            try:
                indexed = await asyncio.to_thread(index, _memory_config.user_id, limit=64)
            except Exception:
                _safe_log('memory_vector_index_deferred')
        await asyncio.sleep(20 if indexed else 600)


async def _diary_loop() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await _diary_tick()
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
            _safe_log('diary_check_unavailable')
        await asyncio.sleep(900)


async def _proactive_loop() -> None:
    global _proactive_reason
    # A prepared login opportunity is checked after runtime initialization;
    # fresh opportunities use a five-minute startup window.
    from runtime.reply.proactive_letters import read_json
    root = _state_root()
    prepared = read_json(root / 'proactive/pending.json') if root else {}
    await asyncio.sleep(10 if prepared else 300)
    while True:
        try:
            await _proactive_tick()
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, GatewayError, asyncio.TimeoutError, sqlite3.Error):
            _proactive_reason = 'retry_later'
            _safe_log('proactive_check_unavailable')
        await asyncio.sleep(300)


def _active_undelivered_letter(*, now: float | None = None, exclude_letter=None) -> dict | None:
    from original_client_letter_contract import _photo_pending

    current_time = time.time() if now is None else now
    for letter in store.letters:
        if letter is exclude_letter:
            continue
        if _photo_pending(letter):
            return letter
        if letter.get("letter_status") in {"PENDING", "PROCESSING"}:
            return letter
        if (
            letter.get("letter_status") == "COMPLETED"
            and letter.get("reply_not_before", 0.0) > current_time
        ):
            return letter
    return None


def _original_request_route(material):
    from letter_triage import TriageResult
    from runtime.media.music_options import validate
    if material.get('cover_source_id') is not None or material.get('original_output') not in ('audio', 'video'):
        raise ValueError('ORIGINAL_OUTPUT_INVALID')
    validate(material.get('music_options', {}))
    return TriageResult('normal', 'singing_video', 'explicit_original_request', 'completed', False,
        music_contexts=('explicit_performance_or_adaptation_request',
            'explicit_audio_output_request' if material['original_output']=='audio' else 'explicit_video_output_request'),
        music_intent='compose', music_role='spontaneous_motif', request_disposition='fulfill',
        direct_response_sufficient=False, music_materially_better=True)


def _cover_request_route(material):
    from runtime.media.cover_options import validate
    validate(material.get('cover_options', {}))
    from runtime.media.cover_upload import source_path
    from letter_triage import TriageResult
    source_path(_local_data_root(), material.get('cover_source_id'))
    output = material.get('cover_output', 'audio')
    if output not in ('audio', 'video'):
        raise ValueError('COVER_OUTPUT_INVALID')
    return TriageResult('normal', 'singing_video', 'explicit_cover_attachment', 'completed', False,
        music_contexts=('explicit_performance_or_adaptation_request',
                        'explicit_audio_output_request' if output == 'audio' else 'explicit_video_output_request'),
        music_intent='adapt', music_role='adaptation', request_disposition='fulfill',
        direct_response_sufficient=False, music_materially_better=True)


async def route(
    method,
    path,
    body,
    query,
    *,
    defer_reply: bool = False,
    companion_confirmed: bool = False,
    _local_import_worker: bool = False,
    _history_gate_owned: bool = False,
):
    canonical_path = contract.canonical_route_path(path)
    p = (
        canonical_path.rstrip("/")
        if canonical_path.startswith("/toy/")
        else canonical_path
    )
    official_import = False

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
                "retryable": contract.error_metadata("METHOD_NOT_ALLOWED")["retryable"],
                "allowed_methods": spec["methods"],
            },
        )
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return err(400, "INVALID_BODY", {
            "status": "FAILED",
            "error_code": "INVALID_BODY",
            "retryable": contract.error_metadata("INVALID_BODY")["retryable"],
        })
    if query is None:
        query = {}
    if (
        spec["capability"] in _CURRENT_STORE_CAPABILITIES
        and query.get("scope", "current") == "current"
    ):
        _require_store_state_available()
    if p == "/health":
        if query.get("probe") == "startup" and query.get("profile", contract.HEALTH_PROFILE_CORE) == contract.HEALTH_PROFILE_CORE:
            return _startup_health_result()
        return _health_result(query.get("profile", contract.HEALTH_PROFILE_CORE))
    if p == "/toy/proactive/status":
        return ok(_proactive_status())
    if p == "/toy/proactive/settings":
        if companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {})
        from runtime.reply.proactive_letters import DEFAULTS, write_json
        if not body or set(body) - set(DEFAULTS) or any(type(value) is not bool for value in body.values()):
            return err(400, "PROACTIVE_SETTINGS_INVALID", {})
        root = _state_root()
        if root is None:
            return err(503, "PROACTIVE_STORAGE_UNAVAILABLE", {})
        previous = _proactive_settings()
        updated = {**previous, **body}
        # Registration is performed only by an explicit settings change.
        if 'login_check_enabled' in body:
            from runtime.reply.proactive_login import configure_login_start
            try:
                configure_login_start(root, enabled=updated['login_check_enabled'])
            except (OSError, ValueError, RuntimeError):
                return err(503, "PROACTIVE_LOGIN_UNAVAILABLE", {})
        try:
            write_json(root / 'proactive' / 'settings.json', updated)
        except OSError:
            if 'login_check_enabled' in body:
                try:
                    configure_login_start(root, enabled=previous['login_check_enabled'])
                except (OSError, ValueError, RuntimeError):
                    _safe_log('proactive_login_rollback_unavailable')
            return err(503, 'PROACTIVE_STORAGE_UNAVAILABLE', {})
        if updated['enabled'] and updated['login_check_enabled']:
            from runtime.reply.proactive_login import start_login_worker
            try:
                start_login_worker(root)
            except (OSError, ValueError, RuntimeError):
                # The app can still check opportunities while open.
                return err(503, 'PROACTIVE_LOGIN_UNAVAILABLE', _proactive_status())
        _refresh_proactive_context()
        return ok(_proactive_status())
    if p in {"/toy/cover/progress", "/toy/media/progress"}:
        letter = next((item for item in store.letters if item["letter_id"] == query.get("letter_id")), None)
        if letter is None or (p == "/toy/cover/progress" and letter.get("music_provider") != "ace_step_xl_cover"):
            return err(404, "LETTER_NOT_FOUND", {})
        state = {"status": letter.get("media_status", "PENDING"), "error_code": letter.get("media_error_code", "")}
        root = _media_root()
        if root is not None and _re.fullmatch(r"[a-fA-F0-9-]{36}", letter["letter_id"]):
            candidates = [root / (letter["letter_id"] + suffix) / "progress.json" for suffix in
                          ("-cover-stages", "-cover-cover-stages", "-song-cover-stages")]
            for candidate in candidates:
                try:
                    stage = json.loads(candidate.read_text(encoding="utf-8")).get("stage")
                    if stage in {"loading", "transcribing", "loading_model", "generating", "decoding", "completed"}:
                        state["stage"] = stage
                except (OSError, ValueError):
                    pass
        return ok(state)
    if p == "/toy/cover/lyrics":
        if companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {})
        from runtime.media.cover_upload import recognize_lyrics
        root = _local_data_root()
        if root is None:
            return err(503, "COVER_STORAGE_UNAVAILABLE", {})
        try:
            return ok(await asyncio.to_thread(recognize_lyrics, root, body.get('source_id'), dict(_os.environ)))
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            code = str(exc) if str(exc) in {'COVER_SOURCE_REQUIRED', 'COVER_TRANSCRIPTION_BUSY', 'COVER_TRANSCRIPTION_UNAVAILABLE'} else 'COVER_TRANSCRIPTION_FAILED'
            _persist_provider_failure(code, 'stage=transcribe; attempts=1', {**_os.environ, 'OLIVIA_LOCAL_DATA_ROOT': str(root)})
            return err(400, code, {'error_code': code})
    if p in ("/toy/sticker-packs", "/toy/sticker-packs/open"):
        if method == "POST" and companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {"status": "FAILED"})
        root = _local_data_root()
        if root is None:
            return err(503, "STICKER_PACK_FOLDER_UNAVAILABLE", {"status": "FAILED"})
        from runtime.letter_stickers import packs as sticker_packs
        try:
            if p.endswith("/open"):
                await asyncio.to_thread(sticker_packs.open_folder, root)
            return ok({"folder": str(sticker_packs.folder(root)),
                       "packs": await asyncio.to_thread(sticker_packs.status, root)})
        except OSError:
            return err(503, "STICKER_PACK_FOLDER_UNAVAILABLE", {"status": "FAILED"})
    if p == "/toy/local-songs" or p.startswith("/toy/local-songs/"):
        if method == "POST" and companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {"status": "FAILED"})
        root = _local_data_root()
        if root is None:
            return err(503, "LOCAL_SONG_STORAGE_UNAVAILABLE", {"status": "FAILED"})
        library = LocalSongLibrary(root, _os.environ)
        try:
            if p.endswith('/from-letter'):
                letter = next((x for x in store.letters if x.get('letter_id') == body.get('letter_id')), None)
                if not letter or letter.get('media_status') != 'COMPLETED':
                    return err(409, 'LETTER_AUDIO_NOT_READY', {})
                from urllib.parse import urlsplit
                song_url = letter.get('reply_song_url')
                name = Path(urlsplit(str(song_url or letter.get('reply_audio_url', ''))).path).name
                if name != str(letter['letter_id']) + ('-song.wav' if song_url else '.wav'):
                    return err(409, 'LETTER_AUDIO_NOT_READY', {})
                source = (root / 'media' / name).resolve()
                if not source.is_relative_to((root / 'media').resolve()) or not source.is_file():
                    return err(409, 'LETTER_AUDIO_NOT_READY', {})
                report = await asyncio.to_thread(library.import_audio, source, body.get('name', '林离的原创歌曲' if song_url else '林离的翻唱'))
                return ok(report)
            if p.endswith("/import"):
                return ok(await asyncio.to_thread(library.import_path, body.get("path")))
            if p.endswith("/reveal"):
                await asyncio.to_thread(library.reveal, body.get("id"))
                return ok({"opened": True})
            if p.endswith("/rename"):
                await asyncio.to_thread(library.rename, body.get("id"), body.get("name"))
            elif p.endswith("/delete"):
                await asyncio.to_thread(library.delete, body.get("id"))
            songs = await asyncio.to_thread(library.songs)
            return ok({"songs": songs, "credit": "芙桃"})
        except (LocalSongError, OSError) as exc:
            # A folder Windows will not let us read used to escape as a bare 500,
            # which the settings page could only show as "导入失败".
            code = (str(exc) if isinstance(exc, LocalSongError) else
                    "LOCAL_SONG_PERMISSION_DENIED" if isinstance(exc, PermissionError) else "LOCAL_SONG_PATH_UNREADABLE")
            _safe_log("local_song_failed", status="FAILED", error_code=code)
            status = 404 if code == "LOCAL_SONG_NOT_FOUND" else 400
            if code == "LOCAL_SONG_CATALOG_INVALID":
                status = 503
            return err(status, code, {"status": "FAILED", "error_code": code})
    if p == "/toy/companion/memory/retry":
        if companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {
                "status": "FAILED",
                "error_code": "COMPANION_CONFIRMATION_REQUIRED",
                "retryable": False,
            })
        started = _start_conversation_memory_initialization(
            asyncio.get_running_loop()
        )
        status = conversation_memory_adapter.status()
        runtime_status = (
            _start_ready_conversation_memory_runtime()
            if not started and status.status == "available"
            else None
        )
        retried = 0
        if body.get("retry_failed_writes") is True and runtime_status is not None:
            retried = retry_exhausted_conversation_memory()
            runtime_status = conversation_memory_runtime_status()
        public_status = "INITIALIZING" if started or status.reason_code == "MEM0_INITIALIZING" else (
            runtime_status.status.upper() if runtime_status is not None else status.status.upper()
        )
        return ok({
            "status": public_status,
            "retryable": public_status in {"INITIALIZING", "DEGRADED", "UNAVAILABLE"},
            "retried_count": retried,
        })
    if spec["state"] == "not_implemented" and p != "/toy/midi/generate":
        return not_implemented(spec["error_code"] or "ROUTE_NOT_IMPLEMENTED")

    if p.startswith('/toy/letter/maintenance/'):
        from runtime.imports.letter_maintenance import preview, apply_selection, project, source_rows, key, digest
        if companion_confirmed is not True:
            return err(403, 'COMPANION_CONFIRMATION_REQUIRED')
        if _store_state_error_code or _state_root() is None:
            return err(503, 'LETTER_BACKUP_STORAGE_UNAVAILABLE')
        if not _history_memory_admin_gate.acquire(blocking=False):
            return err(409, 'MEMORY_ADMIN_BUSY')
        try:
            rows = copy.deepcopy(_letter_collection('current', strict=True, maintenance=False))
            edits = copy.deepcopy(getattr(store, 'letter_maintenance', {}))
            backup = body.get('backup')
            if p.endswith('/detail'):
                candidates = project(rows, edits, include_hidden=True)
                if backup is not None:
                    candidates += list(source_rows(backup))
                found = next((row for row in candidates if key(row) == body.get('key')), None)
                if found is None:
                    return err(404, 'LETTER_NOT_FOUND')
                return ok({'status': 'READY', 'content': found.get('content') or '', 'reply_text': found.get('reply_text') or ''})
            plan = await asyncio.to_thread(preview, rows, edits, backup)
            if p.endswith('/preview'):
                page = body.get('page', 0)
                if type(page) is not int or not 0 <= page <= 10000:
                    return err(400, 'LETTER_MAINTENANCE_INVALID')
                counts = {}
                for item in plan['items']:
                    counts[item['kind']] = counts.get(item['kind'], 0) + 1
                return ok({k: v for k, v in plan.items() if k not in {'_changes', 'items'}} | {
                    'items': plan['items'][page * 30:(page + 1) * 30], 'total': len(plan['items']),
                    'counts': counts, 'page': page})
            current = _letter_collection('current', strict=True, maintenance=False)
            if body.get('token') != plan['token'] or digest(current) != digest(rows) or getattr(store, 'letter_maintenance', {}) != edits:
                return err(409, 'LETTER_MAINTENANCE_STALE')
            updated = apply_selection(plan, body.get('selected'), edits)
            store.letter_maintenance = updated
            try:
                # Atomic store writer includes all current state; never replace a
                # stale state.json from the standalone utility or drop SQL guards.
                _persist_store_state()
            except StoreStateUnavailable:
                store.letter_maintenance = edits
                raise
            return ok({'status': 'APPLIED', 'changed': len(set(body['selected'])), 'provider_calls': 0})
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return err(400, 'LETTER_MAINTENANCE_INVALID')
        except (OSError, sqlite3.Error, StoreStateUnavailable):
            return err(503, 'LETTER_BACKUP_STORAGE_UNAVAILABLE')
        finally:
            _history_memory_admin_gate.release()

    if p in {"/toy/letter/backup/export", "/toy/letter/backup/import"}:
        if companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED")
        try:
            if _store_state_error_code:
                return err(503, "LETTER_BACKUP_STORAGE_UNAVAILABLE")
            if p.endswith('/import') and body.get('relationship_retry') is True:
                _start_history_relationships(retry=True)
                return ok(_history_relationship_status())
            if p.endswith("/export"):
                rows = _letter_collection("current", strict=True) + personal_chat_letters(getattr(store, 'personal_chats', ()))
                payload = await asyncio.to_thread(export_letter_backup, rows)
                return ok({"status": "READY", "backup": payload})
            if not _history_memory_admin_gate.acquire(blocking=False):
                return err(409, "MEMORY_ADMIN_BUSY")
            async def restore_backup():
                try:
                    from runtime.imports.letter_maintenance import project
                    existing = _letter_collection("current", strict=True, maintenance=False)
                    # Recognize both original backups and exports of repaired
                    # dates, including hidden rows, without importing duplicates.
                    existing = existing + project(existing, getattr(store, 'letter_maintenance', {}), include_hidden=True)
                    existing += personal_chat_letters(getattr(store, 'personal_chats', ()))
                    return await asyncio.to_thread(import_letter_backup, body.get("backup"),
                        adapter=_legacy_import_adapter(), existing=existing)
                finally:
                    _history_memory_admin_gate.release()
            operation = asyncio.create_task(restore_backup())
            _history_import_operations.add(operation)
            def backup_settled(task):
                _history_import_operations.discard(task)
                if not task.cancelled():
                    task.exception()
            operation.add_done_callback(backup_settled)
            result = await asyncio.shield(operation)
            _start_history_relationships()
            return ok(result)
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return err(400, "LETTER_BACKUP_INVALID")
        except (OSError, sqlite3.Error):
            return err(503, "LETTER_BACKUP_STORAGE_UNAVAILABLE")

    if p == "/toy/letter/legacy/local-import":
        global _local_import_task, _local_import_result
        if method == 'GET' and query.get('relationship') == '1':
            return ok(_history_relationship_status())
        if method == "GET" and query.get("progress") == "1":
            return _local_import_snapshot()
        if method == "POST" and not _local_import_worker:
            if companion_confirmed is not True:
                return err(403, "COMPANION_CONFIRMATION_REQUIRED")
            if _local_import_task is not None and not _local_import_task.done():
                return _local_import_snapshot()
            if isinstance(body, dict) and body.get("background") is True:
                _local_import_result = None
                _update_official_import_progress(status="RUNNING", stage="preflight", total=0, processed=0)
                _local_import_task = asyncio.create_task(_run_local_import(originals_only=body.get("originals_only") is True))
                return _local_import_snapshot()
        if method == "POST" and not _history_gate_owned:
            gate = _history_memory_admin_gate
            if not gate.acquire(blocking=False):
                return err(409, "MEMORY_ADMIN_BUSY", {"status": "UNAVAILABLE", "error_code": "MEMORY_ADMIN_BUSY", "retryable": True})
            async def guarded_import():
                try:
                    lifecycle = letters_adapter.memory_prompt_builder.memory_lifecycle
                    pending_clear = getattr(lifecycle, "_has_pending_clear", None)
                    if callable(pending_clear) and pending_clear():
                        return err(409, "MEMORY_ADMIN_CLEAR_PENDING", {"status": "UNAVAILABLE", "error_code": "MEMORY_ADMIN_CLEAR_PENDING", "retryable": True})
                    return await route(method, path, body, query,
                                       defer_reply=defer_reply, companion_confirmed=companion_confirmed,
                                       _local_import_worker=True, _history_gate_owned=True)
                finally:
                    gate.release()
            # Keep ownership with the actual work: cancelling the request must
            # not unlock a still-running to_thread write or strand the lock.
            operation = asyncio.create_task(guarded_import())
            _history_import_operations.add(operation)
            def settled(task):
                _history_import_operations.discard(task)
                if not task.cancelled():
                    task.exception()
            operation.add_done_callback(settled)
            return await asyncio.shield(operation)
        source = _default_offline_letter_pair_source()
        if source is None:
            return err(404, "OFFLINE_LETTER_BACKUP_REQUIRED", {
                "status": "UNAVAILABLE",
                "error_code": "OFFLINE_LETTER_BACKUP_REQUIRED",
                "retryable": True,
                "source": "local_backup",
            })
        adapter = _legacy_import_adapter()
        if not getattr(adapter, "enabled", False):
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
                "source": "local_backup",
            })
        try:
            if method == "GET":
                report = await asyncio.to_thread(
                    plan_offline_letter_pair_recovery_with_adapter,
                    source,
                    adapter=adapter,
                )
                return ok({
                    **asdict(report),
                    "status": "READY",
                    "source": "local_backup",
                })
            if companion_confirmed is not True:
                return err(403, "COMPANION_CONFIRMATION_REQUIRED", {
                    "status": "FAILED",
                    "error_code": "COMPANION_CONFIRMATION_REQUIRED",
                    "retryable": False,
                })
            if body.get("originals_only") is True:
                report = await asyncio.to_thread(apply_offline_letter_pair_recovery_to_adapter, source, adapter=adapter)
                if report.status != "committed" or report.rejected:
                    return err(503, "LETTER_BACKUP_STORAGE_UNAVAILABLE")
                _start_history_relationships()
                return ok({**asdict(report), "status": "APPLIED", "source": "local_backup",
                           "memory_mode": "originals", "provider_calls": 0})
            preflight_error = _official_history_preflight_error()
            if preflight_error is not None:
                return err(503, preflight_error, {
                    "status": "UNAVAILABLE",
                    "error_code": preflight_error,
                    "retryable": True,
                    "source": "local_backup",
                })
            exchanges = await asyncio.to_thread(
                offline_letter_pair_exchanges,
                source,
            )
            migration = await _migrate_historical_history(exchanges)
            if migration.status != "completed":
                error_code = (
                    "PRIVATE_WORLD_HISTORY_UNAVAILABLE"
                    if str(migration.error_code or "").startswith("PRIVATE_WORLD_")
                    else "OFFLINE_HISTORY_MEMORY_WRITE_FAILED"
                )
                return err(503, error_code, {
                    "status": "UNAVAILABLE",
                    "error_code": error_code,
                    "retryable": True,
                    "source": "local_backup",
                    "memory_migration": migration.to_dict(),
                })
            report = await asyncio.to_thread(
                apply_offline_letter_pair_recovery_to_adapter,
                source,
                adapter=adapter,
            )
        except ValueError:
            return err(400, "OFFLINE_LETTER_BACKUP_INVALID", {
                "status": "FAILED",
                "error_code": "OFFLINE_LETTER_BACKUP_INVALID",
                "retryable": True,
                "source": "local_backup",
            })
        except (OSError, sqlite3.Error):
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
                "source": "local_backup",
            })
        if report.status != "committed" or report.rejected:
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "FAILED",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
                "source": "local_backup",
            })
        report = replace(
            report,
            history_audit=migration.private_world_status or "completed",
            provider_calls=(1 if migration.private_world_status == "initialized" else 0),
        )
        return ok({
            **asdict(report),
            "status": "APPLIED",
            "source": "local_backup",
            "memory_migration": migration.to_dict(),
        })

    if p == "/toy/letter/legacy/official-import":
        if method == "GET":
            if query.get("preflight") == "1":
                preflight_error = _official_history_preflight_error()
                if preflight_error is not None:
                    return err(503, preflight_error, {
                        "status": "UNAVAILABLE",
                        "error_code": preflight_error,
                        "retryable": True,
                    })
                return ok({
                    "status": "READY",
                    "llm_required": _official_history_llm_required(),
                })
            return ok(_official_import_progress_snapshot())
        if companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {
                "status": "FAILED",
                "error_code": "COMPANION_CONFIRMATION_REQUIRED",
                "retryable": False,
            })
        _update_official_import_progress(
            status="RUNNING",
            stage="listing",
            total=0,
            processed=0,
            imported=0,
            skipped=0,
            retryable=False,
        )
        preflight_error = _official_history_preflight_error()
        if preflight_error is not None:
            _update_official_import_progress(
                status="FAILED", stage="failed", retryable=True
            )
            return err(503, preflight_error, {
                "status": "UNAVAILABLE",
                "error_code": preflight_error,
                "retryable": True,
            })
        official_import = True
        try:
            body = await asyncio.to_thread(_collect_official_import_with_progress)
            exchanges_from_legacy_payload(body)
        except (OSError, UnicodeError, ValueError):
            _update_official_import_progress(
                status="FAILED", stage="failed", retryable=True
            )
            return err(503, "OFFICIAL_LETTER_IMPORT_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "OFFICIAL_LETTER_IMPORT_UNAVAILABLE",
                "retryable": True,
            })
        try:
            account_conflict = _official_account_conflicts(body)
        except Exception:
            _update_official_import_progress(
                status="FAILED", stage="failed", retryable=True
            )
            return err(503, "OFFICIAL_LETTER_IMPORT_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "OFFICIAL_LETTER_IMPORT_UNAVAILABLE",
                "retryable": True,
            })
        if account_conflict:
            _update_official_import_progress(
                status="FAILED", stage="failed", retryable=False
            )
            return err(409, "OFFICIAL_ACCOUNT_CONFLICT", {
                "status": "FAILED",
                "error_code": "OFFICIAL_ACCOUNT_CONFLICT",
                "retryable": False,
            })
        p = "/toy/letter/legacy/import"

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

    if p == "/toy/capabilities/video/source":
        if set(body) != {"capability", "source"}:
            return err(400, "VIDEO_REPLY_SETTING_PAYLOAD_INVALID", {
                "status": "FAILED",
                "error_code": "VIDEO_REPLY_SETTING_PAYLOAD_INVALID",
                "retryable": False,
            })
        capability = body.get("capability")
        source = body.get("source")
        if video_reply_source_url(capability, source) is None:
            return err(400, "VIDEO_REPLY_SETTING_PAYLOAD_INVALID", {
                "status": "FAILED",
                "error_code": "VIDEO_REPLY_SETTING_PAYLOAD_INVALID",
                "retryable": False,
            })
        opened = await asyncio.to_thread(
            _open_video_capability_source, capability, source
        )
        if not opened:
            return err(503, "VIDEO_REPLY_SETTING_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "VIDEO_REPLY_SETTING_UNAVAILABLE",
                "retryable": True,
            })
        return ok({
            "status": "OPENED",
            "capability": capability,
            "source": source,
        })

    if p == '/toy/image/status':
        identifier = query.get('letter_id')
        row = next((item for item in store.letters if item.get('letter_id') == identifier), None)
        if row is None: return err(404, 'IMAGE_NOT_FOUND', {})
        from original_client_letter_contract import serialize_letter_detail
        detail = serialize_letter_detail(row)
        result = {key:detail[key] for key in ('imageStatus','replyImageUrl','imageResolution','imageRenderMode') if key in detail}
        from runtime.diagnostics.photo import project_photo
        facts = project_photo(row)
        for source, target in (('image_error_code', 'imageErrorCode'), ('image_phase', 'imagePhase'), ('image_cloud_status', 'imageCloudStatus')):
            if source in facts:
                result[target] = facts[source]
        return ok(result)

    if p == '/toy/image/ack':
        if not companion_confirmed: return err(403, 'COMPANION_CONFIRMATION_REQUIRED', {})
        name = body.get('filename') if isinstance(body, dict) else None
        if not isinstance(body, dict) or set(body) != {'filename'} or not isinstance(name, str) or not _re.fullmatch(r'photo-[a-f0-9]{32}\.png', name):
            return err(400, 'IMAGE_ACK_INVALID', {})
        row = next((item for item in store.letters if str(item.get('reply_image_url', '')).endswith('/'+name)
                    and item.get('image_status') == 'COMPLETED' and item.get('letter_status') == 'COMPLETED'), None)
        if row is None: return err(404, 'IMAGE_NOT_FOUND', {})
        from original_client_letter_contract import _published
        if not _published(row, now=None): return err(409, 'IMAGE_NOT_PUBLISHED', {})
        row.update(image_delivery_status='DELIVERED', image_world_status='PENDING')
        _persist_store_state()
        from runtime.image_understanding import commit_image_memory
        import sys
        await commit_image_memory(sys.modules[__name__], row)
        return ok({'status':'DELIVERED'})

    if p == '/toy/world/wardrobe':
        from runtime.remote_generation import RemoteGeneration
        from runtime.cloud_service import CloudError
        try:
            api=RemoteGeneration(_os.environ.get('OLIVIA_GPU_API_URL',''),_os.environ.get('OLIVIA_GPU_API_KEY',''))
            if method=='POST':
                if not companion_confirmed:
                    return err(403,'COMPANION_CONFIRMATION_REQUIRED',{})
                if not isinstance(body,dict) or set(body)!={'request_id','style_id'}:
                    return err(400,'WARDROBE_STYLE_INVALID',{})
                result=await api.request('wardrobe_set',body)
            elif method=='GET':
                result=await api.request('wardrobe_get',{})
            else:
                return err(405,'METHOD_NOT_ALLOWED',{})
            for style in result['wardrobe_styles']:
                for look in style['looks']:
                    look['image_url']=f"http://127.0.0.1:{PORT}/toy/wardrobe/images/{look['look_id']}"
            return ok(result)
        except CloudError as exc:
            return err(exc.status,exc.code,{'error_code':exc.code})

    if p == '/toy/world/gifts':
        from runtime.remote_generation import RemoteGeneration
        from runtime.cloud_service import CloudError
        try:
            api = RemoteGeneration(_os.environ.get('OLIVIA_GPU_API_URL', ''), _os.environ.get('OLIVIA_GPU_API_KEY', ''))
            if method == 'POST':
                if not companion_confirmed:
                    return err(403, 'COMPANION_CONFIRMATION_REQUIRED', {})
                if not isinstance(body, dict) or set(body) != {'item'}:
                    return err(400, 'GIFT_REQUEST_INVALID', {'error_code': 'GIFT_REQUEST_INVALID'})
                result = await api.request('gifts_buy', body)
            elif method == 'GET':
                result = await api.request('gifts_get', {})
            else:
                return err(405, 'METHOD_NOT_ALLOWED', {})
        except CloudError as exc:
            return err(exc.status, exc.code, {'error_code': exc.code})
        if diary_store is not None:
            try:
                diary_store.remember_gifts([c for c in result['cameras'] if c['owned']], datetime.now(timezone.utc))
            except (OSError, ValueError, sqlite3.Error):
                pass
        return ok(result)

    if p.startswith('/toy/improve'):
        from runtime.improve import upload as improve
        root = _state_root()
        if root is None:
            return err(503, 'IMPROVE_UNAVAILABLE', {'error_code': 'IMPROVE_UNAVAILABLE'})
        if p == '/toy/improve' and method == 'GET':
            return ok(improve.public(improve.read_state(root)))
        if method != 'POST':
            return err(405, 'METHOD_NOT_ALLOWED', {})
        if not companion_confirmed:
            return err(403, 'COMPANION_CONFIRMATION_REQUIRED', {})
        try:
            if p == '/toy/improve/settings' and isinstance(body, dict) and set(body) == {'enabled'}:
                return ok(improve.set_enabled(root, body['enabled']))
            if p == '/toy/improve/forget' and body in ({}, None):
                return ok(await improve.forget(root, _improve_post))
        except ValueError:
            return err(400, 'IMPROVE_REQUEST_INVALID', {'error_code': 'IMPROVE_REQUEST_INVALID'})
        except (OSError, RuntimeError):
            return err(503, 'IMPROVE_FORGET_UNAVAILABLE', {'error_code': 'IMPROVE_FORGET_UNAVAILABLE'})
        return err(400, 'IMPROVE_REQUEST_INVALID', {'error_code': 'IMPROVE_REQUEST_INVALID'})

    if p.startswith('/toy/diary'):
        if diary_store is None:
            return err(503, 'DIARY_UNAVAILABLE', {'error_code': 'DIARY_UNAVAILABLE'})
        from runtime.diary.diary import local_day
        try:
            if p == '/toy/diary' and method == 'GET':
                page = diary_store.page(page=int(query.get('page', 1)), limit=int(query.get('limit', 20)))
                return ok({**page, 'enabled': diary_store.enabled(), 'today': local_day(datetime.now(timezone.utc))})
            if p == '/toy/diary/memoir' and method == 'GET':
                from runtime.diary.diary import memoir_months
                months = memoir_months(diary_store, [*store.letters, *store.personal_chats], datetime.now(timezone.utc))
                return ok({'months': months, 'chars': sum(m['chars'] for m in months), **_memoir_state})
            if p == '/toy/diary/entry' and method == 'GET':
                entry = diary_store.entry(str(query.get('day', '')))
                if entry is None:
                    return err(404, 'DIARY_ENTRY_NOT_FOUND', {'error_code': 'DIARY_ENTRY_NOT_FOUND'})
                diary_store.mark_seen(entry['day'])
                return ok(entry)
            if method != 'POST':
                return err(405, 'METHOD_NOT_ALLOWED', {})
            if not companion_confirmed:
                return err(403, 'COMPANION_CONFIRMATION_REQUIRED', {})
            if not isinstance(body, dict):
                return err(400, 'DIARY_REQUEST_INVALID', {'error_code': 'DIARY_REQUEST_INVALID'})
            if p == '/toy/diary/settings' and set(body) == {'enabled'}:
                diary_store.set_enabled(body['enabled'])
                return ok({'enabled': diary_store.enabled()})
            if p == '/toy/diary/memoir/start' and set(body) == {'months'}:
                from runtime.diary.diary import memoir_months
                available = {m['month'] for m in memoir_months(diary_store, [*store.letters, *store.personal_chats],
                                                                datetime.now(timezone.utc))}
                months = body['months']
                if (not isinstance(months, list) or not months or len(months) > 120
                        or any(month not in available for month in months)):
                    return err(400, 'DIARY_MEMOIR_INVALID', {'error_code': 'DIARY_MEMOIR_INVALID'})
                if _memoir_state['running']:
                    return err(409, 'DIARY_MEMOIR_RUNNING', {'error_code': 'DIARY_MEMOIR_RUNNING'})
                _memoir_state['running'] = True  # claimed before the task starts
                task = asyncio.create_task(_write_memoirs(sorted(set(months))))
                media_tasks.add(task)
                task.add_done_callback(media_tasks.discard)
                return ok({'started': len(set(months))})
            if p == '/toy/diary/comment' and set(body) == {'day', 'text'}:
                diary_store.add_comment(str(body['day']), body['text'], datetime.now(timezone.utc))
                return ok(diary_store.entry(str(body['day'])))
            if p == '/toy/diary/delete' and set(body) == {'day'}:
                diary_store.delete(str(body['day']))
                return ok({'deleted': str(body['day'])})
            return err(400, 'DIARY_REQUEST_INVALID', {'error_code': 'DIARY_REQUEST_INVALID'})
        except KeyError:
            return err(404, 'DIARY_ENTRY_NOT_FOUND', {'error_code': 'DIARY_ENTRY_NOT_FOUND'})
        except ValueError as exc:
            code = str(exc) if str(exc).startswith('DIARY_') else 'DIARY_REQUEST_INVALID'
            return err(400, code, {'error_code': code})

    if p == "/toy/settings/reply-routes":
        from runtime.wardrobe import catalog
        from runtime.video_reply_settings import image_model_capability, require_image_model
        async def image_capability():
            from runtime.remote_generation import RemoteGeneration
            from runtime.cloud_service import CloudError
            try:
                api = RemoteGeneration(_os.environ.get('OLIVIA_GPU_API_URL', ''), _os.environ.get('OLIVIA_GPU_API_KEY', ''))
                if not api.url or not api.token:
                    return None
                return image_model_capability(await asyncio.wait_for(api.request('capabilities', {}), 3))
            except (CloudError, TimeoutError):
                return None
        try:
            if method == "POST":
                if 'image' in body:
                    VideoReplySettingsStore._validate_image(body['image'])
                    image = body['image']
                    saved = video_reply_settings_store.image_snapshot()
                    retaining_saved = (image.get('model') == saved.get('model') and image['resolution'] == saved['resolution']
                                       and (image['enabled'] is False or saved['enabled'] is True))
                    if 'model' in image and not retaining_saved:
                        capability = await image_capability()
                        require_image_model(image, {'image': capability} if capability is not None else {})
                if set(body) == {'request_id', 'wardrobe'}:
                    return ok(video_reply_settings_store.mutate_wardrobe(body['request_id'], body['wardrobe']))
                if set(body) == {"request_id", "tier", "image"}:
                    return ok(video_reply_settings_store.mutate_tier(body["request_id"], body["tier"], image=body["image"]))
                if set(body) == {'request_id', 'image'}:
                    return ok(video_reply_settings_store.mutate_image(body['request_id'], body['image']))
                if set(body) == {"request_id", "tier"}:
                    return ok(video_reply_settings_store.mutate_tier(body["request_id"], body["tier"]))
                if set(body) not in ({"request_id", "routes"}, {"request_id", "routes", "videos"}):
                    return err(400, "VIDEO_REPLY_SETTING_PAYLOAD_INVALID", {})
                return ok(video_reply_settings_store.mutate_routes(body["request_id"], body["routes"], body.get("videos")))
            capability = await image_capability()
            return ok({"state": "available", "image": video_reply_settings_store.image_snapshot(),
                       **({'image_capability': capability} if capability is not None else {}),
                       "wardrobe": video_reply_settings_store.wardrobe_snapshot(), "wardrobe_styles": catalog(),
                       "tier": video_reply_settings_store.tier_snapshot(), "tier_configured": video_reply_settings_store.saved_tier() is not None,
                       "routes": video_reply_settings_store.routes_snapshot(), "videos": video_reply_settings_store.videos_snapshot(),
                       "ready": await asyncio.to_thread(_route_readiness)})
        except VideoReplySettingsError as exc:
            return err(exc.status, exc.code, {"error_code": exc.code})

    if p == "/toy/letter/route-preview-diagnostic":
        code = body.get("error_code")
        if not isinstance(code, str) or code not in {"REPLY_ROUTE_CLIENT_TIMEOUT", "REPLY_ROUTE_CLIENT_CONNECTION", "REPLY_ROUTE_CLIENT_RESPONSE_INVALID", "REPLY_ROUTE_CLIENT_UNKNOWN"}:
            return err(400, "INVALID_DIAGNOSTIC", {})
        _safe_log("reply_route_frontend_failed", status="FAILED", error_code=code)
        return ok({})
    if p == "/toy/letter/route-preview":
        from letter_triage import explicitly_requested_route
        if body.get('letter_id') is not None:
            original = next((row for row in store.letters if row.get('letter_id') == body['letter_id']), None)
            if original is None:
                return err(404, 'LETTER_NOT_FOUND', {})
            if original.get('letter_status') != 'FAILED' or original.get('superseded_by') or original.get('origin') == 'proactive':
                return err(409, 'LETTER_RESEND_NOT_ALLOWED', {})
            body = {**original.get('material', {}), 'content': original.get('content', '')}
        content = body.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 10000:
            return err(400, "INVALID_CONTENT", {})
        missing = await asyncio.to_thread(_missing_memory_component)
        if missing:
            return err(503, missing, {'error_code': missing, 'retryable': True})
        from runtime.reply.jev_billing import account_key_missing
        if await asyncio.to_thread(account_key_missing):
            return err(503, 'OLIVIA_KEY_REQUIRED', {
                'status': 'FAILED', 'error_code': 'OLIVIA_KEY_REQUIRED', 'retryable': False,
            })
        # Same gate as the account-key check: only the cloud account build.
        if _os.environ.get('OLIVIA_JEV_BILLING_ENABLED') == '1' and _reply_writer_unavailable():
            return err(503, 'REPLY_SERVICE_NOT_CONNECTED', {
                'status': 'FAILED', 'error_code': 'REPLY_SERVICE_NOT_CONNECTED', 'retryable': False,
            })
        try:
            routes = video_reply_settings_store.routes_snapshot()
        except VideoReplySettingsError as exc:
            return err(exc.status, exc.code, {})
        preview_videos = video_reply_settings_store.videos_snapshot()
        if body.get('original_output') is not None:
            try:
                decision = _original_request_route(body)
            except (ValueError, TypeError) as exc:
                return err(400, str(exc), {'error_code': str(exc)})
        elif body.get('cover_source_id') is not None:
            try:
                decision = _cover_request_route(body)
            except ValueError as exc:
                return err(400, str(exc), {'error_code': str(exc)})
        else:
            decision = await _classify_managed_route(content, routes)
        if routes != video_reply_settings_store.routes_snapshot() or preview_videos != video_reply_settings_store.videos_snapshot():
            return err(409, "REPLY_ROUTE_PREVIEW_EXPIRED", {"error_code": "REPLY_ROUTE_PREVIEW_EXPIRED"})
        if decision.status == "unavailable":
            reason = getattr(decision, 'reason_code', '')
            from runtime.reply.companion_decision import ERROR_CODES
            code = {'router_quota_exhausted': 'LLM_QUOTA_EXHAUSTED', 'router_auth_failed': 'LLM_AUTH_FAILED',
                    'router_rate_limited': 'LLM_RATE_LIMITED', 'router_timeout': 'LLM_TIMEOUT',
                    'router_invalid_result': 'REPLY_ROUTE_INVALID_RESULT',
                    'router_invalid_content': 'REPLY_ROUTE_INVALID_CONTENT'}.get(
                        reason, reason if reason in ERROR_CODES else 'VIDEO_TRIAGE_UNAVAILABLE')
            from runtime.diagnostics.failure_context import project_failure_context
            detail = project_failure_context(getattr(decision, "diagnostic", None) or {})
            if code == 'VIDEO_TRIAGE_UNAVAILABLE':
                kind = detail.get('exception_type')
                if kind in {'ClientConnectorCertificateError', 'ClientConnectorSSLError'}:
                    code = 'LLM_TLS_FAILED'
                elif kind == 'ClientConnectorDNSError':
                    code = 'LLM_DNS_FAILED'
                elif kind in {'ClientConnectorError', 'ServerDisconnectedError', 'ClientPayloadError'}:
                    code = 'LLM_CONNECTION_FAILED'
                elif detail.get('http_status', 0) >= 500:
                    code = 'LLM_SERVICE_UNAVAILABLE'
            if not detail:
                detail = {"failure_stage": "route_validation"}
            _safe_log("reply_route_classification_failed", status="FAILED", error_code=code, **detail)
            return err(503, code, {"error_code": code})
        requested = explicitly_requested_route(decision)
        explicit_video = bool({"explicit_video_reply_request", "explicit_video_output_request"}.intersection(decision.music_contexts))
        video_confirmation = bool(requested and explicit_video and not preview_videos[requested])
        readiness = {}
        selected_video = preview_videos.get(requested, False)
        if video_reply_settings_store.saved_tier() is not None or body.get('cover_source_id') or body.get('original_output'):
            from runtime.video_reply_settings import routed_video
            selected_mode = requested or decision.reply_mode
            selected_video = routed_video(selected_mode, decision.music_contexts, {**preview_videos, **({requested: True} if video_confirmation else {})})
            ready = await asyncio.to_thread(_route_readiness, {**preview_videos, selected_mode: selected_video},
                                          diagnostic=readiness,
                                          **({"cover": True} if body.get('cover_source_id') else {}))
        else:
            ready = await asyncio.to_thread(_route_readiness, {**preview_videos, requested: True}, diagnostic=readiness) if video_confirmation else await asyncio.to_thread(_route_readiness, diagnostic=readiness)
        if requested and not ready.get(requested) and readiness.get('backend') == 'remote':
            readiness.setdefault('error_code', 'GPU_CAPABILITY_UNAVAILABLE')
        token = str(uuid.uuid4())
        now = time.monotonic()
        for key, value in list(_reply_route_previews.items()):
            if now - value[0] > 300: _reply_route_previews.pop(key, None)
        if len(_reply_route_previews) >= 128: _reply_route_previews.pop(next(iter(_reply_route_previews)))
        import hashlib
        _reply_route_previews[token] = (now, hashlib.sha256(content.encode()).hexdigest(), decision,
                                       preview_videos, routes, body.get('cover_source_id'), body.get('cover_output', 'audio'),
                                       body.get('original_output'), body.get('music_options', {}))
        return ok({"token": token, "requested_route": requested, "video_enabled": selected_video,
                   "image_enabled": video_reply_settings_store.image_snapshot().get('enabled', False),
                   "image_requested": decision.reason_code == 'jev_image_request',
                   "requires_cover_audio": bool(body.get("cover_source_id")),
                   "needs_confirmation": bool(requested and not routes[requested]),
                   "needs_video_confirmation": video_confirmation,
                   "ready": ready.get(requested, True), "readiness": readiness, "reply_mode": decision.reply_mode})

    if p == "/toy/settings/video-reply":
        if method == "GET":
            setting = video_reply_settings_store.snapshot()
            if setting.state != "available":
                return ok(setting.to_dict())
            environment = MappingProxyType(dict(_os.environ))
            try:
                readiness = await asyncio.to_thread(
                    video_reply_dependency_status,
                    environment,
                    performance_video_path=_current_music_performance(environment),
                    probe_runtime=False,
                )
            except Exception:
                return ok({
                    "state": "unavailable",
                    "reason_code": "VIDEO_REPLY_SETTING_UNAVAILABLE",
                })
            return ok({
                "state": "available",
                "enabled": bool(setting.enabled),
                "effective_enabled": bool(setting.enabled and readiness["ready"]),
                "ready": readiness["ready"],
                "dependencies": readiness["dependencies"],
            })
        if "enabled" not in body:
            return _missing_field("enabled")
        if "request_id" not in body:
            return _missing_field("request_id")
        request_id = body.get("request_id") if len(body) == 2 else None
        try:
            video_reply_settings_store.validate_mutation(request_id, body["enabled"])
        except VideoReplySettingsError as exc:
            return err(
                exc.status,
                exc.code,
                {
                    "status": "UNAVAILABLE" if exc.status == 503 else "FAILED",
                    "error_code": exc.code,
                    "retryable": contract.error_metadata(exc.code)["retryable"],
                },
            )
        if body.get("enabled") is True:
            environment = MappingProxyType(dict(_os.environ))
            try:
                readiness = await asyncio.to_thread(
                    video_reply_dependency_status,
                    environment,
                    performance_video_path=_current_music_performance(environment),
                    probe_runtime=False,
                )
            except Exception:
                return err(503, "VIDEO_REPLY_SETTING_UNAVAILABLE", {
                    "status": "UNAVAILABLE",
                    "error_code": "VIDEO_REPLY_SETTING_UNAVAILABLE",
                    "retryable": True,
                })
            missing = [
                item["id"]
                for item in readiness["dependencies"]
                if item.get("state") != "ready"
            ]
            if not readiness["ready"]:
                return err(409, "VIDEO_REPLY_DEPENDENCIES_MISSING", {
                    "status": "FAILED",
                    "error_code": "VIDEO_REPLY_DEPENDENCIES_MISSING",
                    "retryable": False,
                    "missing_dependencies": missing,
                })
        try:
            result = video_reply_settings_store.mutate(request_id, body["enabled"])
        except VideoReplySettingsError as exc:
            return err(
                exc.status,
                exc.code,
                {
                    "status": "UNAVAILABLE" if exc.status == 503 else "FAILED",
                    "error_code": exc.code,
                    "retryable": contract.error_metadata(exc.code)["retryable"],
                },
            )
        return ok(result.to_dict())

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
        from original_client_letter_contract import _photo_pending
        unread = sum(1 for letter in letters if not letter.get("is_read") and not _photo_pending(letter))
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
        if scope == "current":
            _mark_superseded_failed_retries()
            superseded = next(
                (
                    item
                    for item in store.letters
                    if item.get("letter_id") == lid and item.get("superseded_by")
                ),
                None,
            )
            if superseded is not None:
                return err(410, "LETTER_SUPERSEDED", {
                    "status": "SUPERSEDED",
                    "error_code": "LETTER_SUPERSEDED",
                    "replacement_letter_id": superseded["superseded_by"],
                })
        letters = _letter_collection(scope)
        l = next((x for x in letters if x["letter_id"] == lid), None)
        if not l:
            return err(404, "LETTER_NOT_FOUND", {
                "status": "FAILED",
                "error_code": "LETTER_NOT_FOUND",
            })
        from original_client_letter_contract import _published
        reply_published = _published(l, now=None)
        if scope == "current" and not l.get("read_only") and reply_published:
            proactive_unread = l.get('origin') == 'proactive' and not l.get('is_read', 0)
            l["is_read"] = 1
            # A repaired date is a display projection, not the mutable store row.
            original = next((row for row in store.letters if row.get('letter_id') == lid), None)
            if original is not None:
                original['is_read'] = 1
            if proactive_unread:
                _persist_store_state()
                _refresh_proactive_context()
        reply_text = l.get("reply_text", "") if reply_published else ""
        error_code, retryable = _public_llm_error(l.get("error_code"))
        media_detail = contract.project_letter_detail_media(
            l.get("media_status", "NOT_REQUESTED"),
            l.get("media_error_code"),
            l.get("media_retryable", False),
        )
        return ok({
            "letter_id": l["letter_id"],
            "origin": l.get("origin", "user"),
            "title": l.get("title", ""),
            "reply_allowed": l.get("origin") != "proactive",
            "letter_status": l.get("letter_status", 4),
            "error_code": error_code if l.get("letter_status") == "FAILED" else None,
            "retryable": retryable if l.get("letter_status") == "FAILED" else False,
            "audit_status": l.get("audit_status", 2),
            "content": l.get("content", ""),
            "material": l.get("material", {}),
            "reply_type": 1 if reply_text else 0,
            "reply_text": reply_text,
            "reply_content": reply_text,
            "reply_video_url": l.get("reply_video_url", "") if reply_published else "",
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
            "media_status": media_detail["status"],
            "media_error_code": media_detail["error_code"],
            "media_retryable": media_detail["retryable"],
            "audio_provider": l.get("audio_provider"),
            "reply_structure": l.get("reply_structure"),
            "song_emotion": l.get("song_emotion"),
            "transition_seconds": l.get("transition_seconds"),
            "is_read": 1 if l.get("is_read") else 0,
            "replied_at": l.get("replied_at"),
            "created_at": l.get("created_at", int(time.time())),
            "scope": "legacy" if l.get("read_only") else scope,
            "read_only": bool(l.get("read_only", scope == "legacy")),
        })
    if p == "/toy/letter/legacy/import":
        records = _legacy_records(body)
        if records is None:
            return err(400, "INVALID_BODY", {
                "status": "FAILED",
                "error_code": "INVALID_BODY",
            })
        migration = None
        if official_import:
            _update_official_import_progress(
                status="RUNNING",
                stage="memory",
                total=len(records),
                processed=0,
                retryable=False,
            )
            existing_source_ids = _existing_legacy_source_record_ids()
            if existing_source_ids is None:
                _update_official_import_progress(
                    status="FAILED", stage="failed", retryable=True
                )
                return err(503, "MEMORY_UNAVAILABLE", {
                    "status": "UNAVAILABLE",
                    "error_code": "MEMORY_UNAVAILABLE",
                    "retryable": True,
                })
            migration = (
                await _migrate_official_history(
                    body,
                    skip_source_record_ids=existing_source_ids,
                )
                if existing_source_ids
                else await _migrate_official_history(body)
            )
            if migration.status != "completed":
                error_code = (
                    "PRIVATE_WORLD_HISTORY_UNAVAILABLE"
                    if str(migration.error_code or "").startswith("PRIVATE_WORLD_")
                    else "OFFICIAL_HISTORY_MEMORY_WRITE_FAILED"
                )
                _update_official_import_progress(
                    status="FAILED", stage="failed", retryable=True
                )
                return err(503, error_code, {
                    "status": "UNAVAILABLE",
                    "error_code": error_code,
                    "retryable": True,
                    "migration": migration.to_dict(),
                })
            records = [
                replace(
                    record,
                    metadata={
                        **dict(record.metadata),
                        OFFICIAL_HISTORY_PUBLISH_STATUS_KEY: (
                            OFFICIAL_HISTORY_PUBLISH_STATUS_COMPLETED
                        ),
                        OFFICIAL_HISTORY_MEMORY_SEMANTICS_KEY: (
                            OFFICIAL_HISTORY_MEMORY_SEMANTICS_VERSION
                        ),
                    },
                )
                for record in records
            ]
            _update_official_import_progress(
                status="RUNNING", stage="importing", retryable=False
            )
        adapter = _legacy_import_adapter()
        if not getattr(adapter, "enabled", False):
            if official_import:
                _update_official_import_progress(
                    status="FAILED", stage="failed", retryable=True
                )
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
            })
        try:
            result = adapter.import_legacy_records(
                records,
                atomic=True,
                promote_duplicate_metadata=official_import,
            )
        except Exception:
            if official_import:
                _update_official_import_progress(
                    status="FAILED", stage="failed", retryable=True
                )
            return err(503, "MEMORY_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "MEMORY_UNAVAILABLE",
                "retryable": True,
            })
        payload = result.to_dict()
        payload.update({"read_only": True, "scope": "legacy"})
        if official_import:
            payload["status"] = "APPLIED"
        if result.rolled_back:
            if official_import:
                _update_official_import_progress(
                    status="FAILED", stage="failed", retryable=True
                )
            return err(400, "INVALID_CONTENT", {
                "status": "FAILED",
                "error_code": "INVALID_CONTENT",
                **payload,
            })
        if official_import and migration is not None:
            payload["memory_migration"] = migration.to_dict()
            _update_official_import_progress(
                status="COMPLETED",
                stage="completed",
                total=len(records),
                processed=len(records),
                imported=result.inserted,
                skipped=result.duplicates,
                retryable=False,
            )
        return ok(payload)
    if p == "/toy/letter/send":
        if _proactive_busy:
            return err(409, "PROACTIVE_LETTER_BUSY", {"error_code": "PROACTIVE_LETTER_BUSY"})
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
        preview_token = material.get("route_preview_token")
        once = material.get("route_allow_once")
        video_once = material.get("route_video_once")
        material = {key: value for key, value in material.items() if key not in {"route_preview_token", "route_allow_once", "route_video_once"}}
        if material.get('original_output') is not None:
            try:
                _original_request_route(material)
            except (ValueError, TypeError) as exc:
                return err(400, str(exc), {'error_code': str(exc)})
        if not isinstance(content, str) or not content.strip():
            return err(400, 'INVALID_CONTENT', {'status': 'FAILED', 'error_code': 'INVALID_CONTENT'})
        duration = material.get("music_duration_seconds", VIDEO_REPLY_MUSIC_DURATION_SECONDS)
        if (
            isinstance(duration, bool)
            or duration != VIDEO_REPLY_MUSIC_DURATION_SECONDS
        ):
            return err(400, "MUSIC_DURATION_INVALID", {"status": "FAILED", "error_code": "MUSIC_DURATION_INVALID", "allowed": [VIDEO_REPLY_MUSIC_DURATION_SECONDS]})
        if len(content) > 10000:
            return err(400, 'CONTENT_TOO_LONG', {
                'status': 'FAILED',
                'error_code': 'CONTENT_TOO_LONG',
                'max_length': 10000,
            })
        missing = await asyncio.to_thread(_missing_memory_component)
        if missing:
            return err(503, missing, {'error_code': missing, 'retryable': True})
        from runtime.reply.jev_billing import account_key_missing
        if await asyncio.to_thread(account_key_missing):
            return err(503, 'OLIVIA_KEY_REQUIRED', {
                'status': 'FAILED', 'error_code': 'OLIVIA_KEY_REQUIRED', 'retryable': False,
            })
        # Same gate as the account-key check: only the cloud account build.
        if _os.environ.get('OLIVIA_JEV_BILLING_ENABLED') == '1' and _reply_writer_unavailable():
            return err(503, 'REPLY_SERVICE_NOT_CONNECTED', {
                'status': 'FAILED', 'error_code': 'REPLY_SERVICE_NOT_CONNECTED', 'retryable': False,
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
        active_letter = _active_undelivered_letter()
        if active_letter is not None:
            return err(409, "LETTER_IN_PROGRESS", {
                "status": "FAILED",
                "error_code": "LETTER_IN_PROGRESS",
                "retryable": True,
                "active_letter_id": active_letter["letter_id"],
            })
        from letter_triage import explicitly_requested_route, restrict_reply_route
        try:
            routes = video_reply_settings_store.routes_snapshot()
            videos = video_reply_settings_store.videos_snapshot()
        except VideoReplySettingsError:
            routes = dict.fromkeys(("voice_reply", "singing_video", "voice_song_video"), False)
            videos = dict.fromkeys(routes, False)
        route_decision = None
        if preview_token is not None:
            import hashlib
            preview = _reply_route_previews.get(preview_token) if isinstance(preview_token, str) else None
            if preview is None or time.monotonic() - preview[0] > 300 or preview[1] != hashlib.sha256(content.encode()).hexdigest():
                return err(409, "REPLY_ROUTE_PREVIEW_EXPIRED", {"error_code": "REPLY_ROUTE_PREVIEW_EXPIRED"})
            if preview[3] != videos or preview[4] != routes:
                return err(409, "REPLY_ROUTE_PREVIEW_EXPIRED", {"error_code": "REPLY_ROUTE_PREVIEW_EXPIRED"})
            if len(preview) > 5 and preview[5] is not None and (preview[5] != material.get('cover_source_id') or preview[6] != material.get('cover_output', 'audio')):
                return err(409, "REPLY_ROUTE_PREVIEW_EXPIRED", {"error_code": "REPLY_ROUTE_PREVIEW_EXPIRED"})
            if len(preview) > 7 and (preview[7] != material.get('original_output') or preview[8] != material.get('music_options', {})):
                return err(409, "REPLY_ROUTE_PREVIEW_EXPIRED", {"error_code": "REPLY_ROUTE_PREVIEW_EXPIRED"})
            route_decision = preview[2]
        elif once is not None or video_once is not None:
            return err(400, "REPLY_ROUTE_OVERRIDE_INVALID", {})
        # Clients that have not upgraded still cannot silently bypass a disabled route.
        if route_decision is None and material.get('original_output'):
            route_decision = _original_request_route(material)
        if route_decision is None and material.get('cover_source_id'):
            try:
                route_decision = _cover_request_route(material)
            except ValueError as exc:
                return err(400, str(exc), {'error_code': str(exc)})
        if route_decision is None and video_reply_settings_store.routes_configured():
            route_decision = await _classify_managed_route(content, routes)
        if route_decision is not None:
            if route_decision.status == "unavailable":
                from runtime.reply.companion_decision import ERROR_CODES
                reason = getattr(route_decision, 'reason_code', '')
                code = reason if reason in ERROR_CODES else 'VIDEO_TRIAGE_UNAVAILABLE'
                return err(503, code, {"error_code": code})
            requested = explicitly_requested_route(route_decision)
            explicit_video = bool({"explicit_video_reply_request", "explicit_video_output_request"}.intersection(route_decision.music_contexts))
            if video_once is not None and (video_once != requested or not explicit_video or preview_token is None):
                return err(400, "REPLY_ROUTE_OVERRIDE_INVALID", {})
            if requested and explicit_video and not videos[requested]:
                if video_once != requested:
                    return err(409, "REPLY_VIDEO_CONFIRM_REQUIRED", {"error_code": "REPLY_VIDEO_CONFIRM_REQUIRED", "requested_route": requested})
                videos[requested] = True
            readiness = {}
            if video_reply_settings_store.saved_tier() is not None or material.get('cover_source_id') or material.get('original_output'):
                from runtime.video_reply_settings import routed_video
                selected_mode = requested or route_decision.reply_mode
                ready = await asyncio.to_thread(_route_readiness,
                    {**videos, selected_mode: routed_video(selected_mode, route_decision.music_contexts, videos)},
                    diagnostic=readiness,
                    **({'cover': True} if material.get('cover_source_id') else {}))
            else:
                ready = await asyncio.to_thread(_route_readiness, videos, diagnostic=readiness) if video_once is not None else await asyncio.to_thread(_route_readiness, diagnostic=readiness)
            if requested and not ready.get(requested):
                if readiness.get('backend') == 'remote':
                    code = readiness.get('error_code', 'GPU_CAPABILITY_UNAVAILABLE')
                    return err(503, code, {'error_code': code})
                return err(409, "VIDEO_REPLY_DEPENDENCIES_MISSING", {"error_code": "VIDEO_REPLY_DEPENDENCIES_MISSING"})
            if once is not None and (once != requested or preview_token is None):
                return err(400, "REPLY_ROUTE_OVERRIDE_INVALID", {})
            if requested and not routes[requested]:
                if once != requested:
                    return err(409, "REPLY_ROUTE_CONFIRM_REQUIRED", {"error_code": "REPLY_ROUTE_CONFIRM_REQUIRED", "requested_route": requested})
                routes[requested] = True
            route_decision = restrict_reply_route(route_decision, routes)
            if (material.get('cover_source_id') or material.get('original_output')) and 'explicit_audio_output_request' in route_decision.music_contexts:
                videos[requested] = False
        # Uploaded songs are covers; ordinary singing replies are original music.
        source_id = material.get("cover_source_id")
        if source_id is not None:
            from runtime.media.cover_upload import source_path
            try:
                root = _local_data_root()
                if root is None:
                    raise ValueError("COVER_SOURCE_REQUIRED")
                source = source_path(root, source_id)
                lyrics = material.get("cover_lyrics", "")
                language = material.get("cover_language", "unknown")
                if not isinstance(lyrics, str) or len(lyrics) > 30000 or not isinstance(language, str) or not _re.fullmatch(r"[a-z]{2,8}", language):
                    raise ValueError("COVER_LYRICS_INVALID")
                from runtime.media.ace_cover import cover_paths
                from runtime.remote_pipeline import enabled as remote_enabled
                cloud_lyrics = remote_enabled(_os.environ)
                asr = None if cloud_lyrics else cover_paths(_os.environ)["asr_model"]
                if not cloud_lyrics and not lyrics.strip() and not (asr and asr.is_file()):
                    raise ValueError("COVER_LYRICS_REQUIRED")
                import wave
                try:
                    with wave.open(str(source), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                except (OSError, wave.Error, EOFError):
                    raise ValueError("COVER_AUDIO_INVALID") from None
            except ValueError as exc:
                return err(400, str(exc), {"error_code": str(exc)})
        if _proactive_busy:
            return err(409, "PROACTIVE_LETTER_BUSY", {"error_code": "PROACTIVE_LETTER_BUSY"})
        lid = str(uuid.uuid4())
        from runtime.remote_pipeline import enabled as remote_enabled
        cloud_original = source_id is None and remote_enabled(_os.environ)
        letter = {
            "letter_id": lid,
            "content": content,
            "material": material,
            "letter_status": "PENDING",
            "audit_status": 2,
            "is_read": 1,
            "created_at": int(time.time()),
            "life_received_at": letters_adapter._now().isoformat(),
            "reply_text": "",
            "reply_mode": ReplyMode.TEXT_LETTER.value,
            "triage": {"status": "pending"},
            "music_duration_seconds": None if cloud_original else duration,
            "music_planning_duration_seconds": None if cloud_original else duration,
            "music_provider": "ace_step_xl_cover" if source_id is not None else "cloud_original" if cloud_original else "ace_step_xl_original",
            "reply_routes": routes,
            "reply_route_videos": videos,
            "reply_capability_tier": video_reply_settings_store.saved_tier(),
            "image_reply_settings": video_reply_settings_store.image_snapshot(),
            "route_preflight": route_decision.to_dict() if route_decision else None,
            # Freeze the setting at the service receive boundary.  Recovery,
            # retry, and media work read this field rather than global state.
            "video_reply_enabled": (
                any(routes.values())
                and _video_reply_dependencies_ready()
            ),
        }
        bath_end = _current_life_rhythm().get('bath_end_at')
        if bath_end is not None and bath_end > time.time():
            letter['reply_resume_at'] = bath_end
            letter['reply_wait_reason'] = 'bathing'
        store.letters.insert(0, letter)
        previous_request_id = (
            store.request_keys.get(idempotency_key)
            if idempotency_key is not None
            else None
        )
        had_request_key = (
            idempotency_key is not None and idempotency_key in store.request_keys
        )
        if idempotency_key is not None:
            store.request_keys[idempotency_key] = lid
        try:
            _persist_store_state()
        except StoreStateUnavailable:
            store.letters.remove(letter)
            if idempotency_key is not None:
                if had_request_key:
                    store.request_keys[idempotency_key] = previous_request_id
                else:
                    store.request_keys.pop(idempotency_key, None)
            raise
        if defer_reply or letter.get('reply_resume_at', 0) > time.time() or not _conversation_memory_ready_for_reply():
            _schedule_reply_job(lid, content, idempotency_key=idempotency_key)
            return _send_result_for_letter(letter)
        completed = await generate_reply(lid, content, idempotency_key=idempotency_key)
        if not completed:
            return _send_result_for_letter(letter)
        return _send_result_for_letter(letter)
    if p == "/toy/letter/resend":
        if 'material' in body and not isinstance(body['material'], dict):
            return _invalid_field_type('material', 'object')
        if _proactive_busy:
            return err(409, "PROACTIVE_LETTER_BUSY", {"error_code": "PROACTIVE_LETTER_BUSY"})
        lid = _request_value(body, query, "letter_id", "letterId")
        if lid is None:
            return _missing_field("letter_id")
        original = next(
            (item for item in store.letters if item.get("letter_id") == lid),
            None,
        )
        if original is None:
            return err(404, "LETTER_NOT_FOUND", {
                "status": "FAILED",
                "error_code": "LETTER_NOT_FOUND",
                "retryable": False,
            })
        if original.get('origin') == 'proactive':
            return err(409, 'LETTER_RESEND_NOT_ALLOWED', {})
        if original.get("superseded_by"):
            return err(410, "LETTER_SUPERSEDED", {
                "status": "SUPERSEDED",
                "error_code": "LETTER_SUPERSEDED",
                "replacement_letter_id": original["superseded_by"],
            })
        if original.get("letter_status") != "FAILED":
            return err(409, "LETTER_RESEND_NOT_ALLOWED", {
                "status": "FAILED",
                "error_code": "LETTER_RESEND_NOT_ALLOWED",
                "retryable": False,
            })
        if original.get("error_code") == "MEMORY_UNAVAILABLE":
            try:
                # A failed initializer has no outbox to retry. The explicit
                # resend must restart it; an already-running initializer is
                # single-flight and is left alone by the adapter.
                _start_conversation_memory_initialization(asyncio.get_running_loop())
            except Exception:
                return err(503, "MEMORY_UNAVAILABLE", {
                    "status": "FAILED", "error_code": "MEMORY_UNAVAILABLE", "retryable": True,
                    "letter_id": lid,
                })
        retried = await route(
            "POST",
            "/toy/letter/send",
            {
                "content": original.get("content", ""),
                "material": {**original.get("material", {}), **{
                    key: value for key, value in (body.get('material') or {}).items()
                    if key in {'route_preview_token', 'route_allow_once', 'route_video_once'}
                }},
            },
            {"scope": "current"},
            defer_reply=defer_reply,
            companion_confirmed=companion_confirmed,
        )
        replacement_id = retried.get("data", {}).get("letter_id")
        if isinstance(replacement_id, str) and replacement_id != lid:
            original["superseded_by"] = replacement_id
            _persist_store_state()
        return retried
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

def _record_media_job_failure(exc: Exception, stage: str, environment: Mapping[str, str]) -> None:
    """Keep structural failure evidence without exception messages or user content."""
    details = {"stage": stage if stage in {"prepare", "voice_plan", "render", "publish"} else "prepare"}
    for prefix, error in (("", exc), ("cause_", exc.__cause__)):
        if error is None:
            continue
        name = type(error).__name__
        if _re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
            details[prefix + "exception_type"] = name
        candidate = str(error)
        if len(candidate) <= 80 and _re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", candidate):
            details[prefix + "candidate_code"] = candidate
    _persist_provider_failure("MEDIA_JOB_FAILED", json.dumps(details, sort_keys=True), environment)


def _sync_media_delivery_world(letter: dict) -> None:
    from runtime.reply.media_delivery import delivery_references
    events = delivery_references(letter)
    if not events:
        return
    if daily_life_runtime is None:
        letter['media_world_status'] = 'DISABLED'
        return
    try:
        for event in events:
            daily_life_runtime.store.record_media_delivery(event)
        letter['media_world_status'] = 'COMMITTED'
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Media remains playable. The persisted letter is the retry source.
        letter['media_world_status'] = 'PENDING'


def _record_published_media(letter: dict, *, reply_text: str, delivery_id: str,
                            path: Path, components: tuple[str, ...], presentation: str) -> None:
    if letter.get('origin') == 'proactive' and letter.get('letter_status') != 'COMPLETED':
        return
    from runtime.reply.media_delivery import make_delivery, delivery_references
    if (not delivery_id or letter.get('private_world_delivery_id') != delivery_id
            or letter.get('reply_text') != reply_text or not path.is_file() or path.stat().st_size == 0):
        return
    events = delivery_references(letter)
    for component in components:
        if not any(event['component'] == component for event in events):
            try:
                event = make_delivery(letter, component=component, presentation=presentation,
                                      occurred_at=datetime.now(timezone.utc))
            except (ValueError, TypeError, KeyError):
                letter['media_world_status'] = 'UNAVAILABLE'
                return  # Invalid legacy metadata must not break playable media.
            events.append(event)
    letter['media_deliveries'] = events
    letter['media_world_status'] = 'PENDING'
    # Persist the playable letter and its receipt before publishing world facts.
    # A crash after this point can replay the same idempotent events on startup.
    _persist_media_state()
    _sync_media_delivery_world(letter)


async def _render_media_job(letter_id: str, content: str, reply_text: str, reply_mode: str) -> None:
    """Render one media reply at a time and persist a relative artifact path."""
    from runtime.cloud_service import CloudError

    letter = next((item for item in store.letters if item["letter_id"] == letter_id), None)
    if letter is None:
        return
    delivery_id = letter.get('private_world_delivery_id', '')
    if letter.get('reply_text') is not None and letter['reply_text'] != reply_text:
        return
    binding = (delivery_id, letter.get('reply_revision'), letter.get('private_world_reply_sha256'), letter.get('reply_text'))
    def still_current():
        return binding == (letter.get('private_world_delivery_id', ''), letter.get('reply_revision'),
                           letter.get('private_world_reply_sha256'), letter.get('reply_text'))
    from runtime.remote_pipeline import enabled as remote_enabled
    is_video = letter.get("reply_video_enabled", reply_mode != "voice_reply") is True
    # Local renderers still share one physical GPU. Only cloud video dispatch
    # and polling may overlap the audio lane.
    lane = video_media_semaphore if is_video and remote_enabled(_os.environ) else media_semaphore
    async with lane:
        if not still_current():
            return
        letter["media_status"] = "PROCESSING"
        _persist_media_state()
        music_environment = dict(_os.environ)
        if letter.get('material', {}).get('original_output'):
            from runtime.media.music_options import validate
            music_environment['OLIVIA_ORIGINAL_MUSIC_OPTIONS'] = json.dumps(validate(letter['material'].get('music_options', {})))
        environment = MappingProxyType(music_environment)
        data_root = _local_data_root(environment)
        output_dir = data_root / "media" if data_root is not None else None
        if output_dir is None:
            letter["media_status"] = "UNAVAILABLE"
            letter["media_error_code"] = "MEDIA_PROVIDER_UNAVAILABLE"
            letter["media_retryable"] = True
            _persist_media_state()
            return
        output_path = output_dir / f"{letter_id}.mp4"
        video_enabled = letter.get("reply_video_enabled", reply_mode != "voice_reply") is True
        letter["reply_video_enabled"] = video_enabled
        if video_enabled and reply_mode == "voice_reply":
            reply_mode = ReplyMode.SPOKEN_VIDEO.value
        elif video_enabled and reply_mode == "voice_song_video":
            reply_mode = ReplyMode.MUSICAL_VIDEO.value
        if not video_enabled:
            output_path = output_dir / f"{letter_id}.wav"
        stage = "prepare"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            from runtime.remote_pipeline import enabled as remote_enabled
            if video_enabled and not remote_enabled(environment):
                require_breeze_hardware()
            def runtime_path(name: str) -> Path:
                if remote_enabled(environment):
                    return Path()
                configured = configured_media_path(environment, name)
                if configured is None and environment.get(name, "").strip():
                    raise ReplyMediaError("MEDIA_PROVIDER_UNAVAILABLE")
                return configured if configured is not None else Path()

            tts_config = runtime_path("OLIVIA_TTS_CONFIG")
            if reply_mode in {"voice_reply", "voice_song_video"}:
                audio_path = output_dir / (f"{letter_id}-speech.wav" if reply_mode == "voice_song_video" else f"{letter_id}.wav")
                if not (letter.get("reply_audio_url") and audio_path.is_file()):
                    stage = "voice_plan"
                    voice_plan = await _voice_plan_for_letter(letter, reply_text)
                    stage = "speech"
                    metadata = await asyncio.to_thread(render_reply_audio, reply_text, audio_path,
                        tts_config_path=tts_config, voice_performance_plan=voice_plan, environment=environment)
                    if not still_current():
                        return
                    letter["reply_audio_url"] = f"http://127.0.0.1:{PORT}/toy/media/{audio_path.name}"
                    letter["reply_audio_duration"] = metadata["duration_seconds"]
                    _persist_media_state()
                if reply_mode == "voice_reply":
                    letter.update(media_status="COMPLETED", media_error_code=None, media_retryable=False)
                    _record_published_media(letter, reply_text=reply_text, delivery_id=delivery_id,
                        path=audio_path, components=('speech',), presentation='audio')
                    _persist_media_state()
                    return
            if reply_mode == ReplyMode.SPOKEN_VIDEO.value:

                stage = "voice_plan"
                voice_plan = await _voice_plan_for_letter(letter, reply_text)
                stage = "prepare"
                spoken_action_base = runtime_path("OLIVIA_ORDINARY_ACTION_BASE")
                stage = "render"
                await asyncio.to_thread(
                    render_reply_video,
                    reply_text,
                    output_path,
                    tts_config_path=tts_config,
                    visual_config_path=Path(),
                    worker_path=Path(),
                    scene_path=spoken_action_base,
                    latentsync_python_path=runtime_path("OLIVIA_LATENTSYNC_PYTHON"),
                    latentsync_root=runtime_path("OLIVIA_LATENTSYNC_ROOT"),
                    adaptive_delivery=True,
                    voice_performance_plan=voice_plan,
                    environment=environment,
                )
            elif reply_mode in {ReplyMode.MUSICAL_VIDEO.value, "singing_video", "voice_song_video"}:
                stage = "voice_plan"
                voice_plan = await _music_voice_plan_for_letter(letter, reply_text) if reply_mode == ReplyMode.MUSICAL_VIDEO.value else None
                stage = "prepare"
                music_duration_seconds = int(letter.get("music_planning_duration_seconds") or letter.get("music_duration_seconds") or VIDEO_REPLY_MUSIC_DURATION_SECONDS)
                performance_scene = _current_music_performance(environment)
                if video_enabled and not remote_enabled(environment) and (performance_scene is None or not performance_scene.is_file()):
                    raise MusicReplyError("MUSIC_PERFORMANCE_SCENE_NOT_CONFIGURED")
                spoken_action_base = configured_media_path(
                    environment, "OLIVIA_ORDINARY_ACTION_BASE"
                )
                if (
                    not remote_enabled(environment) and reply_mode == ReplyMode.MUSICAL_VIDEO.value and (spoken_action_base is None
                    or not spoken_action_base.is_file())
                ):
                    raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_UNAVAILABLE")
                official_reply_reference = configured_media_path(
                    environment, "OLIVIA_OFFICIAL_REPLY_REFERENCE"
                )
                if (
                    not remote_enabled(environment) and reply_mode == ReplyMode.MUSICAL_VIDEO.value and (official_reply_reference is None
                    or not official_reply_reference.is_file())
                ):
                    raise MusicReplyError("MUSIC_REPLY_TRANSITION_UNAVAILABLE")
                stage = "render"
                song_output = output_dir / f"{letter_id}-song.wav" if not video_enabled and reply_mode == "voice_song_video" else output_path
                music_renderer = render_musical_reply
                cover_options = {}
                if letter.get("music_provider") in {"ace_step_xl_original", "cloud_original"}:
                    from runtime.media.original_song import render_original_reply
                    music_renderer = render_original_reply
                if letter.get("music_provider") == "ace_step_xl_cover":
                    from runtime.media.cover_reply import render_cover_reply
                    from runtime.media.cover_upload import source_path
                    material = letter.get("material", {})
                    try:
                        cover_source = source_path(_local_data_root(environment), material.get("cover_source_id"))
                    except (ValueError, TypeError):
                        raise MusicReplyError("COVER_SOURCE_REQUIRED") from None
                    music_renderer = render_cover_reply
                    cover_options = {"source_audio": cover_source, "cover_lyrics": material.get("cover_lyrics", ""),
                                     "cover_language": material.get("cover_language", "unknown"),
                                     "cover_options": material.get("cover_options", {})}
                from runtime.reply.character_emotion_context import checked_expression_context
                song_expression_context = checked_expression_context(letter) or {
                    'as_of': '1970-01-01T00:00:00+00:00', 'world': None, 'emotion': None}
                render_metadata = await asyncio.to_thread(music_renderer,
                    content,
                    reply_text,
                    song_output,
                    normal_video_path=output_dir / f"{letter_id}-official-spoken-v1.mp4",
                    song_video_path=output_dir / (
                        f"{letter_id}-song-v2-{music_duration_seconds}s.mp4"
                    ),
                    official_reply_reference_path=official_reply_reference or Path(),
                    tts_config_path=tts_config,
                    visual_config_path=runtime_path("OLIVIA_VISUAL_CONFIG"),
                    worker_path=runtime_path("OLIVIA_LIVETALKING_WORKER"),
                    performance_video_path=performance_scene or Path(),
                    duration_seconds=music_duration_seconds,
                    spoken_action_base_path=spoken_action_base,
                    voice_performance_plan=voice_plan,
                    gateway=letters_adapter.gateway,
                    expression_context=song_expression_context,
                    environment=environment,
                    include_spoken=reply_mode == ReplyMode.MUSICAL_VIDEO.value,
                    **({'reply_adapter': letters_adapter} if letter.get('music_provider') != 'ace_step_xl_cover' else {}),
                    **cover_options,
                    **({"render_video": False} if not video_enabled else {}),
                )
                if not still_current():
                    return
                letter.update(_sanitized_music_render_metadata(render_metadata))
                if render_metadata.get('music_planning_duration_seconds') in (110, 240):
                    letter['music_planning_duration_seconds'] = render_metadata['music_planning_duration_seconds']
                measured_duration = render_metadata.get('duration_seconds')
                if type(measured_duration) in (int, float) and 0 < measured_duration < 86400:
                    letter['music_duration_seconds'] = measured_duration
                if reply_mode == "voice_song_video":
                    letter["reply_song_url"] = f"http://127.0.0.1:{PORT}/toy/media/{song_output.name}"
                    letter["reply_song_duration"] = letter.get('music_duration_seconds')
                    letter["reply_structure"] = "speech_and_separate_song_audio"
                    output_path = song_output
            stage = "publish"
            if not still_current():
                return
            if video_enabled:
                letter["reply_video_url"] = f"http://127.0.0.1:{PORT}/toy/media/{output_path.name}"
                # The text can already have been read. Notify once when the
                # corresponding video becomes available, without a new letter.
                if not letter.get('video_completed_at'):
                    letter['video_completed_at'] = time.time()
                    letter['is_read'] = 0
            elif reply_mode != "voice_song_video":
                letter["reply_audio_url"] = f"http://127.0.0.1:{PORT}/toy/media/{output_path.name}"
            letter["media_status"] = "COMPLETED"
            letter["media_error_code"] = None
            letter["media_retryable"] = False
            components = ('speech',) if reply_mode == ReplyMode.SPOKEN_VIDEO.value else (
                (('speech',) if reply_mode in {ReplyMode.MUSICAL_VIDEO.value, 'voice_song_video'} else ())
                + (('cover',) if letter.get('music_provider') == 'ace_step_xl_cover' else ('music',)))
            _record_published_media(letter, reply_text=reply_text, delivery_id=delivery_id,
                path=output_path, components=components, presentation='video' if video_enabled else 'audio')
            _persist_media_state()
        except (
            CloudError,
            ReplyMediaError,
            MusicReplyError,
            VoiceDirectionError,
            GatewayError,
            asyncio.TimeoutError,
            ValueError,
            OSError,
        ) as exc:
            if not still_current():
                return
            _record_media_job_failure(exc, stage, environment)
            candidate = str(exc)[:80]
            error_contract = contract.letter_detail_media_error_metadata(candidate)
            error_code = candidate if error_contract is not None else "MEDIA_PROVIDER_UNAVAILABLE"
            error_contract = contract.letter_detail_media_error_metadata(error_code)
            letter["media_status"] = (
                str(error_contract["status"])
                if error_contract is not None
                else "UNAVAILABLE"
            )
            letter["media_error_code"] = error_code
            letter["media_retryable"] = bool(
                error_contract and error_contract["retryable"]
            )
            if reply_mode == 'voice_song_video' and letter.get('reply_audio_url'):
                _record_published_media(letter, reply_text=reply_text, delivery_id=delivery_id,
                    path=output_dir / f'{letter_id}-speech.wav', components=('speech',), presentation='audio')
            _persist_media_state()

        except Exception as exc:
            # A background worker must never leave a completed text reply stuck processing.
            if still_current():
                _record_media_job_failure(exc, stage, environment)
                letter.update(media_status="UNAVAILABLE", media_error_code="MEDIA_PROVIDER_UNAVAILABLE", media_retryable=True)
                _persist_media_state()


def _schedule_media_job(letter_id: str, content: str, reply_text: str, reply_mode: str) -> None:
    active = media_jobs.get(letter_id)
    if active is not None and not active.done():
        return
    task = asyncio.create_task(
        _render_media_job(
            letter_id,
            content,
            reply_text,
            _exact_reply_mode(reply_mode),
        )
    )
    media_tasks.add(task)
    media_jobs[letter_id] = task

    def discard(completed: asyncio.Task) -> None:
        media_tasks.discard(completed)
        if media_jobs.get(letter_id) is completed:
            media_jobs.pop(letter_id, None)

    task.add_done_callback(discard)


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
    except Exception as exc:
        # The key can be removed after a letter was queued; say so plainly.
        from runtime.diagnostics.failure_context import cause_code
        cause = cause_code(exc)
        code = ("OLIVIA_KEY_REQUIRED" if str(exc) == "JEV_BILLING_ACCOUNT_UNAVAILABLE"
                else "LLM_QUOTA_EXHAUSTED" if cause == "JEV_BALANCE_INSUFFICIENT"
                else "LLM_UNAVAILABLE")
        letter = next(
            (item for item in store.letters if item["letter_id"] == letter_id),
            None,
        )
        if letter is not None and letter.get("letter_status") in {
            "PENDING",
            "PROCESSING",
        }:
            letter["letter_status"] = "FAILED"
            letter["error_code"] = code
            _mark_media_not_requested(letter)
            _persist_store_state()
        from runtime.diagnostics.failure_context import letter_failure_context
        _safe_log("letter_failed", error_code=code, **letter_failure_context(exc))
        return False


def _fail_pending_reply_for_memory_timeout(letter_id: str) -> None:
    letter = next(
        (item for item in store.letters if item["letter_id"] == letter_id),
        None,
    )
    if letter is None or letter.get("letter_status") != "PENDING":
        return
    letter["letter_status"] = "FAILED"
    letter["error_code"] = "MEMORY_UNAVAILABLE"
    _mark_media_not_requested(letter)
    _persist_store_state()
    _safe_log("letter_failed", error_code="MEMORY_UNAVAILABLE")


async def _run_reply_when_memory_ready(
    letter_id: str,
    content: str,
    *,
    idempotency_key: str | None,
    ready_timeout_seconds: float | None = None,
) -> bool:
    letter = next((item for item in store.letters if item['letter_id'] == letter_id), None)
    if letter is None:
        return False
    while letter.get('reply_resume_at', 0) > time.time():
        await asyncio.sleep(min(60, letter['reply_resume_at'] - time.time()))
        if letter.get('letter_status') not in {'PENDING', 'PROCESSING'}:
            return False
    timeout_seconds = (
        MEMORY_READY_REPLY_TIMEOUT_SECONDS
        if ready_timeout_seconds is None
        else max(0.0, float(ready_timeout_seconds))
    )
    if ready_timeout_seconds is None:
        letter = next(
            (item for item in store.letters if item["letter_id"] == letter_id),
            None,
        )
        created_at = None if letter is None else max(letter.get("created_at", 0), letter.get('reply_resume_at', 0))
        if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
            elapsed = max(0.0, time.time() - float(created_at))
            timeout_seconds = max(0.0, timeout_seconds - elapsed)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not _conversation_memory_ready_for_reply():
        if asyncio.get_running_loop().time() >= deadline:
            runtime = conversation_memory_reply_readiness_status()
            # A timed-out wait must not discard a letter while the existing
            # memory call is still being settled. Only the outbox owns retries.
            busy = (
                runtime.enabled and runtime.worker_running
                and (runtime.delivery_pending
                     or (runtime.pending_count > 0 and runtime.reason_code is None))
                and runtime.status == "degraded"
                and runtime.reason_code in {None, "MEMORY_OUTBOX_RETRY_EXHAUSTED"}
            )
            if not busy:
                _fail_pending_reply_for_memory_timeout(letter_id)
                return False
        await asyncio.sleep(0.25)
    return await _run_reply_job(letter_id, content, idempotency_key=idempotency_key)


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
        _run_reply_when_memory_ready(
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


def _schedule_pending_media_jobs() -> int:
    """Resume only durable completed replies whose media render was interrupted."""

    scheduled = 0
    for letter in tuple(store.letters):
        if letter.get("media_status") not in {"PENDING", "QUEUED"}:
            continue
        if letter.get("letter_status") != "COMPLETED":
            continue
        if not receive_eligibility_from_letter(letter).enabled:
            continue
        letter_id = str(letter.get("letter_id", "")).strip()
        content = letter.get("content")
        reply_text = letter.get("reply_text")
        reply_mode = _exact_reply_mode(letter.get("reply_mode"))
        if (
            not letter_id
            or not isinstance(content, str)
            or not content.strip()
            or not isinstance(reply_text, str)
            or not reply_text.strip()
            or reply_mode not in {ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value, "voice_reply", "singing_video", "voice_song_video"}
        ):
            continue
        active = media_jobs.get(letter_id)
        if active is not None and not active.done():
            continue
        _schedule_media_job(letter_id, content, reply_text, reply_mode)
        scheduled += 1
    return scheduled


async def _recover_photo_memories():
    import sys
    from runtime.image_understanding import commit_image_memory
    while True:
        for row in tuple(store.letters):
            if row.get('image_world_status') == 'PENDING' and row.get('image_delivery_status') == 'DELIVERED':
                try:
                    await commit_image_memory(sys.modules[__name__], row)
                except Exception:
                    _safe_log('image_memory_pending')
        await asyncio.sleep(60)


_DAILY_LIFE_REFRESH_CHECK_SECONDS = 60


async def _refresh_daily_life_periodically() -> None:
    while True:
        await asyncio.sleep(_DAILY_LIFE_REFRESH_CHECK_SECONDS)
        if daily_life_runtime is not None:
            daily_life_runtime.schedule_refresh(datetime.now(timezone.utc))


async def _start_reply_tasks(_app: web.Application) -> None:
    global _proactive_task
    if _refresh_contact_relationship_projection():
        try:
            _persist_store_state()
        except (OSError, StoreStateUnavailable):
            _safe_log('contact_projection_persist_unavailable')
    _refresh_proactive_context()
    prefs = _proactive_settings()
    if prefs['enabled'] and prefs['login_check_enabled'] and _state_root() is not None:
        from runtime.reply.proactive_login import start_login_worker
        try:
            start_login_worker(_state_root())
        except (OSError, ValueError, RuntimeError):
            _safe_log('proactive_login_start_unavailable')
    _proactive_task = asyncio.create_task(_proactive_loop())
    _schedule_pending_reply_jobs()
    _schedule_pending_media_jobs()
    from runtime.image_reply import schedule as schedule_image
    import sys
    for letter in store.letters:
        if letter.get('letter_status') == 'COMPLETED':
            schedule_image(sys.modules[__name__], letter)
    photo_recovery = asyncio.create_task(_recover_photo_memories())
    media_tasks.add(photo_recovery)
    photo_recovery.add_done_callback(media_tasks.discard)
    vector_task = asyncio.create_task(_vector_index_loop())
    media_tasks.add(vector_task)
    vector_task.add_done_callback(media_tasks.discard)
    improve_task = asyncio.create_task(_improve_loop())
    media_tasks.add(improve_task)
    improve_task.add_done_callback(media_tasks.discard)
    if diary_store is not None:
        diary_task = asyncio.create_task(_diary_loop())
        media_tasks.add(diary_task)
        diary_task.add_done_callback(media_tasks.discard)
    if daily_life_runtime is not None:
        daily_life_runtime.schedule_refresh(datetime.now(timezone.utc))
        refresh_task = asyncio.create_task(_refresh_daily_life_periodically())
        media_tasks.add(refresh_task)
        refresh_task.add_done_callback(media_tasks.discard)
        media_changed = False
        for letter in store.letters:
            if letter.get('media_deliveries'):
                old_media_status = letter.get('media_world_status')
                _sync_media_delivery_world(letter)
                media_changed = media_changed or old_media_status != letter.get('media_world_status')
            # Each start retries pending letters, and each retry is a paid extraction:
            # a letter that keeps failing is let go after three attempts.
            if (letter.get("letter_status") == "COMPLETED" and letter.get("daily_life_status") == "PENDING"
                    and letter.get("daily_life_attempts", 0) < _LETTER_LIFE_ATTEMPTS):
                letter["daily_life_attempts"] = letter.get("daily_life_attempts", 0) + 1
                _schedule_daily_life_exchange(letter)
        if media_changed:
            try:
                _persist_store_state()
            except (OSError, StoreStateUnavailable):
                _safe_log('media_world_status_persist_unavailable')


def _start_ready_conversation_memory_runtime():
    if getattr(conversation_memory_adapter, "closed", False):
        return None
    builder = letters_adapter.memory_prompt_builder
    status = ensure_conversation_memory_runtime(
        memory_adapter,
        conversation_memory_adapter,
        memory_lifecycle=builder.memory_lifecycle,
    )
    builder.conversation_runtime_status = status.to_dict()
    if status.status == "available":
        _schedule_pending_reply_jobs()
    return status


_memory_initialization_recovery: asyncio.Task | None = None


async def _recover_conversation_memory_initialization(*, interval: float = 30.0) -> None:
    """Retry failed startup without requiring settings access or a new letter."""
    delay = interval
    while True:
        await asyncio.sleep(delay)
        if getattr(conversation_memory_adapter, 'closed', False):
            return
        status = conversation_memory_adapter.status()
        if status.status in {'available', 'disabled'}:
            return
        # The adapter is single-flight: never create a second model loader or
        # vector-store owner while an initialization is still in progress.
        if status.reason_code == 'MEM0_INITIALIZING':
            continue
        if not callable(getattr(conversation_memory_adapter, 'start_initialization', None)):
            return
        _start_conversation_memory_initialization(asyncio.get_running_loop())
        delay = min(300.0, delay * 2)


async def _start_conversation_memory(_app: web.Application) -> None:
    global _memory_initialization_recovery
    global _history_relationship_queue
    from runtime.imports.relationship_batches import RelationshipBatches
    try:
        _history_relationship_queue = RelationshipBatches(_state_root() / 'history-relationship.sqlite3')
        _start_history_relationships()
    except (OSError, sqlite3.Error):
        _safe_log('history_relationship_failed', error_code='HISTORY_RELATIONSHIP_STORAGE_UNAVAILABLE')
    started = _start_conversation_memory_initialization(asyncio.get_running_loop())
    if not started and conversation_memory_adapter.status().status == "available":
        _start_ready_conversation_memory_runtime()
    if _memory_initialization_recovery is None or _memory_initialization_recovery.done():
        _memory_initialization_recovery = asyncio.create_task(_recover_conversation_memory_initialization())


def _start_conversation_memory_initialization(loop: asyncio.AbstractEventLoop) -> bool:
    start = getattr(conversation_memory_adapter, "start_initialization", None)
    if callable(start):
        return bool(start(
            on_ready=lambda: loop.call_soon_threadsafe(
                _start_ready_conversation_memory_runtime
            )
        ))
    return False


async def _stop_reply_tasks(_app: web.Application) -> None:
    global _proactive_task, _history_relationship_task, _history_relationship_queue
    if _history_relationship_task is not None:
        _history_relationship_task.cancel()
        await asyncio.gather(_history_relationship_task, return_exceptions=True)
        _history_relationship_task = None
    _history_relationship_queue = None
    if _proactive_task is not None:
        _proactive_task.cancel()
        await asyncio.gather(_proactive_task, return_exceptions=True)
        _proactive_task = None
    _refresh_proactive_context()
    tasks = tuple(reply_tasks | media_tasks | private_world_candidate_tasks | set(daily_life_tasks.values()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    reply_tasks.clear()
    private_world_candidate_tasks.clear()
    daily_life_tasks.clear()
    if daily_life_runtime is not None:
        await daily_life_runtime.close()
    media_tasks.clear()
    reply_jobs.clear()
    media_jobs.clear()


async def _stop_conversation_memory(_app: web.Application) -> None:
    global _memory_initialization_recovery
    if _memory_initialization_recovery is not None:
        _memory_initialization_recovery.cancel()
        await asyncio.gather(_memory_initialization_recovery, return_exceptions=True)
        _memory_initialization_recovery = None
    close = getattr(conversation_memory_adapter, "close", None)
    if callable(close):
        close()
    stop_conversation_memory_runtime()


def install_reply_task_lifecycle(app: web.Application) -> None:
    """Resume durable pending replies and stop owned tasks with the HTTP app."""

    app.on_startup.append(_start_conversation_memory)
    app.on_startup.append(_start_reply_tasks)
    from runtime.personal_chat.backend import install_personal_chat
    import sys
    install_personal_chat(app, sys.modules[__name__])
    app.on_cleanup.append(_stop_conversation_memory)
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
    letter["private_world_occurred_at"] = letters_adapter._now().isoformat()
    letter["private_world_reply_sha256"] = hashlib.sha256(
        canonical_text.encode("utf-8")
    ).hexdigest()
    letter["private_world_semantic_key"] = f"canonical.{semantic_digest}"


def _refresh_contact_relationship_projection() -> bool:
    """Refresh cached letter/IM behavior from one current projection read."""
    if private_world_relationship_committer is None:
        return False
    try:
        from runtime.personal_chat.contact_invitation import observe
        ledger = private_world_relationship_committer.ledger
        snapshot, events = ledger.snapshot(), ledger.events()
        changed = False
        fields = ('contact_qualification', 'contact_projection_revision', 'initiative_tier', 'initiative_caution')
        for row in (*store.letters, *getattr(store, 'personal_chats', ())):
            if not row.get('private_world_delivery_id'):
                continue
            before = tuple(row.get(key) for key in fields)
            observe(row, snapshot, events)
            changed = changed or before != tuple(row.get(key) for key in fields)
        return changed
    except (AttributeError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        _safe_log('contact_projection_refresh_unavailable')
        return False


_LETTER_LIFE_ATTEMPTS = 3


def _schedule_daily_life_exchange(letter: dict) -> None:
    if daily_life_runtime is None:
        return
    if not letter.get("letter_id") or not letter.get("reply_revision"):
        return
    source_id = f"reply:{letter['letter_id']}:{letter['reply_revision']}"
    active = daily_life_tasks.get(source_id)
    if active is not None and not active.done():
        return

    async def deliver():
        try:
            from runtime.personal_chat.contact_invitation import status, validate_choice
            contact = status(store.letters, private_world_port.snapshot())
            invitation_id = contact.get('invitation_id') if letter.get('origin') != 'proactive' else None
            # A delayed extraction cannot interpret an older letter as acceptance.
            invitation = next((r for r in store.letters if r.get('letter_id') == invitation_id), None)
            if invitation and float(letter.get('created_at', 0)) < float(invitation.get('published_at', 0)):
                invitation_id = None
            await daily_life_runtime.consume_exchange(
                source_id, str(letter.get("content", "")), str(letter.get("reply_text", "")),
                occurred_at=datetime.fromisoformat(letter["private_world_occurred_at"]),
                received_at=datetime.fromisoformat(letter.get("life_received_at", letter["private_world_occurred_at"])),
                **({'origin': 'proactive'} if letter.get('origin') == 'proactive' else {}),
                **({'contact_invited': True} if invitation_id else {}),
            )
            origin_kwargs = {'origin': 'proactive'} if letter.get('origin') == 'proactive' else {}
            signal = daily_life_runtime.store.exchange_relationship(source_id, letter["content"], letter["reply_text"], **origin_kwargs)
            if signal is not None:
                if private_world_relationship_committer is None:
                    raise RuntimeError("DAILY_LIFE_RELATIONSHIP_UNAVAILABLE")
                relationship_status = private_world_relationship_committer.commit_exchange(
                    letter["private_world_delivery_id"], letter["content"], letter["reply_text"], signal,
                    occurred_at=datetime.fromisoformat(letter["private_world_occurred_at"]),
                )
                letter["relationship_status"] = relationship_status.value
                if relationship_status.value not in {"COMMITTED", "DUPLICATE"}:
                    raise RuntimeError("DAILY_LIFE_RELATIONSHIP_UNAVAILABLE")
                _refresh_contact_relationship_projection()
            if invitation_id:
                payload = daily_life_runtime.store._exchange_payload(source_id, letter['content'], letter['reply_text'])
                choice = validate_choice(payload.get('contact_choice'), letter['content'])
                if choice:
                    letter['contact_invitation_id'] = invitation_id
                    letter['contact_choice'] = choice
            boundaries = daily_life_runtime.store.exchange_boundaries(source_id, letter["content"], letter["reply_text"], **origin_kwargs)
            if boundaries:
                if private_world_relationship_committer is None:
                    raise RuntimeError("DAILY_LIFE_RELATIONSHIP_UNAVAILABLE")
                boundary_status = private_world_relationship_committer.commit_boundaries(
                    letter["private_world_delivery_id"], letter["reply_text"], boundaries,
                    occurred_at=datetime.fromisoformat(letter["private_world_occurred_at"]),
                )
                if boundary_status.value not in {"COMMITTED", "DUPLICATE"}:
                    raise RuntimeError("DAILY_LIFE_BOUNDARY_UNAVAILABLE")
            letter["daily_life_status"] = "COMMITTED"
            letter.pop("daily_life_error_code", None)
            letter.pop("daily_life_failure_reason", None)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            letter["daily_life_error_code"] = "DAILY_LIFE_EXCHANGE_UNAVAILABLE"
            reason = str(exc)
            from runtime.reply.companion_decision import ERROR_CODES as jev_error_codes
            from runtime.private_world.jev_exchange import EXCHANGE_ERROR_CODES
            # Persist only bounded machine codes, never model text or credentials.
            letter["daily_life_failure_reason"] = (
                reason if reason in jev_error_codes or reason in EXCHANGE_ERROR_CODES or _re.fullmatch(r"DAILY_LIFE_[A-Z_]{1,64}", reason)
                else "DAILY_LIFE_" + type(exc).__name__.upper()
            )
        finally:
            _persist_store_state()

    task = asyncio.create_task(deliver())
    daily_life_tasks[source_id] = task
    task.add_done_callback(lambda _task: daily_life_tasks.pop(source_id, None))


async def _deliver_private_world_candidate(
    letter: dict,
    user_message: str,
    canonical_reply: str,
) -> CandidateDeliveryStatus | None:
    store = private_world_candidate_store
    if store is None:
        return
    try:
        snapshot = private_world_port.snapshot()
        request = PrivateWorldCandidateRequest.create(
            source_letter_id=str(letter["letter_id"]),
            source_reply_revision=int(letter["reply_revision"]),
            user_message=user_message,
            canonical_reply=canonical_reply,
            character_view=snapshot.character_view(),
            occurred_at=datetime.fromisoformat(
                str(letter["private_world_occurred_at"])
            ),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return
    return await deliver_private_world_candidate(
        private_world_candidate_analyzer,
        store,
        request,
        checkpoint=letter,
        persist=_persist_store_state,
    )


def _schedule_private_world_candidate(
    letter: dict,
    user_message: str,
    canonical_reply: str,
) -> None:
    if private_world_candidate_store is None:
        return
    task = asyncio.create_task(
        _deliver_private_world_candidate(letter, user_message, canonical_reply)
    )
    private_world_candidate_tasks.add(task)
    task.add_done_callback(private_world_candidate_tasks.discard)


async def wait_for_private_world_candidate_tasks() -> None:
    tasks = tuple(private_world_candidate_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _commit_private_world_letter(letter: dict) -> bool:
    if letter.get("private_world_status") != "PENDING":
        return False
    if private_world_committer is None:
        letter["private_world_error_code"] = "PRIVATE_WORLD_UNAVAILABLE"
        return False
    try:
        reply_digest = letter.get("private_world_reply_sha256")
        if not isinstance(reply_digest, str):
            reply_digest = hashlib.sha256(
                str(letter["reply_text"]).encode("utf-8")
            ).hexdigest()
            letter["private_world_reply_sha256"] = reply_digest
        delivery = DeliveryEvent(
            delivery_id=str(letter["private_world_delivery_id"]),
            occurred_at=datetime.fromisoformat(str(letter["private_world_occurred_at"])),
            semantic_key=str(letter["private_world_semantic_key"]),
            canonical_reply_sha256=reply_digest,
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


def commit_private_world_relationship_fact(
    command: RelationshipFactCommand,
) -> RelationshipFactStatus:
    """Commit a typed character fact through the dedicated authority path."""

    if private_world_relationship_committer is None:
        return RelationshipFactStatus.UNAVAILABLE
    return private_world_relationship_committer.commit(command)


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


async def _run_reply_pipeline_for_letter(
    letter: dict,
    content: str,
    exact_mode: str,
    *,
    idempotency_key: str | None,
    reply_input_override: str | None = None,
    request_suffix: str = "",
):
    letter_id = str(letter["letter_id"])
    revision = max(0, int(letter.get("reply_revision", 0))) + 1
    source_token = _CURRENT_LETTER_MEMORY_SOURCE.set(
        f"reply:{letter_id}:{revision}"
    )
    receipt_token = _CURRENT_LETTER_RECEIPT.set(
        datetime.fromisoformat(letter["life_received_at"]) if letter.get("life_received_at") else letters_adapter._now()
    )
    from runtime.reply.companion_runtime import TURN_CONTEXT
    input_revision = letter.get('input_revision', 0)
    def turn_is_current():
        return letter.get('input_revision', 0) == input_revision and letter.get('content', content) == content
    async def save_companion_decision(record):
        if not turn_is_current():
            raise RuntimeError('JEV_INPUT_SUPERSEDED')
        letter['companion_decision'] = record
        _persist_store_state()
    decision_token = TURN_CONTEXT.set(dict(received_source_id=f'reply:{letter_id}:user',
        semantic_kinds=(['text', 'image'] if exact_mode == ReplyMode.TEXT_LETTER.value
                        and letter.get('image_reply_settings', {}).get('enabled') else ['text'])
                       # A spoken reply the user asks for is honoured even when the
                       # route classifier chose a text letter (the voice route must be on).
                       + (['audio_speech'] if exact_mode in {ReplyMode.TEXT_LETTER.value, 'voice_reply'}
                          and (letter.get('reply_routes') or {}).get('voice_reply') is True else []),
        input_revision=input_revision, companion_decision=letter.get('companion_decision'),
        save_companion_decision=save_companion_decision, turn_is_current=turn_is_current,
        recovery_namespace=_memory_config.user_id))
    try:
        reply_input = reply_input_override
        if reply_input is None:
            reply_input = (
                build_ordinary_video_llm_content(content)
                if exact_mode in {ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value}
                or (exact_mode in {"voice_reply", "voice_song_video"} and letter.get("reply_video_enabled") is True)
                else content
            )
        request = ReplyRequest(
            content=reply_input,
            received_user_text=content,
            request_id=f"letter-reply:{letter_id}{request_suffix}",
            idempotency_key=(
                f"{idempotency_key}:{letter_id}{request_suffix}"
                if idempotency_key
                else None
            ),
            max_input_chars=LLM_CONFIG.max_input_chars,
            gateway_scope=(
                (GatewayRequestScope.TEXT_LETTER_MAX_REASONING
                 if exact_mode == ReplyMode.TEXT_LETTER.value
                 else GatewayRequestScope.MEDIA_REPLY_LOW_REASONING)
                if exact_mode != ReplyMode.FUTURE_IM.value
                and supports_scoped_reasoning(LLM_CONFIG)
                else None
            ),
        )
        from runtime.image_reply import photo_reply_context
        context = photo_reply_context(
            letters_adapter.build_reply_context(ReplyMode(exact_mode)),
            letter.get('image_reply_settings', {}),
        )
        if exact_mode == ReplyMode.TEXT_LETTER.value:
            from runtime.reply.reply_context import TrustedTime
            if letter.get('generation_context_at'):
                try:
                    context = replace(context, trusted_time=TrustedTime(
                        datetime.fromisoformat(letter['generation_context_at']), source=context.trusted_time.source))
                except (ValueError, TypeError):
                    letter.pop('generation_context_at', None)
            letter.setdefault('generation_context_at', context.trusted_time.instant.isoformat())
        if exact_mode == ReplyMode.TEXT_LETTER.value:
            from original_client_letter_contract import _published
            context = replace(context, sticker_history=tuple(
                row.get('reply_sticker_id')
                for row in store.letters
                if row.get('letter_id') != letter_id
                and row.get('letter_status') == 'COMPLETED'
                and _published(row, now=None)
            ))
        return await asyncio.wait_for(
            reply_pipeline.run(request, context),
            timeout=_reply_pipeline_timeout_seconds(exact_mode),
        )
    finally:
        TURN_CONTEXT.reset(decision_token)
        _CURRENT_LETTER_MEMORY_SOURCE.reset(source_token)
        _CURRENT_LETTER_RECEIPT.reset(receipt_token)


async def generate_reply(letter_id, content, *, idempotency_key=None):
    from runtime.reply.jev_billing import billing_scope
    with billing_scope('letter:' + str(letter_id)):
        return await _generate_reply_billed(letter_id, content, idempotency_key=idempotency_key)


async def _generate_reply_billed(letter_id, content, *, idempotency_key=None):
    """Run one routed current-letter reply to its canonical terminal state."""

    letter = next(
        (item for item in store.letters if item["letter_id"] == letter_id),
        None,
    )
    if letter is None:
        return False

    # A manual retry after enabling the native image consumer must not restore
    # the old text-only capability selection and fail forever. Keep its receipt.
    saved = letter.get('companion_decision') or {}
    saved_plan = saved.get('plan', {})
    old_parts = [p.get('kind') for step in saved_plan.get('proposal', {}).get('steps', [])
                 for p in step.get('parts', [])]
    if (letter.get('error_code') == 'JEV_PLAN_UNSUPPORTED' and old_parts == ['image']
            and saved_plan.get('resolution', {}).get('status') == 'unsupported'
            and letter.get('image_reply_settings', {}).get('enabled') is True):
        letter['superseded_companion_decision'] = saved
        letter.pop('companion_decision', None)
        letter['input_revision'] = int(letter.get('input_revision', 0)) + 1

    # Persist once: retries and slow background extraction cannot move receipt
    # time to the end of generation or create another sleep interruption.
    letter.setdefault("life_received_at", letters_adapter._now().isoformat())

    letter["letter_status"] = "PROCESSING"
    letter.pop('expression_context', None)
    for field in ('quality_status', 'quality_violation_codes', 'reviewer_calls',
                  'rewrite_calls', 'quality_error_code', 'quality_failure_stage'):
        letter.pop(field, None)
    _persist_store_state()
    receive_eligibility = receive_eligibility_from_letter(letter)
    if isinstance(letter.get("route_preflight"), dict):
        decision = TriageResult(**letter["route_preflight"])
    elif receive_eligibility.enabled:
        try:
            from runtime.reply.companion_runtime import configured_port
            if configured_port() is not None:
                decision = await _classify_managed_route(
                    content, letter.get("reply_routes") or video_reply_settings_store.routes_snapshot()
                )
            else:
                decision = await emotion_triage.classify(content)
        except (GatewayError, asyncio.TimeoutError, ValueError, RuntimeError):
            _safe_log("triage_degraded", error_code="VIDEO_TRIAGE_UNAVAILABLE")
            decision = TriageResult(
                "unknown",
                ReplyMode.TEXT_LETTER.value,
                "video_triage_unavailable",
                "unavailable",
                False,
                character_willing=True,
            )
    else:
        decision = TriageResult(
            "unknown",
            ReplyMode.TEXT_LETTER.value,
            "video_reply_disabled",
            "disabled",
            False,
            character_willing=True,
        )
    if isinstance(letter.get("reply_routes"), dict):
        from letter_triage import restrict_reply_route
        decision = restrict_reply_route(decision, letter["reply_routes"])
    exact_mode = _exact_reply_mode(decision.reply_mode)
    letter["triage"] = decision.to_dict()
    letter["reply_mode"] = exact_mode
    if isinstance(letter.get("reply_route_videos"), dict):
        if letter.get("reply_capability_tier") is not None:
            from runtime.video_reply_settings import routed_video
            letter["reply_video_enabled"] = routed_video(exact_mode, decision.music_contexts, letter["reply_route_videos"])
        else:
            letter["reply_video_enabled"] = letter["reply_route_videos"].get(exact_mode, False)
    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
        "voice_reply", "singing_video", "voice_song_video",
    }:
        letter["media_status"] = "PENDING"
        letter.pop("media_error_code", None)
        letter["media_retryable"] = False
    _schedule_text_reply_delay(letter, exact_mode)
    _persist_store_state()

    try:
        result = await _run_reply_pipeline_for_letter(
            letter,
            content,
            exact_mode,
            idempotency_key=idempotency_key,
        )
    except asyncio.CancelledError:
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_INTERRUPTED"
        _mark_media_not_requested(letter)
        _persist_store_state()
        _safe_log("letter_cancelled")
        raise
    except asyncio.TimeoutError:
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_TIMEOUT"
        _mark_media_not_requested(letter)
        _persist_store_state()
        _safe_log("letter_failed", error_code="LLM_TIMEOUT")
        return False
    except (ValueError, RuntimeError) as exc:
        from runtime.diagnostics.failure_context import cause_code, letter_failure_context
        cause = cause_code(exc)
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_QUOTA_EXHAUSTED" if cause == "JEV_BALANCE_INSUFFICIENT" else "LLM_UNAVAILABLE"
        _mark_media_not_requested(letter)
        _persist_store_state()
        _safe_log("letter_failed", error_code=letter["error_code"], **letter_failure_context(exc))
        return False

    if result.quality_status is not None:
        from runtime.diagnostics.support_bundle import project_reply_quality
        quality = project_reply_quality({
            'quality_status': result.quality_status,
            'reviewer_calls': getattr(result, 'reviewer_calls', None),
            'rewrite_calls': getattr(result, 'rewrite_calls', None),
            'quality_error_code': result.error_code,
            'degraded_stages': getattr(result, 'degraded_stages', {}),
            'stage_timing_seconds': getattr(result, 'stage_timing_seconds', {}),
            'stage_cache_hits': getattr(result, 'stage_cache_hits', {}),
            'stage_actual_calls': getattr(result, 'stage_actual_calls', {}),
        })
        letter.update(quality)
        # Keep the existing private state contract; exported metadata is finite.
        letter["quality_status"] = result.quality_status
        letter["quality_violation_codes"] = list(result.violation_codes)
    if result.state is not ReplyState.COMPLETED:
        public_code, _retryable = _public_llm_error(result.error_code)
        letter["letter_status"] = "FAILED"
        letter["error_code"] = public_code
        _mark_media_not_requested(letter)
        _persist_store_state()
        from runtime.diagnostics.failure_context import project_failure_context
        from runtime.diagnostics.support_bundle import project_reply_quality
        _safe_log("letter_failed", error_code=public_code,
                  **project_reply_quality(letter),
                  **project_failure_context({**getattr(result, 'failure_context', {}),
                      'cause_code': getattr(result, 'error_code', None)}))
        return False
    if getattr(result, 'degraded_stages', None):
        letter['image_status'] = 'SKIPPED'
    if getattr(result, 'companion_decision', None) is not None:
        letter['companion_decision'] = result.companion_decision
        letter['companion_timing'] = result.companion_timing
        letter['companion_delivery'] = result.companion_delivery
        from runtime.reply.companion_runtime import media_locked
        # Like QQ, a letter is spoken by default once the user has turned voice
        # replies on, unless JEV chose another medium or the user limited media.
        default_voice = (result.companion_delivery == 'text' and not getattr(result, 'degraded_stages', None)
                         and (letter.get('reply_routes') or {}).get('voice_reply') is True
                         and not media_locked(result.companion_decision.get('plan')))
        if ((result.companion_delivery == 'audio_speech' or default_voice)
                and exact_mode == ReplyMode.TEXT_LETTER.value):
            # JEV found a spoken reply was asked for: send this letter as a voice reply.
            exact_mode = 'voice_reply'
            letter['reply_mode'] = exact_mode
            letter['reply_video_enabled'] = False
        image_requested = (result.companion_delivery == 'image'
                           and letter.get('image_reply_settings', {}).get('enabled') is True)
        from runtime.image_reply import secondary_photo_allowed
        if image_requested:
            letter['image_status'] = 'PENDING'
        elif not secondary_photo_allowed(letter):
            letter['image_status'] = 'SKIPPED'
        else:
            letter.pop('image_status', None)  # The photo planner decides after the reply.
        if result.companion_timing in {'wait_user', 'defer', 'no_reply'}:
            letter['letter_status'] = 'SKIPPED'
            letter.pop('reply_text', None)
            _mark_media_not_requested(letter)
            _persist_store_state()
            return True
    else:
        for field in ('companion_decision', 'companion_timing', 'companion_delivery'):
            letter.pop(field, None)
    # A copied provenance header from an earlier message is metadata, never part of her letter.
    from dataclasses import replace as _replace_result
    from runtime.personal_chat.decision import HISTORY_HEADER
    cleaned = HISTORY_HEADER.sub('', result.text or '').strip()
    if cleaned != (result.text or '').strip():
        if not cleaned:
            letter["letter_status"] = "FAILED"
            letter["error_code"] = "LLM_PROTOCOL_ERROR"
            _mark_media_not_requested(letter)
            _persist_store_state()
            _safe_log("letter_failed", error_code="LLM_PROTOCOL_ERROR")
            return False
        result = _replace_result(result, text=cleaned)
    if (
        exact_mode
        in {
            ReplyMode.SPOKEN_VIDEO.value,
            ReplyMode.MUSICAL_VIDEO.value,
        }
        and not ordinary_video_reply_length_ok(result.text)
    ):
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_REPLY_LENGTH_INVALID"
        _mark_media_not_requested(letter)
        _persist_store_state()
        _safe_log("letter_failed", error_code="LLM_REPLY_LENGTH_INVALID")
        return False

    _prepare_private_world_delivery(letter, result.text)
    if daily_life_runtime is not None:
        letter["daily_life_status"] = "PENDING"
    letter["reply_text"] = result.text
    from runtime.reply.character_emotion_context import store_expression_context
    store_expression_context(letter, getattr(result, 'expression_context', None), result.text)
    letter["reply_sticker_id"] = getattr(result, "sticker_id", None)
    letter["reply_signature"] = getattr(result, "signature", None)
    letter["letter_status"] = "COMPLETED"
    _mark_superseded_failed_retries()
    _persist_store_state()
    private_world_committed = _commit_private_world_letter(letter)
    _persist_store_state()
    if private_world_committed:
        _schedule_private_world_candidate(letter, content, result.text)
    _schedule_daily_life_exchange(letter)

    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
        "voice_reply", "singing_video", "voice_song_video",
    }:
        letter["media_status"] = "PENDING"
        _persist_media_state()
        _schedule_media_job(letter_id, content, result.text, exact_mode)

    letters_adapter.remember_conversation(content, result.text)
    from runtime.image_reply import schedule as schedule_image
    import sys
    schedule_image(sys.modules[__name__], letter)  # It applies the plain-turn and requested-media rules.
    _safe_log("letter_completed", reply_mode=exact_mode)
    return True



if __name__ == "__main__":
    recover_pending_private_world()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    install_reply_task_lifecycle(app)
    _safe_log('server_start', host='127.0.0.1', port=PORT)
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None)
