from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

import installer.patch_native_navigation as native_navigation
from installer.patch_native_navigation import (
    NativeNavigationPatchError,
    patch_native_navigation,
)


MAIN_MEMBER = "assets/main-31595bd3.js"
HOME_ROUTE = (
    '!z.isNew||$?(await t.replace({name:ve.Home}),' 
    'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
)
MAILBOX_DISABLED = "N3=!1,Ss=!1,wa=({onComplete"
MAILBOX_ENABLED = "N3=!0,Ss=!1,wa=({onComplete"
OFFLINE_WIDGETS_DISABLED = (
    "e.isOfflineMode&&(l.value.mailWidget!==!1&&(l.value.mailWidget=!1),"
    "l.value.musicWidget!==!1&&(l.value.musicWidget=!1))"
)
OFFLINE_WIDGETS_ENABLED = "l.value.mailWidget=!0,l.value.musicWidget=!0"
OFFLINE_REQUEST_BLOCKED = "if(t.isOfflineMode)throw new Ol(e)"
OFFLINE_REQUEST_ALLOWED = "if(!1)throw new Ol(e)"
MAIL_FETCH_SKIPPED = "He(()=>{p.value||d.fetchMailList(!0)})"
MAIL_FETCH_ALLOWED = "He(()=>{d.fetchMailList(!0)})"
OFFLINE_POLL_SKIPPED = (
    "s.isOfflineMode||(s.appMode===Se.PRO?Lt().proRestoreFromApi():"
    "s.appMode===Se.LITE&&(Lt().liteStartPoll(),uo().startPolling()))"
)
OFFLINE_POLL_ALLOWED = (
    "s.appMode===Se.PRO?Lt().proRestoreFromApi():"
    "s.appMode===Se.LITE&&(s.isOfflineMode?uo().startPolling():"
    "(Lt().liteStartPoll(),uo().startPolling()))"
)
LOCAL_BRIDGE = (
    'c.appConf.toyWsUrl="ws://127.0.0.1:8899/ws",'
    'c.appConf.toyApiUrl="http://127.0.0.1:8899"'
)
OFFLINE_CALL_PATCH = bytes((0x33, 0xC0, 0x90, 0x90, 0x90, 0x90))
STUDIO_SIGNATURES = (
    bytes.fromhex("CB E8 D2 37 08 00 EB 1E FF 15 B2 EC 08 00 48 8D 8F A8"),
    bytes.fromhex("CB E8 72 34 08 00 EB 1E FF 15 52 E9 08 00 48 8D 8F A8"),
    bytes.fromhex("CB E8 B2 1F 08 00 EB 2B FF 15 92 D4 08 00 84 C0 75 14"),
    bytes.fromhex("CB E8 FF 1D 08 00 EB 1C FF 15 DF D2 08 00 48 8D 4F 38"),
)
CONTAINER_SIGNATURE = bytes.fromhex(
    "48 8B DA 48 8B F9 FF 15 61 A4 04 00 84 C0 0F 85"
)


def _write_feapp(path: Path, javascript: str) -> bytes:
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MAIN_MEMBER, javascript)
        archive.writestr("assets/index.css", "body{display:block}")
    return path.read_bytes()


