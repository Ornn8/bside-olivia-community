from __future__ import annotations

import os
from pathlib import Path

import pytest

from conversation_memory_port import UnavailableConversationMemoryPort
from mem0_memory import (
    Mem0Config,
    Mem0ConversationMemoryAdapter,
    create_mem0_adapter,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("OLIVIA_MEMORY_RUNTIME_TEST") != "1",
    reason="requires the installed pinned Mem0/FastEmbed runtime",
)


def test_installed_mem0_initializes_offline_with_local_qdrant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    __import__("mem0")
    __import__("fastembed")
    cache = Path(os.environ["OLIVIA_TEST_MODEL_CACHE"]).resolve()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    config = Mem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=cache,
    )
    adapter = create_mem0_adapter(config)
    assert not isinstance(adapter, UnavailableConversationMemoryPort), getattr(
        adapter, "reason_code", None
    )
    assert isinstance(adapter, Mem0ConversationMemoryAdapter)
    assert os.environ["FASTEMBED_CACHE_PATH"] == str(cache)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    status = adapter.status()
    assert status.status == "available"
    assert status.provider == "mem0"
    assert config.qdrant_path.is_dir()
    assert config.data_root != tmp_path / "memory"
    assert adapter.search_context(
        "合成中文检索",
        user_id="local-user",
        limit=3,
    ) == ()
