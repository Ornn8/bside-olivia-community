from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    (
        ("companion_memory_context", "runtime.memory.companion_memory_context"),
        ("conversation_memory_admin", "runtime.memory.conversation_memory_admin"),
        ("conversation_memory_port", "runtime.memory.conversation_memory_port"),
        ("conversation_memory_runtime", "runtime.memory.conversation_memory_runtime"),
        ("local_memory", "runtime.memory.local_memory"),
        ("mem0_memory", "runtime.memory.mem0_memory"),
        ("memory", "runtime.memory.memory"),
        ("memory_port", "runtime.memory.memory_port"),
        ("memory_prompt", "runtime.memory.memory_prompt"),
    ),
)
def test_legacy_memory_module_is_the_canonical_module(
    legacy_name: str,
    canonical_name: str,
) -> None:
    legacy_module = import_module(legacy_name)
    canonical_module = import_module(canonical_name)

    assert legacy_module is canonical_module


def test_default_memory_paths_remain_at_the_project_root() -> None:
    from local_memory import load_memory_config
    from mem0_memory import load_mem0_config
    from memory import Memory

    assert Path(Memory().path) == ROOT / "memory_store.json"
    assert load_memory_config(environ={}).data_root == ROOT / ".olivia_data" / "memory"
    assert load_mem0_config(environ={}).data_root == (
        ROOT / ".olivia_data" / "memory" / "mem0"
    )