def _read_main(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(MAIN_MEMBER).decode("utf-8")


def _write_supported_client(root: Path) -> dict[Path, bytes]:
    feapp = root / "resources" / "feapp.dat"
    originals = {
        feapp: _write_feapp(
            feapp,
            HOME_ROUTE
            + MAILBOX_DISABLED
            + OFFLINE_WIDGETS_DISABLED
            + OFFLINE_REQUEST_BLOCKED
            + MAIL_FETCH_SKIPPED
            + OFFLINE_POLL_SKIPPED
            + LOCAL_BRIDGE,
        )
    }
    studio = root / "plugins" / "Studio" / "NutStudioUI.dll"
    studio.parent.mkdir(parents=True)
    studio.write_bytes(b"studio-prefix" + b"gap".join(STUDIO_SIGNATURES) + b"studio-suffix")
    originals[studio] = studio.read_bytes()
    container = root / "plugins" / "Container" / "NutContainerPlugin.dll"
    container.parent.mkdir(parents=True)
    container.write_bytes(b"container-prefix" + CONTAINER_SIGNATURE + b"container-suffix")
    originals[container] = container.read_bytes()
    return originals


def _patched_signature(signature: bytes, call_offset: int) -> bytes:
    return (
        signature[:call_offset]
        + OFFLINE_CALL_PATCH
        + signature[call_offset + len(OFFLINE_CALL_PATCH) :]
    )


def _assert_transaction_rolled_back(originals: dict[Path, bytes]) -> None:
    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()
    roots = {path.parents[2] for path in originals}
    assert not [temporary for root in roots for temporary in root.rglob("*.tmp")]


def test_patch_native_navigation_enables_widgets_without_changing_home_route(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    feapp = client_root / "resources" / "feapp.dat"
    originals = _write_supported_client(client_root)

    result = patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(feapp)
    backup = feapp.with_name("feapp.dat.native-nav.orig")
    assert result["status"] == "PATCHED"
    assert result["client_version"] == "0.0.9.627"
    assert backup.read_bytes() == originals[feapp]
    assert MAILBOX_ENABLED in patched
    assert MAILBOX_DISABLED not in patched
    assert OFFLINE_WIDGETS_ENABLED in patched
    assert OFFLINE_WIDGETS_DISABLED not in patched
    assert HOME_ROUTE in patched
    assert LOCAL_BRIDGE in patched
    assert 'localStorage.setItem("appMode","lite")' not in patched
    assert "await t.replace({name:ve.Collection})" not in patched


def test_patch_native_navigation_patches_both_native_dlls_with_original_backups(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(client_root)

    result = patch_native_navigation(client_root, work_root=tmp_path)

    assert set(result["files"]) == {"feapp", "studio_ui", "container_plugin"}
    for path, original in originals.items():
        assert path.with_name(path.name + ".native-nav.orig").read_bytes() == original

    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    studio_patched = studio.read_bytes()
    for signature in STUDIO_SIGNATURES:
        assert signature not in studio_patched
        assert _patched_signature(signature, 8) in studio_patched

    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    container_patched = container.read_bytes()
    assert CONTAINER_SIGNATURE not in container_patched
    assert _patched_signature(CONTAINER_SIGNATURE, 6) in container_patched


def test_patch_native_navigation_allows_mail_requests_through_local_bridge(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    _write_supported_client(client_root)

    patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(client_root / "resources" / "feapp.dat")
    assert OFFLINE_REQUEST_ALLOWED in patched
    assert OFFLINE_REQUEST_BLOCKED not in patched
    assert LOCAL_BRIDGE in patched


def test_patch_native_navigation_fetches_mail_while_client_is_offline(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    _write_supported_client(client_root)

    patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(client_root / "resources" / "feapp.dat")
    assert MAIL_FETCH_ALLOWED in patched
    assert MAIL_FETCH_SKIPPED not in patched


def test_patch_native_navigation_starts_lite_mail_polling_while_offline(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    _write_supported_client(client_root)

    patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(client_root / "resources" / "feapp.dat")
    assert OFFLINE_POLL_ALLOWED in patched
    assert OFFLINE_POLL_SKIPPED not in patched


def test_patch_native_navigation_rejects_a_missing_signature_before_writes(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(client_root)
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    studio.write_bytes(
        studio.read_bytes().replace(
            STUDIO_SIGNATURES[2],
            b"\x7f" * len(STUDIO_SIGNATURES[2]),
            1,
        )
    )
    originals[studio] = studio.read_bytes()

    with pytest.raises(
        NativeNavigationPatchError,
        match=r"NutStudioUI\.dll offline call #3 signature.*found 0",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()
    assert not list(client_root.rglob("*.tmp"))


def test_patch_native_navigation_rejects_a_repeated_signature_before_writes(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(client_root)
    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    container.write_bytes(container.read_bytes() + b"duplicate" + CONTAINER_SIGNATURE)
    originals[container] = container.read_bytes()

    with pytest.raises(
        NativeNavigationPatchError,
        match=r"NutContainerPlugin\.dll lite-bar call #1 signature.*found 2",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()
    assert not list(client_root.rglob("*.tmp"))


def test_patch_native_navigation_rolls_back_when_second_target_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(client_root)
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    real_replace = native_navigation.os.replace
    failed = False

    def fail_once_on_studio(source: Path, destination: Path) -> None:
        nonlocal failed
        if Path(destination) == studio and not failed:
            failed = True
            raise OSError("synthetic second-target publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(native_navigation.os, "replace", fail_once_on_studio)

    with pytest.raises(
        NativeNavigationPatchError,
        match="native navigation publication failed",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert failed is True
    _assert_transaction_rolled_back(originals)


def test_patch_native_navigation_rolls_back_when_third_target_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(client_root)
    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    real_replace = native_navigation.os.replace
    failed = False

    def fail_once_on_container(source: Path, destination: Path) -> None:
        nonlocal failed
        if Path(destination) == container and not failed:
            failed = True
            raise OSError("synthetic third-target publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(native_navigation.os, "replace", fail_once_on_container)

    with pytest.raises(
        NativeNavigationPatchError,
        match="native navigation publication failed",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert failed is True
    _assert_transaction_rolled_back(originals)


def test_patch_native_navigation_is_idempotent_with_complete_backups(
    tmp_path: Path,
) -> None:
    client_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(client_root)

    first = patch_native_navigation(client_root, work_root=tmp_path)
    first_patched = {path: path.read_bytes() for path in originals}
    repeated = patch_native_navigation(client_root, work_root=tmp_path)

    assert first["status"] == "PATCHED"
    assert repeated["status"] == "ALREADY_PATCHED"
    assert repeated["files"] == first["files"]
    for path, original in originals.items():
        assert path.read_bytes() == first_patched[path]
        assert path.with_name(path.name + ".native-nav.orig").read_bytes() == original
