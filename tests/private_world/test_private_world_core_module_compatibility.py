from importlib import import_module

import pytest


@pytest.mark.parametrize(("legacy", "canonical"), (
    ("private_world_port", "runtime.private_world.port"),
    ("private_world_commands", "runtime.private_world.commands"),
    ("private_world_ledger", "runtime.private_world.ledger"),
    ("private_world_reducer", "runtime.private_world.reducer"),
    ("private_world_service", "runtime.private_world.service"),
))
def test_legacy_private_world_core_module_is_canonical(legacy: str, canonical: str) -> None:
    assert import_module(legacy) is import_module(canonical)
