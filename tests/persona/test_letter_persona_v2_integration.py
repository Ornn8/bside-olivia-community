from datetime import datetime, timezone
from pathlib import Path

from conversation_memory_port import (
    ConversationMemoryStatus,
    NullConversationMemoryPort,
)
from llm_gateway import GatewayConfig
from local_memory import LocalMemoryAdapter
from local_server import LetterAdapter
from memory_port import CONVERSATION_MEMORY, NullMemoryPort
from memory_prompt import MemoryPrompt
from private_world_port import (
    ContinuationAwareness,
    PrivateWorldSnapshot,
)
from persona_loader import load_persona


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class SnapshotPort:
    def __init__(self, snapshot: PrivateWorldSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> PrivateWorldSnapshot:
        return self._snapshot


class FixedMemoryPromptBuilder:
    def build(self, query: str, *, max_chars: int | None = None) -> MemoryPrompt:
        return MemoryPrompt(
            text="<MEMORY_CONTEXT_UNTRUSTED_DATA>legacy synthetic letter</MEMORY_CONTEXT_UNTRUSTED_DATA>",
            status="available",
        )


class ExplicitConversationMemory:
    enabled = True

    def status(self):
        return ConversationMemoryStatus(
            "available",
            True,
            "mem0",
            "qdrant-local",
            memory_count=0,
        )

    def search_context(self, query, *, user_id, limit):
        del query, user_id, limit
        return ()


def _config(**changes: object) -> GatewayConfig:
    values: dict[str, object] = {
        "provider": "mock",
        "model": "synthetic",
        "persona_v2_enabled": True,
        "persona_v2_file": str(ROOT / "linli_character" / "persona_v2.json"),
    }
    values.update(changes)
    return GatewayConfig(**values)  # type: ignore[arg-type]


def test_enabled_v2_uses_persona_assembly_and_separate_user_message() -> None:
    adapter = LetterAdapter(
        _config(),
        memory_port=NullMemoryPort(),
        now=lambda: NOW,
    )

    messages = adapter._messages("synthetic current letter")

    assert tuple(message["role"] for message in messages) == ("system", "user")
    assert messages[1]["content"] == "synthetic current letter"
    assert "<constitution>" in messages[0]["content"]
    assert "<mode_constraints>" in messages[0]["content"]
    assert "synthetic current letter" not in messages[0]["content"]


def test_letter_adapter_uses_explicit_conversation_memory_port() -> None:
    conversation_memory = ExplicitConversationMemory()
    archive_memory = NullMemoryPort()

    adapter = LetterAdapter(
        _config(),
        memory_port=archive_memory,
        conversation_memory=conversation_memory,
        now=lambda: NOW,
    )

    assert adapter.memory_port is archive_memory
    assert adapter.memory_prompt_builder.conversation_memory is conversation_memory


def test_disabled_conversation_port_preserves_sqlite_canonical_write(
    tmp_path: Path,
) -> None:
    with LocalMemoryAdapter(tmp_path / "memory.sqlite3") as archive_memory:
        adapter = LetterAdapter(
            _config(),
            memory_port=archive_memory,
            conversation_memory=NullConversationMemoryPort(),
            now=lambda: NOW,
        )

        adapter.remember_conversation("synthetic user letter", "synthetic canonical reply")

        records = archive_memory.search(
            "synthetic",
            domains=(CONVERSATION_MEMORY,),
        )
        assert sorted(record.text for record in records) == [
            "Assistant completed a reply: synthetic canonical reply",
            "User sent a new letter: synthetic user letter",
        ]


def test_private_world_projection_enters_only_as_bounded_character_input() -> None:
    adapter = LetterAdapter(
        _config(),
        memory_port=NullMemoryPort(),
        private_world_port=SnapshotPort(
            PrivateWorldSnapshot(
                trust=88,
                nickname_permissions=("linli",),
                continuation_awareness=ContinuationAwareness.PENDING,
            )
        ),
        now=lambda: NOW,
    )

    system = adapter._messages("synthetic")[0]["content"]

    assert '"trust":"high"' in system
    assert "88" not in system
    assert "linli" in system
    assert "pending" not in system
    assert "control_only" not in system


def test_legacy_memory_remains_a_whole_untrusted_history_block() -> None:
    adapter = LetterAdapter(_config(), memory_port=NullMemoryPort(), now=lambda: NOW)
    adapter.memory_prompt_builder = FixedMemoryPromptBuilder()

    system = adapter._messages("synthetic")[0]["content"]

    assert "<untrusted_history>" in system
    assert "legacy synthetic letter" in system
    assert "<MEMORY_CONTEXT_UNTRUSTED_DATA>" not in system
    assert r"\u003cMEMORY_CONTEXT_UNTRUSTED_DATA" in system


def test_corrupt_v2_file_falls_back_to_safe_draft(tmp_path: Path) -> None:
    corrupt = tmp_path / "persona_v2.json"
    corrupt.write_text("{broken", encoding="utf-8")
    adapter = LetterAdapter(
        _config(persona_v2_file=str(corrupt)),
        memory_port=NullMemoryPort(),
        now=lambda: NOW,
    )

    system = adapter._messages("synthetic")[0]["content"]

    assert "Persona status is DRAFT" in system
    assert "Do not invent identity or shared history" in system


def test_disabled_v2_preserves_existing_letter_adapter_path() -> None:
    adapter = LetterAdapter(
        _config(persona_v2_enabled=False),
        memory_port=NullMemoryPort(),
        now=lambda: NOW,
    )

    expected = adapter.persona_provider.messages_for(
        "synthetic", max_chars=adapter.config.max_input_chars
    )
    assert adapter._messages("synthetic") == expected


def test_gateway_config_parses_v2_feature_flag_and_file() -> None:
    config = GatewayConfig.from_mapping(
        {"persona_v2_enabled": True, "persona_v2_file": "synthetic/persona.json"}
    )

    assert config.persona_v2_enabled is True
    assert config.persona_v2_file == "synthetic/persona.json"


def test_release_defaults_enable_a_ready_public_persona() -> None:
    config = GatewayConfig()

    assert config.persona_v2_enabled is True
    assert config.persona_v2_file == "linli_character/persona_release_v2.json"
    loaded = load_persona(ROOT / config.persona_v2_file)
    assert loaded.snapshot.status == "READY"
    assert loaded.snapshot.declarations
    assert all(row.allowed_public_release for row in loaded.snapshot.declarations)
