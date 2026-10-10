"""A patch must carry the shared address module imported during startup."""
from pathlib import Path

from installer.full_patch import (
    PAYLOAD_REQUIRED_RELATIVE_FILES, PAYLOAD_REQUIRED_ROOT_FILES, copy_project_payload,
)


def test_installed_payload_contains_official_address_module(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'installed'
    for relative in (*PAYLOAD_REQUIRED_ROOT_FILES, *PAYLOAD_REQUIRED_RELATIVE_FILES):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# synthetic payload\n', encoding='utf-8')
    name = 'runtime/official_endpoints.py'
    module = Path(__file__).parents[2] / name
    target = source / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(module.read_bytes())

    copied = copy_project_payload(source, destination)

    assert name in copied
    assert (destination / name).read_bytes() == module.read_bytes()
