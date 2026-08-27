# Olivia 本地 toy API 兼容层（仅本地运行）
# 运行: python local_server.py   (监听 127.0.0.1:8899)
# 前端 patch: getClientConfig 的 toyApiUrl -> http://127.0.0.1:8899
#
# 本地适配点（按配置接入模型）:
#   letter_adapter.reply(content)  -> 本地 LLM 生成回信
#   music_adapter.generate(midi)   -> 本地音乐模型生成演奏
import asyncio
from contextvars import ContextVar
import json
import os as _os
import re as _re
import random
import time
import uuid
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

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
from letter_triage import LetterEmotionTriage, TriageResult, _current_music_performance
from runtime.media.media_paths import configured_media_path
from music_reply import MusicReplyError, render_musical_reply, select_speaking_scene, speaking_scene_candidates
from runtime.media.music_duration import MUSIC_DURATION_OPTIONS
from runtime.reply.reply_media import ReplyMediaError, render_reply_video
from runtime.reply.reply_delivery import (
    build_ordinary_video_llm_content,
    build_ordinary_video_repair_content,
    ordinary_video_reply_length_ok,
)
from voice_direction import (
    VoiceDirectionError,
    VoicePerformancePlan,
    direct_music_voice_performance,
    direct_voice_performance,
)
from runtime.video_reply_settings import (
    VideoReplySettingsError,
    VideoReplySettingsStore,
    receive_eligibility_from_letter,
)
from conversation_memory_port import ConversationMemoryPort
from conversation_memory_runtime import conversation_memory_runtime_status
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
from private_world_candidate import (
    PrivateWorldCandidateAnalyzer,
    PrivateWorldCandidateRequest,
    PrivateWorldCandidateRuntime,
    create_private_world_candidate_runtime,
    deliver_private_world_candidate,
)
from private_world_candidates import SQLitePrivateWorldCandidateStore
from private_world_reducer import ReducerEventKind
from private_world_service import PrivateWorldCommandService
from runtime.memory.private_world_projection import project_private_world
from runtime.memory.private_world_runtime import (
    PrivateWorldRuntime,
    create_private_world_runtime,
    resolve_private_world_database,
)
from runtime.imports.official_letters import collect_default_official_text_replies
from runtime.imports.historical_memory import (
    HistoricalMigrationResult,
    apply_historical_private_world,
    assess_historical_relationship,
    exchanges_from_legacy_payload,
    migrate_historical_exchanges,
)
from runtime.reply.reply_context import (
    ReplyContext,
    ReplyMode,
    TrustedTime,
    TrustedWorldFact,
    WorldFactKind,
)
from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
from runtime.reply.reply_reviewer import NullReviewer

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


