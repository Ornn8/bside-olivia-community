from __future__ import annotations

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    (
        ("reply_orchestrator", "runtime.reply.reply_orchestrator"),
        ("reply_model_quality", "runtime.reply.reply_model_quality"),
    ),
)
def test_legacy_reply_module_is_the_canonical_module(
    legacy_name: str,
    canonical_name: str,
) -> None:
    legacy_module = import_module(legacy_name)
    canonical_module = import_module(canonical_name)

    assert legacy_module is canonical_module
