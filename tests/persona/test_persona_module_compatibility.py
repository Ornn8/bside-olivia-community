from __future__ import annotations

from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    (
        ("persona_loader", "runtime.persona.persona_loader"),
        ("persona_assembly", "runtime.persona.persona_assembly"),
        ("persona_provider", "runtime.persona.persona_provider"),
    ),
)
def test_legacy_persona_module_is_the_canonical_module(
    legacy_name: str,
    canonical_name: str,
) -> None:
    legacy_module = import_module(legacy_name)
    canonical_module = import_module(canonical_name)

    assert legacy_module is canonical_module