_CURRENT_LETTER_MEMORY_SOURCE: ContextVar[str | None] = ContextVar(
    "current_letter_memory_source",
    default=None,
)


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
            text = await asyncio.to_thread(
                self.adapter.reply,
                content,
                "",
                request_id=request_id,
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
        conversation_memory: ConversationMemoryPort | None = None,
        private_world_port: PrivateWorldPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or LLM_CONFIG
        try:
            self.gateway = create_gateway(self.config)
        except GatewayError:
            self.gateway = UnconfiguredAdapter()
        self.memory_port: MemoryPort = memory_port or NullMemoryPort()
        self.conversation_memory = conversation_memory
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
        self.memory_prompt_builder = (
            MemoryPromptBuilder(self.memory_port)
            if conversation_memory is None
            else MemoryPromptBuilder(
                self.memory_port,
                conversation_memory=conversation_memory,
            )
        )

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
            max_chars=self.config.max_input_chars,
        )

    def _persona_v2_messages(
        self, content: str, context: str = ""
    ) -> tuple[dict[str, str], ...]:
        user_content = content + ("\n\n" + context if context else "")
        loaded = load_persona(self.persona_v2_path)
        reply_context = self.build_reply_context(ReplyMode.TEXT_LETTER)
        memory_context = self._build_memory_prompt(
            content,
            max_chars=min(
                self.config.max_input_chars,
                self._memory_context_limit(),
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
        """Keep Mem0 and Archive prompt budgets independently bounded."""

        candidates = (
            getattr(getattr(self.conversation_memory, "config", None), "context_max_chars", None),
            getattr(self.memory_port, "context_max_chars", 2400),
            2400,
        )
        for value in candidates:
            if isinstance(value, bool):
                continue
            try:
                limit = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= limit <= 10_000:
                return limit
        return 2400

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
    ) -> str:
        try:
            messages = self._messages(content, context)
            return asyncio.run(
                self.gateway.complete(messages, request_id=request_id)
            ).text
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


def _local_data_root(environment: Mapping[str, str] | None = None) -> Path | None:
    configured = configured_media_path(
        _os.environ if environment is None else environment,
        "OLIVIA_LOCAL_DATA_ROOT",
    )
    return configured.resolve(strict=False) if configured is not None else None


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
memory_adapter: MemoryPort = create_memory_adapter(_archive_memory_config)
conversation_memory_adapter: ConversationMemoryPort = (
    create_conversation_memory_adapter(
        _memory_config,
        llm_fallback={
            "base_url": LLM_CONFIG.base_url,
            "model": LLM_CONFIG.model,
            "api_key_env": LLM_CONFIG.api_key_env,
        },
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
private_world_runtime: PrivateWorldRuntime = create_private_world_runtime(
    user_id=_memory_config.user_id,
)
private_world_port: PrivateWorldPort = private_world_runtime.port
private_world_committer: PrivateWorldDeliveryCommitter | None = (
    private_world_runtime.committer
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
)


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


async def _migrate_official_history(
    payload: Mapping[str, object],
) -> HistoricalMigrationResult:
    exchanges = exchanges_from_legacy_payload(payload)
    result = await asyncio.to_thread(
        migrate_historical_exchanges,
        exchanges,
        memory=conversation_memory_adapter,
        user_id=_memory_config.user_id,
    )
    if result.status != "completed" or not exchanges:
        return result
    try:
        memory_status = conversation_memory_adapter.status().status
    except Exception:
        memory_status = "unavailable"
    if memory_status == "disabled":
        return replace(result, private_world_status="skipped_memory_disabled")
    try:
        if private_world_port.snapshot() != PrivateWorldSnapshot():
            return replace(result, private_world_status="already_initialized")
    except Exception:
        return replace(
            result,
            status="partial",
            private_world_status="unavailable",
            error_code="PRIVATE_WORLD_HISTORY_UNAVAILABLE",
        )
    if private_world_command_service is None:
        return replace(
            result,
            status="partial",
            private_world_status="unavailable",
            error_code="PRIVATE_WORLD_HISTORY_UNAVAILABLE",
        )
    try:
        assessment = await assess_historical_relationship(
            exchanges,
            gateway=letters_adapter.gateway,
            persona_policy=letters_adapter.get_persona_policy(),
        )
        private_world_status = await asyncio.to_thread(
            apply_historical_private_world,
            exchanges,
            assessment=assessment,
            command_service=private_world_command_service,
        )
    except Exception:
        return replace(
            result,
            status="partial",
            private_world_status="unavailable",
            error_code="PRIVATE_WORLD_HISTORY_INITIALIZATION_FAILED",
        )
    return replace(result, private_world_status=private_world_status)
# A file-only Mem0 profile owns the same canonical state root on restart; load
# only after the validated conversation adapter has selected that root.
_load_store_state()
emotion_triage = LetterEmotionTriage(letters_adapter.gateway)
media_semaphore = asyncio.Semaphore(1)
media_tasks: set[asyncio.Task] = set()
reply_tasks: set[asyncio.Task] = set()
private_world_candidate_tasks: set[asyncio.Task] = set()
reply_jobs: dict[str, asyncio.Task] = {}
media_jobs: dict[str, asyncio.Task] = {}


def _persist_media_state() -> None:
    _persist_store_state()


async def _voice_plan_for_letter(
    letter: dict,
    reply_text: str,
) -> VoicePerformancePlan:
    """Direct the frozen reply once, then reuse its persisted performance plan."""

    if "voice_performance_plan" in letter:
        stored = letter.get("voice_performance_plan")
        if not isinstance(stored, dict):
            raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_PLAN_INVALID")
        try:
            plan = VoicePerformancePlan.from_dict(stored)
        except VoiceDirectionError:
            raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_PLAN_INVALID") from None
        if plan.reply_text != reply_text:
            raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_PLAN_INVALID")
        return plan

    letter_id = str(letter.get("letter_id", "")).strip()
    if not letter_id:
        raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_REQUEST_INVALID")
    request_id = f"letter-reply:{letter_id}:voice-direction"
    persisted_request_id = letter.get("voice_direction_request_id")
    if persisted_request_id is not None and persisted_request_id != request_id:
        raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_REQUEST_INVALID")
    if persisted_request_id is None:
        # Commit the provider idempotency key before issuing the paid call, so a
        # restart in the provider-success/persistence window uses the same key.
        letter["voice_direction_request_id"] = request_id
        _persist_media_state()
    plan = await asyncio.wait_for(
        direct_voice_performance(
            reply_text,
            letters_adapter.gateway,
            letter_content=str(letter.get("content", "")),
            request_id=request_id,
        ),
        timeout=LLM_TIMEOUT_SECONDS,
    )
    if plan.reply_text != reply_text:
        raise VoiceDirectionError("VOICE_DIRECTION_TEXT_MISMATCH")
    letter["voice_performance_plan"] = plan.to_dict()
    _persist_media_state()
    return plan


async def _music_voice_plan_for_letter(
    letter: dict,
    reply_text: str,
) -> VoicePerformancePlan:
    """Keep the musical prelude on its pre-A director and persistence lane."""

    stored = letter.get("voice_performance_plan")
    if stored is not None:
        try:
            if not isinstance(stored, dict):
                raise VoiceDirectionError("VOICE_DIRECTION_INVALID")
            plan = VoicePerformancePlan.from_music_dict(stored)
        except VoiceDirectionError:
            raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_PLAN_INVALID") from None
        if plan.reply_text != reply_text:
            raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_PLAN_INVALID")
        return plan

    letter_id = str(letter.get("letter_id", "")).strip()
    if not letter_id:
        raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_REQUEST_INVALID")
    request_id = f"letter-reply:{letter_id}:voice-direction"
    persisted_request_id = letter.get("voice_direction_request_id")
    if persisted_request_id is not None and persisted_request_id != request_id:
        raise VoiceDirectionError("VOICE_DIRECTION_PERSISTED_REQUEST_INVALID")
    if persisted_request_id is None:
        letter["voice_direction_request_id"] = request_id
        _persist_media_state()
    plan = await asyncio.wait_for(
        direct_music_voice_performance(
            reply_text,
            letters_adapter.gateway,
            request_id=request_id,
        ),
        timeout=LLM_TIMEOUT_SECONDS,
    )
    if plan.reply_text != reply_text:
        raise VoiceDirectionError("VOICE_DIRECTION_TEXT_MISMATCH")
    letter["voice_performance_plan"] = plan.to_dict()
    _persist_media_state()
    return plan


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
    metadata = l.get("metadata")
    imported_official = isinstance(metadata, dict) and (
        metadata.get("import_kind") == "official_text_reply"
    )
    summary = (
        l.get("content")
        if imported_official
        else l.get("reply_text") or l.get("content")
    ) or ""
    return {
        "letter_id": l["letter_id"],
        "summary": summary[:50],
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
    configured = _local_data_root()
    return configured / "media" if configured is not None else None


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
                defer_reply=(method == "POST" and path.rstrip("/") == "/toy/letter/send"),
                companion_confirmed=(
                    request.headers.get("X-Olivia-Companion-Action") == "confirmed"
                ),
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
    if getattr(memory_adapter, "enabled", False) and hasattr(memory_adapter, "list_legacy"):
        try:
            return list(getattr(memory_adapter, "list_legacy")())
        except Exception:
            if strict:
                raise
            _safe_log("memory_read_skipped", domain="legacy_letters")
    return store.legacy_letters


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


def _letter_collection(scope: str):
    if scope == "legacy":
        return _legacy_letter_collection()
    _mark_superseded_failed_retries()
    return [letter for letter in store.letters if not letter.get("superseded_by")]


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
    if code == "LLM_REPLY_LENGTH_INVALID":
        return "LLM_REPLY_LENGTH_INVALID", False
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


def _reply_pipeline_timeout_seconds() -> float:
    """Cover generation plus review, one rewrite, and the final recheck."""

    default_quality_timeout = min(float(LLM_TIMEOUT_SECONDS), 60.0)
    try:
        quality_timeout = float(
            _os.environ.get(
                "OLIVIA_REPLY_REVIEW_TIMEOUT_SECONDS",
                str(default_quality_timeout),
            )
        )
    except (TypeError, ValueError):
        quality_timeout = default_quality_timeout
    quality_timeout = max(1.0, min(quality_timeout, 300.0))
    return float(LLM_TIMEOUT_SECONDS) + 3.0 * quality_timeout + 5.0


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


async def route(
    method,
    path,
    body,
    query,
    *,
    defer_reply: bool = False,
    companion_confirmed: bool = False,
):
    p = path.rstrip("/")
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
    if p == "/health":
        return _health_result(query.get("profile", contract.HEALTH_PROFILE_CORE))
    if spec["state"] == "not_implemented" and p != "/toy/midi/generate":
        return not_implemented(spec["error_code"] or "ROUTE_NOT_IMPLEMENTED")

    if p == "/toy/letter/legacy/official-import":
        if companion_confirmed is not True:
            return err(403, "COMPANION_CONFIRMATION_REQUIRED", {
                "status": "FAILED",
                "error_code": "COMPANION_CONFIRMATION_REQUIRED",
                "retryable": False,
            })
        official_import = True
        try:
            body = await asyncio.to_thread(collect_default_official_text_replies)
        except (OSError, UnicodeError, ValueError):
            return err(503, "OFFICIAL_LETTER_IMPORT_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "OFFICIAL_LETTER_IMPORT_UNAVAILABLE",
                "retryable": True,
            })
        try:
            account_conflict = _official_account_conflicts(body)
        except Exception:
            return err(503, "OFFICIAL_LETTER_IMPORT_UNAVAILABLE", {
                "status": "UNAVAILABLE",
                "error_code": "OFFICIAL_LETTER_IMPORT_UNAVAILABLE",
                "retryable": True,
            })
        if account_conflict:
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

    if p == "/toy/settings/video-reply":
        if method == "GET":
            return ok(video_reply_settings_store.snapshot().to_dict())
        if "enabled" not in body:
            return _missing_field("enabled")
        if "request_id" not in body:
            return _missing_field("request_id")
        request_id = body.get("request_id") if len(body) == 2 else None
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
        if scope == "current" and not l.get("read_only"):
            l["is_read"] = 1
        reply_published = l.get("reply_not_before", 0.0) <= time.time()
        reply_text = l.get("reply_text", "") if reply_published else ""
        error_code, retryable = _public_llm_error(l.get("error_code"))
        media_detail = contract.project_letter_detail_media(
            l.get("media_status", "NOT_REQUESTED"),
            l.get("media_error_code"),
            l.get("media_retryable", False),
        )
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
            "media_status": media_detail["status"],
            "media_error_code": media_detail["error_code"],
            "media_retryable": media_detail["retryable"],
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
        if official_import:
            payload["status"] = "APPLIED"
        if result.rolled_back:
            return err(400, "INVALID_CONTENT", {
                "status": "FAILED",
                "error_code": "INVALID_CONTENT",
                **payload,
            })
        if official_import:
            migration = await _migrate_official_history(body)
            payload["memory_migration"] = migration.to_dict()
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
        duration = material.get("music_duration_seconds", 60)
        if isinstance(duration, bool) or duration not in MUSIC_DURATION_OPTIONS:
            return err(400, "MUSIC_DURATION_INVALID", {"status": "FAILED", "error_code": "MUSIC_DURATION_INVALID", "allowed": list(MUSIC_DURATION_OPTIONS)})
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
            # Freeze the setting at the service receive boundary.  Recovery,
            # retry, and media work read this field rather than global state.
            "video_reply_enabled": video_reply_settings_store.receive_snapshot().enabled,
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
        environment = MappingProxyType(dict(_os.environ))
        data_root = _local_data_root(environment)
        output_dir = data_root / "media" if data_root is not None else None
        if output_dir is None:
            letter["media_status"] = "UNAVAILABLE"
            letter["media_error_code"] = "MEDIA_PROVIDER_UNAVAILABLE"
            letter["media_retryable"] = True
            _persist_media_state()
            return
        output_path = output_dir / f"{letter_id}.mp4"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            def runtime_path(name: str) -> Path:
                configured = configured_media_path(environment, name)
                if configured is None and environment.get(name, "").strip():
                    raise ReplyMediaError("MEDIA_PROVIDER_UNAVAILABLE")
                return configured if configured is not None else Path()

            tts_config = runtime_path("OLIVIA_TTS_CONFIG")
            visual_config = runtime_path("OLIVIA_VISUAL_CONFIG")
            worker = runtime_path("OLIVIA_LIVETALKING_WORKER")
            if reply_mode == "musical_video":
                voice_plan = await _music_voice_plan_for_letter(letter, reply_text)
                music_duration_seconds = int(letter.get("music_duration_seconds", 60))
                performance_scene = _current_music_performance(environment)
                if performance_scene is None or not performance_scene.is_file():
                    raise MusicReplyError("MUSIC_PERFORMANCE_SCENE_NOT_CONFIGURED")
                await asyncio.to_thread(render_musical_reply,
                    content,
                    reply_text,
                    output_path,
                    normal_video_path=output_dir / f"{letter_id}-official-spoken-v1.mp4",
                    song_video_path=output_dir / (
                        f"{letter_id}-song-v2-{music_duration_seconds}s.mp4"
                    ),
                    official_reply_reference_path=select_speaking_scene(
                        speaking_scene_candidates(environment)
                    ) or Path(),
                    tts_config_path=tts_config,
                    visual_config_path=visual_config,
                    worker_path=worker,
                    performance_video_path=performance_scene,
                    duration_seconds=music_duration_seconds,
                    voice_performance_plan=voice_plan,
                    environment=environment,
                )
            else:
                voice_plan = await _voice_plan_for_letter(letter, reply_text)
                from datetime import datetime

                hour = datetime.now().hour
                scene_key = "morning" if 5 <= hour < 10 else "day" if 10 <= hour < 17 else "dusk" if 17 <= hour < 20 else "night"
                normal_scene = configured_media_path(
                    environment, f"OLIVIA_SCENE_{scene_key.upper()}"
                )
                if normal_scene is None or not normal_scene.is_file():
                    raise ReplyMediaError("ORDINARY_SCENE_NOT_CONFIGURED")
                latentsync_python = runtime_path("OLIVIA_LATENTSYNC_PYTHON")
                latentsync_root = runtime_path("OLIVIA_LATENTSYNC_ROOT")
                await asyncio.to_thread(render_reply_video,
                    reply_text,
                    output_path,
                    tts_config_path=tts_config,
                    visual_config_path=visual_config,
                    worker_path=worker,
                    scene_path=normal_scene,
                    latentsync_python_path=latentsync_python,
                    latentsync_root=latentsync_root,
                    adaptive_delivery=True,
                    voice_performance_plan=voice_plan,
                    enforce_content_gate=True,
                    environment=environment,
                )
            letter["reply_video_url"] = f"http://127.0.0.1:{PORT}/toy/media/{output_path.name}"
            letter["media_status"] = "COMPLETED"
            letter["media_error_code"] = None
            letter["media_retryable"] = False
            _persist_media_state()
        except (
            ReplyMediaError,
            MusicReplyError,
            VoiceDirectionError,
            GatewayError,
            asyncio.TimeoutError,
            ValueError,
            OSError,
        ) as exc:
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
            _persist_media_state()


def _schedule_media_job(letter_id: str, content: str, reply_text: str, reply_mode: str) -> None:
    active = media_jobs.get(letter_id)
    if active is not None and not active.done():
        return
    task = asyncio.create_task(_render_media_job(letter_id, content, reply_text, reply_mode))
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
            or reply_mode not in {ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value}
        ):
            continue
        active = media_jobs.get(letter_id)
        if active is not None and not active.done():
            continue
        _schedule_media_job(letter_id, content, reply_text, reply_mode)
        scheduled += 1
    return scheduled


async def _start_reply_tasks(_app: web.Application) -> None:
    _schedule_pending_reply_jobs()
    _schedule_pending_media_jobs()


async def _stop_reply_tasks(_app: web.Application) -> None:
    tasks = tuple(reply_tasks | media_tasks | private_world_candidate_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    reply_tasks.clear()
    private_world_candidate_tasks.clear()
    media_tasks.clear()
    reply_jobs.clear()
    media_jobs.clear()


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


async def _deliver_private_world_candidate(
    letter: dict,
    user_message: str,
    canonical_reply: str,
) -> None:
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
    await deliver_private_world_candidate(
        private_world_candidate_analyzer,
        store,
        request,
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
    try:
        reply_input = reply_input_override
        if reply_input is None:
            reply_input = (
                build_ordinary_video_llm_content(content)
                if exact_mode
                in {ReplyMode.SPOKEN_VIDEO.value, ReplyMode.MUSICAL_VIDEO.value}
                else content
            )
        request = ReplyRequest(
            content=reply_input,
            request_id=f"letter-reply:{letter_id}{request_suffix}",
            idempotency_key=(
                f"{idempotency_key}:{letter_id}{request_suffix}"
                if idempotency_key
                else None
            ),
            max_input_chars=LLM_CONFIG.max_input_chars,
        )
        return await asyncio.wait_for(
            reply_pipeline.run(
                request,
                letters_adapter.build_reply_context(ReplyMode(exact_mode)),
            ),
            timeout=_reply_pipeline_timeout_seconds(),
        )
    finally:
        _CURRENT_LETTER_MEMORY_SOURCE.reset(source_token)


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
    receive_eligibility = receive_eligibility_from_letter(letter)
    if receive_eligibility.enabled:
        decision = await emotion_triage.classify(content)
    else:
        decision = TriageResult(
            "unknown",
            ReplyMode.TEXT_LETTER.value,
            "video_reply_disabled",
            "disabled",
            False,
            character_willing=True,
        )
    exact_mode = _exact_reply_mode(decision.reply_mode)
    letter["triage"] = decision.to_dict()
    letter["reply_mode"] = exact_mode
    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
    }:
        letter["media_status"] = "UNAVAILABLE"
        letter["media_error_code"] = "MEDIA_PROVIDER_UNAVAILABLE"
        letter["media_retryable"] = True
    _schedule_text_reply_delay(letter, exact_mode)
    _persist_store_state()

    try:
        result = await _run_reply_pipeline_for_letter(
            letter,
            content,
            exact_mode,
            idempotency_key=idempotency_key,
        )
        if (
            result.state is ReplyState.COMPLETED
            and exact_mode == ReplyMode.SPOKEN_VIDEO.value
            and not ordinary_video_reply_length_ok(result.text)
        ):
            result = await _run_reply_pipeline_for_letter(
                letter,
                content,
                exact_mode,
                idempotency_key=idempotency_key,
                reply_input_override=build_ordinary_video_repair_content(result.text),
                request_suffix=":duration-repair",
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
    if (
        exact_mode == ReplyMode.SPOKEN_VIDEO.value
        and not ordinary_video_reply_length_ok(result.text)
    ):
        letter["letter_status"] = "FAILED"
        letter["error_code"] = "LLM_REPLY_LENGTH_INVALID"
        _persist_store_state()
        _safe_log("letter_failed", error_code="LLM_REPLY_LENGTH_INVALID")
        return False

    _prepare_private_world_delivery(letter, result.text)
    letter["reply_text"] = result.text
    letter["letter_status"] = "COMPLETED"
    _mark_superseded_failed_retries()
    _persist_store_state()
    private_world_committed = _commit_private_world_letter(letter)
    _persist_store_state()
    if private_world_committed:
        _schedule_private_world_candidate(letter, content, result.text)

    if exact_mode in {
        ReplyMode.SPOKEN_VIDEO.value,
        ReplyMode.MUSICAL_VIDEO.value,
    }:
        letter["media_status"] = "PENDING"
        _persist_media_state()
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
