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


def test_windows_payload_excludes_macos_desktop_host_files(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'installed'
    for relative in (*PAYLOAD_REQUIRED_ROOT_FILES, *PAYLOAD_REQUIRED_RELATIVE_FILES):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# synthetic payload\n', encoding='utf-8')
    mac_only = ('desktop_backend.py', 'desktop_original_frontend.py', 'contracts/desktop_handshake.schema.json')
    for relative in (*mac_only, 'contracts/kept.schema.json', 'runtime/mac_keychain.py'):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}\n', encoding='utf-8')

    copy_project_payload(source, destination)

    for relative in mac_only:
        assert not (destination / relative).exists(), relative
    assert (destination / 'contracts/kept.schema.json').is_file()
    # Imported lazily on macOS only, so it may stay in the shared payload.
    assert (destination / 'runtime/mac_keychain.py').is_file()
