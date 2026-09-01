from __future__ import annotations

import hashlib
import io
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
_PRODUCTION_SUPPORTED_FINGERPRINTS = {
    name: dict(states)
    for name, states in native_navigation._SUPPORTED_INPUT_FINGERPRINTS.items()
}


def _write_feapp(path: Path, javascript: str) -> bytes:
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            (MAIN_MEMBER, javascript),
            ("assets/index.css", "body{display:block}"),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
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


def _fingerprint(value: bytes) -> tuple[int, str]:
    return len(value), hashlib.sha256(value).hexdigest()


def _managed_client_root(work_root: Path) -> Path:
    return work_root / "app" / "0.0.9.627"


@pytest.fixture(autouse=True)
def _trust_synthetic_supported_build(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path("synthetic") / "0.0.9.627"
    feapp_buffer = io.BytesIO()
    with zipfile.ZipFile(feapp_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            (
                MAIN_MEMBER,
                HOME_ROUTE
                + MAILBOX_DISABLED
                + OFFLINE_WIDGETS_DISABLED
                + OFFLINE_REQUEST_BLOCKED
                + MAIL_FETCH_SKIPPED
                + OFFLINE_POLL_SKIPPED
                + LOCAL_BRIDGE,
            ),
            ("assets/index.css", "body{display:block}"),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    originals = {
        "feapp": feapp_buffer.getvalue(),
        "studio_ui": b"studio-prefix" + b"gap".join(STUDIO_SIGNATURES) + b"studio-suffix",
        "container_plugin": b"container-prefix" + CONTAINER_SIGNATURE + b"container-suffix",
    }
    patched = {
        "feapp": native_navigation._patched_feapp(originals["feapp"]),
        "studio_ui": native_navigation._patched_dll(
            originals["studio_ui"], STUDIO_SIGNATURES, 8, "studio"
        ),
        "container_plugin": native_navigation._patched_dll(
            originals["container_plugin"], (CONTAINER_SIGNATURE,), 6, "container"
        ),
    }
    monkeypatch.setattr(
        native_navigation,
        "_SUPPORTED_INPUT_FINGERPRINTS",
        {
            name: {
                "original": _fingerprint(originals[name]),
                "patched": _fingerprint(patched[name]),
            }
            for name in originals
        },
    )


def _patched_signature(signature: bytes, call_offset: int) -> bytes:
    return (
        signature[:call_offset]
        + OFFLINE_CALL_PATCH
        + signature[call_offset + len(OFFLINE_CALL_PATCH) :]
    )


@pytest.mark.parametrize(
    ("name", "state", "expected"),
    (
        ("feapp", "original", (27_769_992, "2abf4bc1208d3f7f39fbd2b4556c980ce5d641c75cee8863c3ca69e6029f7dcf")),
        ("feapp", "patched", (27_769_978, "71c30b40dbbbf9d6949828425d5b093ad32aaf2d7b3c53b3f1c5a4a42643cc42")),
        ("studio_ui", "original", (1_297_376, "3756767fc01c2a1c034a56c1ae2920651f13021a1b4cc0c3ed291fc92a9728e1")),
        ("studio_ui", "patched", (1_297_376, "294dcfe023c84bc83bdd531d8431bf76b2b7a1fbc0941a250ae8f4cf2ed8fa99")),
        ("container_plugin", "original", (498_144, "53b61d8e9766c5b1cf2af29ed1a4ac7985052db65c37aa1829c71416050e31d1")),
        ("container_plugin", "patched", (498_144, "d78112ca218f805d437d2b03fe4c772c7cb279848dccce16570cbd466fe66ab4")),
    ),
)
def test_supported_build_fingerprints_are_frozen(
    name: str, state: str, expected: tuple[int, str]
) -> None:
    assert _PRODUCTION_SUPPORTED_FINGERPRINTS[name][state] == expected


def _assert_transaction_rolled_back(originals: dict[Path, bytes]) -> None:
    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()
    roots = {path.parents[2] for path in originals}
    assert not [temporary for root in roots for temporary in root.rglob("*.tmp")]


def test_patch_native_navigation_enables_widgets_without_changing_home_route(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
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
    with zipfile.ZipFile(io.BytesIO(originals[feapp])) as before, zipfile.ZipFile(
        feapp
    ) as after:
        assert before.namelist() == after.namelist()
        assert before.comment == after.comment
        assert after.read("assets/index.css") == before.read("assets/index.css")
        expected_main = before.read(MAIN_MEMBER).decode("utf-8")
    for old, new in (
        (MAILBOX_DISABLED, MAILBOX_ENABLED),
        (OFFLINE_WIDGETS_DISABLED, OFFLINE_WIDGETS_ENABLED),
        (OFFLINE_REQUEST_BLOCKED, OFFLINE_REQUEST_ALLOWED),
        (MAIL_FETCH_SKIPPED, MAIL_FETCH_ALLOWED),
        (OFFLINE_POLL_SKIPPED, OFFLINE_POLL_ALLOWED),
    ):
        expected_main = expected_main.replace(old, new, 1)
    assert patched == expected_main


def test_patch_native_navigation_patches_both_native_dlls_with_original_backups(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)

    result = patch_native_navigation(client_root, work_root=tmp_path)

    assert set(result["files"]) == {"feapp", "studio_ui", "container_plugin"}
    for path, original in originals.items():
        assert path.with_name(path.name + ".native-nav.orig").read_bytes() == original

    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    studio_patched = studio.read_bytes()
    studio_changed = {
        index
        for index, (before, after) in enumerate(
            zip(originals[studio], studio_patched, strict=True)
        )
        if before != after
    }
    assert studio_changed == {
        originals[studio].find(signature) + 8 + byte_offset
        for signature in STUDIO_SIGNATURES
        for byte_offset in range(len(OFFLINE_CALL_PATCH))
        if signature[8 + byte_offset] != OFFLINE_CALL_PATCH[byte_offset]
    }
    for signature in STUDIO_SIGNATURES:
        assert signature not in studio_patched
        assert _patched_signature(signature, 8) in studio_patched

    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    container_patched = container.read_bytes()
    assert {
        index
        for index, (before, after) in enumerate(
            zip(originals[container], container_patched, strict=True)
        )
        if before != after
    } == {
        originals[container].find(CONTAINER_SIGNATURE) + 6 + byte_offset
        for byte_offset in range(len(OFFLINE_CALL_PATCH))
        if CONTAINER_SIGNATURE[6 + byte_offset] != OFFLINE_CALL_PATCH[byte_offset]
    }
    assert CONTAINER_SIGNATURE not in container_patched
    assert _patched_signature(CONTAINER_SIGNATURE, 6) in container_patched


def test_patch_result_contains_only_logical_ids_hashes_and_sizes(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    _write_supported_client(client_root)

    result = patch_native_navigation(client_root, work_root=tmp_path)

    assert str(tmp_path) not in repr(result)
    for name, evidence in result["files"].items():
        assert name in {"feapp", "studio_ui", "container_plugin"}
        assert set(evidence) == {
            "source_size",
            "source_sha256",
            "backup_size",
            "backup_sha256",
            "patched_size",
            "patched_sha256",
        }
        assert evidence["source_size"] == evidence["backup_size"]
        assert evidence["source_sha256"] == evidence["backup_sha256"]


def test_patch_native_navigation_allows_mail_requests_through_local_bridge(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    _write_supported_client(client_root)

    patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(client_root / "resources" / "feapp.dat")
    assert OFFLINE_REQUEST_ALLOWED in patched
    assert OFFLINE_REQUEST_BLOCKED not in patched
    assert LOCAL_BRIDGE in patched


def test_patch_native_navigation_fetches_mail_while_client_is_offline(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    _write_supported_client(client_root)

    patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(client_root / "resources" / "feapp.dat")
    assert MAIL_FETCH_ALLOWED in patched
    assert MAIL_FETCH_SKIPPED not in patched


def test_patch_native_navigation_starts_lite_mail_polling_while_offline(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    _write_supported_client(client_root)

    patch_native_navigation(client_root, work_root=tmp_path)

    patched = _read_main(client_root / "resources" / "feapp.dat")
    assert OFFLINE_POLL_ALLOWED in patched
    assert OFFLINE_POLL_SKIPPED not in patched


def test_patch_native_navigation_rejects_unknown_build_with_all_anchors(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    studio.write_bytes(b"unknown-build" + studio.read_bytes())

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNSUPPORTED_INPUT",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert studio.read_bytes().startswith(b"unknown-build")
    for path in originals:
        assert not path.with_name(path.name + ".native-nav.orig").exists()


def test_patch_native_navigation_reports_input_read_failure_without_a_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    original_read_bytes = Path.read_bytes

    def deny_studio(path: Path) -> bytes:
        if path == studio:
            raise PermissionError(f"denied: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny_studio)

    with pytest.raises(NativeNavigationPatchError) as raised:
        patch_native_navigation(client_root, work_root=tmp_path)

    assert str(raised.value) == "NATIVE_NAV_INPUT_READ_FAILED"
    assert str(tmp_path) not in str(raised.value)
    for path in originals:
        assert not path.with_name(path.name + ".native-nav.orig").exists()


def test_patch_native_navigation_rejects_unmanaged_version_root(
    tmp_path: Path,
) -> None:
    official_root = tmp_path / "0.0.9.627"
    originals = _write_supported_client(official_root)

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNMANAGED_ROOT",
    ):
        patch_native_navigation(official_root, work_root=tmp_path)

    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()


def test_patch_native_navigation_rejects_reparse_component_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    monkeypatch.setattr(
        native_navigation,
        "_is_reparse_point",
        lambda path: Path(path).name == "Studio",
    )

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNSAFE_PATH",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()


def test_patch_native_navigation_rejects_reparse_ancestor_of_work_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = tmp_path / "managed"
    client_root = _managed_client_root(work_root)
    originals = _write_supported_client(client_root)
    unsafe_ancestor = tmp_path
    monkeypatch.setattr(
        native_navigation,
        "_is_reparse_point",
        lambda path: Path(path) == unsafe_ancestor,
    )

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNSAFE_PATH",
    ):
        patch_native_navigation(client_root, work_root=work_root)

    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()


def test_patch_native_navigation_rechecks_reparse_points_immediately_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    studio_parent = client_root / "plugins" / "Studio"
    original_read_bytes = Path.read_bytes
    target_reads = 0
    unsafe = False

    def mark_unsafe_after_inputs_are_read(path: Path) -> bytes:
        nonlocal target_reads, unsafe
        value = original_read_bytes(path)
        if path in originals:
            target_reads += 1
            if target_reads == len(originals):
                unsafe = True
        return value

    monkeypatch.setattr(Path, "read_bytes", mark_unsafe_after_inputs_are_read)
    monkeypatch.setattr(
        native_navigation,
        "_is_reparse_point",
        lambda path: unsafe and Path(path) == studio_parent,
    )

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNSAFE_PATH",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    for path, original in originals.items():
        assert original_read_bytes(path) == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()


def test_patch_native_navigation_rechecks_each_managed_write_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    studio_parent = client_root / "plugins" / "Studio"
    original_open = Path.open
    unsafe = False

    def flip_after_first_staging_open(path: Path, *args: object, **kwargs: object):
        nonlocal unsafe
        stream = original_open(path, *args, **kwargs)
        if ".native-nav-" in path.name and path.suffix == ".tmp":
            unsafe = True
        return stream

    monkeypatch.setattr(Path, "open", flip_after_first_staging_open)
    monkeypatch.setattr(
        native_navigation,
        "_is_reparse_point",
        lambda path: unsafe and Path(path) == studio_parent,
    )

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNSAFE_PATH",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    _assert_transaction_rolled_back(originals)


def test_patch_native_navigation_rejects_a_missing_signature_before_writes(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
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
        match="NATIVE_NAV_UNSUPPORTED_INPUT",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    for path, original in originals.items():
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".native-nav.orig").exists()
    assert not list(client_root.rglob("*.tmp"))


def test_patch_native_navigation_rejects_a_repeated_signature_before_writes(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    container.write_bytes(container.read_bytes() + b"duplicate" + CONTAINER_SIGNATURE)
    originals[container] = container.read_bytes()

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_UNSUPPORTED_INPUT",
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
    client_root = _managed_client_root(tmp_path)
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
        match="NATIVE_NAV_PUBLICATION_FAILED",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert failed is True
    _assert_transaction_rolled_back(originals)


def test_patch_native_navigation_rolls_back_when_third_target_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
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
        match="NATIVE_NAV_PUBLICATION_FAILED",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert failed is True
    _assert_transaction_rolled_back(originals)


def test_patch_native_navigation_rolls_back_a_tampered_published_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    real_replace = native_navigation.os.replace
    corrupted = False

    def corrupt_first_container_publication(source: Path, destination: Path) -> None:
        nonlocal corrupted
        real_replace(source, destination)
        if Path(destination) == container and not corrupted:
            corrupted = True
            container.write_bytes(container.read_bytes() + b"tampered")

    monkeypatch.setattr(
        native_navigation.os,
        "replace",
        corrupt_first_container_publication,
    )

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_PUBLISHED_TAMPERED",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert corrupted is True
    _assert_transaction_rolled_back(originals)


def test_patch_native_navigation_is_idempotent_with_complete_backups(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
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


def test_patch_native_navigation_rejects_tampered_complete_backups(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    patch_native_navigation(client_root, work_root=tmp_path)
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    backup = studio.with_name(studio.name + ".native-nav.orig")
    backup.write_bytes(backup.read_bytes() + b"tampered")
    live_before = {path: path.read_bytes() for path in originals}

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_BACKUP_TAMPERED",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert {path: path.read_bytes() for path in originals} == live_before


def test_patch_native_navigation_rejects_partial_backups_with_stable_error(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    feapp = client_root / "resources" / "feapp.dat"
    feapp.with_name(feapp.name + ".native-nav.orig").write_bytes(originals[feapp])

    with pytest.raises(NativeNavigationPatchError) as raised:
        patch_native_navigation(client_root, work_root=tmp_path)

    assert str(raised.value) == "NATIVE_NAV_BACKUPS_INCOMPLETE"
    assert str(tmp_path) not in str(raised.value)


def test_patch_native_navigation_rejects_tampered_live_files_with_backups(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    patch_native_navigation(client_root, work_root=tmp_path)
    container = (
        client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    )
    container.write_bytes(container.read_bytes() + b"tampered")
    backups_before = {
        path: path.with_name(path.name + ".native-nav.orig").read_bytes()
        for path in originals
    }

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_LIVE_TAMPERED",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    for path, backup_value in backups_before.items():
        assert path.with_name(path.name + ".native-nav.orig").read_bytes() == backup_value


def test_patch_native_navigation_rejects_unregistered_patch_output_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    registered = {
        name: dict(states)
        for name, states in native_navigation._SUPPORTED_INPUT_FINGERPRINTS.items()
    }
    size, _ = registered["feapp"]["patched"]
    registered["feapp"]["patched"] = (size, "0" * 64)
    monkeypatch.setattr(
        native_navigation,
        "_SUPPORTED_INPUT_FINGERPRINTS",
        registered,
    )

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_PATCH_MISMATCH",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    _assert_transaction_rolled_back(originals)
