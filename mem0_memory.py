"""Optional local Mem0 OSS adapter for new-conversation long-term memory.

Archive, Persona evidence, system prompts, Reviewer payloads, and PrivateWorld
remain outside this adapter.  The optional provider is imported lazily and all
provider failures collapse to stable, privacy-safe states.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import re
import threading
from typing import Callable, Mapping, Protocol, Sequence

from conversation_memory_port import (
    ConversationMemoryPort,
    ConversationMemoryRecord,
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)


MEM0_OSS_VERSION = "2.0.18"
_DOMAIN = "conversation_memory"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class Mem0AdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Mem0Backend(Protocol):
    def add(self, messages: object, **kwargs: object) -> object: ...

    def search(self, query: str, **kwargs: object) -> object: ...

    def get_all(self, **kwargs: object) -> object: ...

    def delete(self, memory_id: str) -> object: ...

    def delete_all(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class Mem0Config:
    enabled: bool
    data_root: Path
    user_id: str = "local-user"
    agent_id: str = "linli"
    collection_name: str = "olivia_conversation_memory_v1"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key_env: str = "DEEPSEEK_API_KEY"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dims: int = 512
    embedding_cache: Path | None = None
    context_max_chars: int = 2400
    config_error: str | None = None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        root = Path(self.data_root)
        if str(root) in {"", "."}:
            raise ValueError("an explicit data root is required")
        object.__setattr__(self, "data_root", root)
        for value, field_name in (
            (self.user_id, "user_id"),
            (self.agent_id, "agent_id"),
            (self.collection_name, "collection_name"),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise ValueError(f"{field_name} is invalid")
        if not isinstance(self.llm_base_url, str) or len(self.llm_base_url) > 2048:
            raise ValueError("llm_base_url is invalid")
        if not isinstance(self.llm_model, str) or len(self.llm_model) > 256:
            raise ValueError("llm_model is invalid")
        if not isinstance(self.llm_api_key_env, str) or not re.fullmatch(
            r"^[A-Z][A-Z0-9_]{0,95}$", self.llm_api_key_env
        ):
            raise ValueError("llm_api_key_env is invalid")
        if not isinstance(self.embedding_model, str) or not self.embedding_model.strip():
            raise ValueError("embedding_model is invalid")
        if type(self.embedding_dims) is not int or not 64 <= self.embedding_dims <= 8192:
            raise ValueError("embedding_dims is invalid")
        if self.embedding_cache is not None:
            object.__setattr__(self, "embedding_cache", Path(self.embedding_cache))
        if type(self.context_max_chars) is not int or not 0 <= self.context_max_chars <= 20_000:
            raise ValueError("context_max_chars is invalid")
        if self.config_error is not None and not re.fullmatch(
            r"^[A-Z][A-Z0-9_]{0,95}$", self.config_error
        ):
            raise ValueError("config_error is invalid")

    @property
    def qdrant_path(self) -> Path:
        return self.data_root / "qdrant"

    @property
    def history_path(self) -> Path:
        return self.data_root / "history" / "history.db"

    @property
    def model_cache(self) -> Path:
        return self.embedding_cache or self.data_root.parent / "model-cache"

    def provider_config(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        environment = environ or os.environ
        return {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": self.collection_name,
                    "path": str(self.qdrant_path),
                    "on_disk": True,
                    "embedding_model_dims": self.embedding_dims,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.llm_model,
                    "api_key": environment.get(self.llm_api_key_env, ""),
                    "openai_base_url": self.llm_base_url,
                    "temperature": 0.1,
                },
            },
            "embedder": {
                "provider": "huggingface",
                "config": {
                    "model": self.embedding_model,
                    "embedding_dims": self.embedding_dims,
                    "model_kwargs": {
                        "device": "cpu",
                        "cache_folder": str(self.model_cache),
                        "local_files_only": True,
                    },
                },
            },
            "history_db_path": str(self.history_path),
        }


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_mem0_config(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> Mem0Config:
    environment = environ or os.environ
    root = Path(project_root or Path(__file__).resolve().parent)
    configured_root = environment.get("OLIVIA_MEMORY_ROOT", "").strip()
    data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else root / ".olivia_data" / "memory" / "mem0"
    )
    if not data_root.is_absolute():
        data_root = root / data_root
    cache_value = environment.get("OLIVIA_MEMORY_EMBEDDING_CACHE", "").strip()
    embedding_cache = Path(cache_value).expanduser() if cache_value else None
    if embedding_cache is not None and not embedding_cache.is_absolute():
        embedding_cache = root / embedding_cache

    enabled = _bool(environment.get("OLIVIA_MEMORY_ENABLED"), False)
    llm_base_url = environment.get(
        "OLIVIA_MEMORY_LLM_BASE_URL",
        environment.get("OLIVIA_LLM_BASE_URL", ""),
    ).strip()
    llm_model = environment.get(
        "OLIVIA_MEMORY_LLM_MODEL",
        environment.get("OLIVIA_LLM_MODEL", ""),
    ).strip()
    key_env = environment.get(
        "OLIVIA_MEMORY_LLM_API_KEY_ENV",
        environment.get("OLIVIA_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY"),
    ).strip()
    error: str | None = None
    if enabled and (not llm_base_url or not llm_model):
        error = "MEM0_LLM_CONFIG_INCOMPLETE"

    dims = _integer(environment.get("OLIVIA_MEMORY_EMBEDDING_DIMS"), 512)
    if not 64 <= dims <= 8192:
        dims = 512
        error = "MEM0_EMBEDDING_DIMS_INVALID"
    context_max = _integer(environment.get("OLIVIA_MEMORY_CONTEXT_MAX_CHARS"), 2400)
    if not 0 <= context_max <= 20_000:
        context_max = 2400
        error = "MEM0_CONTEXT_LIMIT_INVALID"

    return Mem0Config(
        enabled=enabled,
        data_root=data_root,
        user_id=environment.get("OLIVIA_MEMORY_USER_ID", "local-user").strip()
        or "local-user",
        agent_id=environment.get("OLIVIA_MEMORY_AGENT_ID", "linli").strip()
        or "linli",
        collection_name=environment.get(
            "OLIVIA_MEMORY_COLLECTION", "olivia_conversation_memory_v1"
        ).strip()
        or "olivia_conversation_memory_v1",
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key_env=key_env or "DEEPSEEK_API_KEY",
        embedding_model=environment.get(
            "OLIVIA_MEMORY_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        ).strip()
        or "BAAI/bge-small-zh-v1.5",
        embedding_dims=dims,
        embedding_cache=embedding_cache,
        context_max_chars=context_max,
        config_error=error,
    )


def _rows(value: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(value, Mapping):
        value = value.get("results", value.get("memories", value.get("data", ())))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _row_id(row: Mapping[str, object]) -> str | None:
    value = row.get("id", row.get("memory_id"))
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def _row_to_record(
    row: Mapping[str, object],
    *,
    user_id: str,
) -> ConversationMemoryRecord | None:
    metadata = row.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    memory_id = _row_id(row)
    text = row.get("memory", row.get("text"))
    source_id = metadata.get("source_id", row.get("source_id"))
    domain = metadata.get("domain", row.get("domain", _DOMAIN))
    row_user = row.get("user_id", user_id)
    if (
        memory_id is None
        or not isinstance(text, str)
        or not text.strip()
        or not isinstance(source_id, str)
        or not _ID_RE.fullmatch(source_id)
        or domain != _DOMAIN
        or row_user != user_id
    ):
        return None
    score_value = row.get("score")
    score = (
        float(score_value)
        if not isinstance(score_value, bool)
        and isinstance(score_value, (int, float))
        and 0 <= float(score_value) <= 1
        else None
    )
    safe_metadata = {
        key: value
        for key, value in metadata.items()
        if key in {"category", "canonical", "manual", "actor"}
        and (value is None or isinstance(value, (bool, int, float, str)))
    }
    try:
        return ConversationMemoryRecord(
            memory_id=memory_id,
            text=text,
            user_id=user_id,
            source_id=source_id,
            score=score,
            occurred_at=_date(metadata.get("occurred_at", row.get("occurred_at"))),
            created_at=_date(row.get("created_at")),
            metadata=safe_metadata,
        )
    except ValueError:
        return None


class Mem0ConversationMemoryAdapter:
    enabled = True

    def __init__(self, backend: Mem0Backend, config: Mem0Config) -> None:
        if not isinstance(config, Mem0Config) or not config.enabled:
            raise ValueError("an enabled Mem0 config is required")
        self.backend = backend
        self.config = config
        self._lock = threading.RLock()
        self._last_error_code: str | None = None

    def _filters(self, user_id: str) -> dict[str, object]:
        if not isinstance(user_id, str) or not _ID_RE.fullmatch(user_id):
            raise Mem0AdapterError("MEM0_USER_ID_INVALID")
        return {
            "user_id": user_id,
            "agent_id": self.config.agent_id,
            "domain": _DOMAIN,
        }

    def _records(
        self,
        value: object,
        *,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationMemoryRecord, ...]:
        records: list[ConversationMemoryRecord] = []
        for row in _rows(value):
            record = _row_to_record(row, user_id=user_id)
            if record is not None:
                records.append(record)
            if len(records) >= limit:
                break
        return tuple(records)

    def list_memories(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> tuple[ConversationMemoryRecord, ...]:
        if not 1 <= limit <= 1000:
            return ()
        try:
            with self._lock:
                value = self.backend.get_all(
                    filters=self._filters(user_id),
                    top_k=limit,
                )
            self._last_error_code = None
            return self._records(value, user_id=user_id, limit=limit)
        except Exception:
            self._last_error_code = "MEM0_LIST_FAILED"
            return ()

    def search_context(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> tuple[ConversationMemoryRecord, ...]:
        if not isinstance(query, str) or not query.strip() or not 1 <= limit <= 100:
            return ()
        try:
            with self._lock:
                value = self.backend.search(
                    query.strip(),
                    filters=self._filters(user_id),
                    top_k=limit,
                )
            self._last_error_code = None
            return self._records(value, user_id=user_id, limit=limit)
        except Exception:
            self._last_error_code = "MEM0_SEARCH_FAILED"
            return ()

    def remember_exchange(
        self,
        *,
        user_message: str,
        assistant_message: str,
        occurred_at: datetime,
        source_id: str,
        user_id: str,
    ) -> MemoryWriteResult:
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            return MemoryWriteResult(
                MemoryWriteStatus.UNAVAILABLE,
                source_id,
                error_code="MEM0_EXCHANGE_INVALID",
            )
        try:
            existing = self.list_memories(user_id=user_id, limit=1000)
            if any(record.source_id == source_id for record in existing):
                return MemoryWriteResult(MemoryWriteStatus.DUPLICATE, source_id)
            metadata = {
                "source_id": source_id,
                "occurred_at": occurred_at.isoformat(),
                "domain": _DOMAIN,
                "canonical": True,
            }
            with self._lock:
                value = self.backend.add(
                    [
                        {"role": "user", "content": str(user_message)},
                        {"role": "assistant", "content": str(assistant_message)},
                    ],
                    user_id=user_id,
                    agent_id=self.config.agent_id,
                    metadata=metadata,
                )
            ids = tuple(
                memory_id
                for row in _rows(value)
                if (memory_id := _row_id(row)) is not None
            )
            self._last_error_code = None
            return MemoryWriteResult(
                MemoryWriteStatus.WRITTEN if ids else MemoryWriteStatus.SKIPPED,
                source_id,
                ids,
            )
        except Exception:
            self._last_error_code = "MEM0_WRITE_FAILED"
            return MemoryWriteResult(
                MemoryWriteStatus.UNAVAILABLE,
                source_id,
                error_code="MEM0_WRITE_FAILED",
            )

    def add_manual_memory(
        self,
        text: str,
        *,
        user_id: str,
        source_id: str,
    ) -> ConversationMemoryRecord:
        metadata = {
            "source_id": source_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "domain": _DOMAIN,
            "manual": True,
            "actor": "local_user",
        }
        try:
            with self._lock:
                value = self.backend.add(
                    str(text),
                    user_id=user_id,
                    agent_id=self.config.agent_id,
                    metadata=metadata,
                    infer=False,
                )
            records = self._records(value, user_id=user_id, limit=1)
            if not records:
                records = tuple(
                    record
                    for record in self.list_memories(user_id=user_id, limit=1000)
                    if record.source_id == source_id
                )[:1]
            if not records:
                raise Mem0AdapterError("MEM0_MANUAL_WRITE_FAILED")
            self._last_error_code = None
            return records[0]
        except Mem0AdapterError:
            raise
        except Exception as exc:
            self._last_error_code = "MEM0_MANUAL_WRITE_FAILED"
            raise Mem0AdapterError("MEM0_MANUAL_WRITE_FAILED") from exc

    def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        try:
            if not any(
                record.memory_id == memory_id
                for record in self.list_memories(user_id=user_id, limit=1000)
            ):
                return False
            with self._lock:
                self.backend.delete(memory_id)
            self._last_error_code = None
            return True
        except Exception:
            self._last_error_code = "MEM0_DELETE_FAILED"
            return False

    def clear_user(self, *, user_id: str) -> int:
        records = self.list_memories(user_id=user_id, limit=1000)
        if not records:
            return 0
        try:
            with self._lock:
                self.backend.delete_all(
                    user_id=user_id,
                    agent_id=self.config.agent_id,
                )
            self._last_error_code = None
            return len(records)
        except Exception:
            self._last_error_code = "MEM0_CLEAR_FAILED"
            return 0

    def export_user(self, *, user_id: str) -> dict[str, object]:
        return {
            "schema_version": "p03.conversation-memory-export.v1",
            "user_id": user_id,
            "provider": "mem0",
            "records": [
                record.to_prompt_dict()
                for record in self.list_memories(user_id=user_id, limit=1000)
            ],
        }

    def status(self) -> ConversationMemoryStatus:
        records = self.list_memories(user_id=self.config.user_id, limit=1000)
        if self._last_error_code:
            return ConversationMemoryStatus(
                "degraded",
                True,
                "mem0",
                "qdrant-local",
                reason_code=self._last_error_code,
            )
        return ConversationMemoryStatus(
            "available",
            True,
            "mem0",
            "qdrant-local",
            memory_count=len(records),
        )


def _default_factory(config: Mapping[str, object]) -> Mem0Backend:
    module = importlib.import_module("mem0")
    memory_type = getattr(module, "Memory", None)
    if memory_type is None or not hasattr(memory_type, "from_config"):
        raise ImportError("Mem0 Memory.from_config is unavailable")
    return memory_type.from_config(dict(config))


def create_mem0_adapter(
    config: Mem0Config | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    memory_factory: Callable[[Mapping[str, object]], Mem0Backend] | None = None,
) -> ConversationMemoryPort:
    active = config or load_mem0_config(environ=environ)
    if active.config_error:
        return UnavailableConversationMemoryPort(active.config_error)
    if not active.enabled:
        return NullConversationMemoryPort()
    try:
        active.qdrant_path.parent.mkdir(parents=True, exist_ok=True)
        active.history_path.parent.mkdir(parents=True, exist_ok=True)
        active.model_cache.mkdir(parents=True, exist_ok=True)
        backend = (memory_factory or _default_factory)(active.provider_config(environ))
        return Mem0ConversationMemoryAdapter(backend, active)
    except (ModuleNotFoundError, ImportError):
        return UnavailableConversationMemoryPort("MEM0_IMPORT_FAILED")
    except (OSError, RuntimeError, TypeError, ValueError):
        return UnavailableConversationMemoryPort("MEM0_INITIALIZATION_FAILED")


__all__ = [
    "MEM0_OSS_VERSION",
    "Mem0AdapterError",
    "Mem0Config",
    "Mem0ConversationMemoryAdapter",
    "create_mem0_adapter",
    "load_mem0_config",
]
