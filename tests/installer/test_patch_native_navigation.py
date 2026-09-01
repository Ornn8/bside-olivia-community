from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

import installer.patch_native_navigation as native_navigation
from installer.patch_native_navigation import (
    NativeNavigationPatchError,
    patch_native_navigation,
)


MAIN_MEMBER = "assets/synthetic-main.js"
HOME_ROUTE = "fixture-home-route;"
MAILBOX_DISABLED = "fixture-mailbox-disabled;"
MAILBOX_ENABLED = "fixture-mailbox-enabled;"
OFFLINE_WIDGETS_DISABLED = "fixture-widgets-disabled;"
OFFLINE_WIDGETS_ENABLED = "fixture-widgets-enabled;"
OFFLINE_REQUEST_BLOCKED = "fixture-request-blocked;"
OFFLINE_REQUEST_ALLOWED = "fixture-request-allowed;"
MAIL_FETCH_SKIPPED = "fixture-fetch-skipped;"
MAIL_FETCH_ALLOWED = "fixture-fetch-allowed;"
OFFLINE_POLL_SKIPPED = "fixture-poll-skipped;"
OFFLINE_POLL_ALLOWED = "fixture-poll-allowed;"
LOCAL_BRIDGE = "fixture-local-bridge;"
OFFLINE_CALL_PATCH = b"ALLOW!"
STUDIO_SIGNATURES = (
    b"studio01BLOCKED-tail",
    b"studio02BLOCKED-tail",
    b"studio03BLOCKED-tail",
    b"studio04BLOCKED-tail",
)
CONTAINER_SIGNATURE = b"cont01BLOCKED-tail"


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
    _write_compatibility_manifest(root, originals)
    return originals


def _fingerprint(value: bytes) -> tuple[int, str]:
    return len(value), hashlib.sha256(value).hexdigest()


def _managed_client_root(work_root: Path) -> Path:
    return work_root / "app" / "0.0.9.627"


def _patched_feapp_fixture(source: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
        comment = archive.comment
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.comment = comment
        for info, payload in members:
            if info.filename == MAIN_MEMBER:
                text = payload.decode("utf-8")
                for before, after in (
                    (MAILBOX_DISABLED, MAILBOX_ENABLED),
                    (OFFLINE_WIDGETS_DISABLED, OFFLINE_WIDGETS_ENABLED),
                    (OFFLINE_REQUEST_BLOCKED, OFFLINE_REQUEST_ALLOWED),
                    (MAIL_FETCH_SKIPPED, MAIL_FETCH_ALLOWED),
                    (OFFLINE_POLL_SKIPPED, OFFLINE_POLL_ALLOWED),
                ):
                    text = text.replace(before, after, 1)
                payload = text.encode("utf-8")
            archive.writestr(info, payload)
    return output.getvalue()


def _patched_binary_fixture(
    source: bytes, signatures: tuple[bytes, ...], offset: int
) -> bytes:
    patched = bytearray(source)
    for signature in signatures:
        start = source.index(signature) + offset
        patched[start : start + len(OFFLINE_CALL_PATCH)] = OFFLINE_CALL_PATCH
    return bytes(patched)


def _file_state(value: bytes) -> dict[str, object]:
    size, digest = _fingerprint(value)
    return {"size_bytes": size, "sha256": digest}


def _write_compatibility_manifest(
    client_root: Path, originals: dict[Path, bytes]
) -> Path:
    feapp = client_root / "resources" / "feapp.dat"
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    container = client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    patched = {
        feapp: _patched_feapp_fixture(originals[feapp]),
        studio: _patched_binary_fixture(originals[studio], STUDIO_SIGNATURES, 8),
        container: _patched_binary_fixture(originals[container], (CONTAINER_SIGNATURE,), 6),
    }
    manifest = {
        "schema_version": "olivia.native-navigation-compatibility.v1",
        "client_version": "0.0.9.627",
        "files": [
            {
                "id": "feapp",
                "relative_path": "resources/feapp.dat",
                "original": _file_state(originals[feapp]),
                "patched": _file_state(patched[feapp]),
                "archive_member": MAIN_MEMBER,
                "text_replacements": [
                    {"id": name, "before": before, "after": after}
                    for name, before, after in (
                        ("mailbox", MAILBOX_DISABLED, MAILBOX_ENABLED),
                        ("widgets", OFFLINE_WIDGETS_DISABLED, OFFLINE_WIDGETS_ENABLED),
                        ("request", OFFLINE_REQUEST_BLOCKED, OFFLINE_REQUEST_ALLOWED),
                        ("fetch", MAIL_FETCH_SKIPPED, MAIL_FETCH_ALLOWED),
                        ("poll", OFFLINE_POLL_SKIPPED, OFFLINE_POLL_ALLOWED),
                    )
                ],
            },
            {
                "id": "studio_ui",
                "relative_path": "plugins/Studio/NutStudioUI.dll",
                "original": _file_state(originals[studio]),
                "patched": _file_state(patched[studio]),
                "binary_replacements": [
                    {
                        "id": f"studio-{index}",
                        "signature_hex": signature.hex(),
                        "patch_offset": 8,
                        "replacement_hex": OFFLINE_CALL_PATCH.hex(),
                    }
                    for index, signature in enumerate(STUDIO_SIGNATURES, start=1)
                ],
            },
            {
                "id": "container_plugin",
                "relative_path": "plugins/Container/NutContainerPlugin.dll",
                "original": _file_state(originals[container]),
                "patched": _file_state(patched[container]),
                "binary_replacements": [
                    {
                        "id": "container-1",
                        "signature_hex": CONTAINER_SIGNATURE.hex(),
                        "patch_offset": 6,
                        "replacement_hex": OFFLINE_CALL_PATCH.hex(),
                    }
                ],
            },
        ],
    }
    path = client_root.parents[1] / "local_backend" / "installer" / native_navigation.COMPATIBILITY_MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _patched_signature(signature: bytes, call_offset: int) -> bytes:
    return (
        signature[:call_offset]
        + OFFLINE_CALL_PATCH
        + signature[call_offset + len(OFFLINE_CALL_PATCH) :]
    )


def test_public_patcher_contains_no_private_build_signatures() -> None:
    assert not hasattr(native_navigation, "_SUPPORTED_INPUT_FINGERPRINTS")
    assert not hasattr(native_navigation, "STUDIO_SIGNATURES")
    assert not hasattr(native_navigation, "CONTAINER_SIGNATURE")


def test_patch_native_navigation_requires_install_private_manifest(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    manifest = (
        tmp_path
        / "local_backend"
        / "installer"
        / native_navigation.COMPATIBILITY_MANIFEST_NAME
    )
    manifest.unlink()

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_MANIFEST_REQUIRED",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    assert {path: path.read_bytes() for path in originals} == originals


def test_patch_native_navigation_removes_orphan_staging_files_before_recovery(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    orphans = []
    for path in originals:
        orphan = path.with_name(path.name + ".native-nav-interrupted.tmp")
        orphan.write_bytes(b"partial")
        orphans.append(orphan)

    result = patch_native_navigation(client_root, work_root=tmp_path)

    assert result["status"] == "PATCHED"
    assert not any(path.exists() for path in orphans)


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
    assert raised.value.__cause__ is None
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


def test_patch_native_navigation_aggregates_cleanup_failure_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    feapp = client_root / "resources" / "feapp.dat"
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    failed_backup = feapp.with_name(feapp.name + ".native-nav.orig")
    real_replace = native_navigation.os.replace
    real_unlink = Path.unlink
    publish_failed = False
    cleanup_attempts: list[Path] = []

    def fail_publication_once(source: Path, destination: Path) -> None:
        nonlocal publish_failed
        if Path(destination) == studio and not publish_failed:
            publish_failed = True
            raise OSError("synthetic publication failure")
        real_replace(source, destination)

    def fail_one_backup_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.endswith(".native-nav.orig"):
            cleanup_attempts.append(path)
        if path == failed_backup:
            raise OSError(f"synthetic cleanup failure: {path}")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(native_navigation.os, "replace", fail_publication_once)
    monkeypatch.setattr(Path, "unlink", fail_one_backup_cleanup)

    with pytest.raises(NativeNavigationPatchError) as raised:
        patch_native_navigation(client_root, work_root=tmp_path)

    assert str(raised.value) == "NATIVE_NAV_CLEANUP_FAILED"
    assert str(tmp_path) not in str(raised.value)
    assert len(cleanup_attempts) == 3
    assert patch_native_navigation(client_root, work_root=tmp_path)["status"] == "PATCHED"


def test_patch_native_navigation_attempts_every_rollback_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    targets = set(originals)
    real_replace = native_navigation.os.replace
    real_read = native_navigation._read_bytes
    target_replaces = {path: 0 for path in targets}
    rollback_attempts: list[Path] = []
    verification_failed = False

    def fail_one_rollback(source: Path, destination: Path) -> None:
        destination = Path(destination)
        if destination in targets:
            target_replaces[destination] += 1
            if target_replaces[destination] == 2:
                rollback_attempts.append(destination)
                if destination.name == "NutContainerPlugin.dll":
                    raise OSError(f"synthetic rollback failure: {destination}")
        real_replace(source, destination)

    def fail_first_verification(path: Path, error_code: str) -> bytes:
        nonlocal verification_failed
        if error_code == "NATIVE_NAV_PUBLISHED_READ_FAILED" and not verification_failed:
            verification_failed = True
            raise NativeNavigationPatchError(error_code)
        return real_read(path, error_code)

    monkeypatch.setattr(native_navigation.os, "replace", fail_one_rollback)
    monkeypatch.setattr(native_navigation, "_read_bytes", fail_first_verification)

    with pytest.raises(NativeNavigationPatchError) as raised:
        patch_native_navigation(client_root, work_root=tmp_path)

    assert str(raised.value) == "NATIVE_NAV_ROLLBACK_FAILED"
    assert str(tmp_path) not in str(raised.value)
    assert set(rollback_attempts) == targets
    assert patch_native_navigation(client_root, work_root=tmp_path)["status"] == "PATCHED"


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


def test_patch_native_navigation_recovers_partial_live_publication(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    patch_native_navigation(client_root, work_root=tmp_path)
    studio = client_root / "plugins" / "Studio" / "NutStudioUI.dll"
    container = client_root / "plugins" / "Container" / "NutContainerPlugin.dll"
    studio.write_bytes(originals[studio])
    container.write_bytes(originals[container])

    recovered = patch_native_navigation(client_root, work_root=tmp_path)
    repeated = patch_native_navigation(client_root, work_root=tmp_path)

    assert recovered["status"] == "PATCHED"
    assert repeated["status"] == "ALREADY_PATCHED"


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


def test_patch_native_navigation_recovers_partial_backup_publication(
    tmp_path: Path,
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    feapp = client_root / "resources" / "feapp.dat"
    feapp.with_name(feapp.name + ".native-nav.orig").write_bytes(originals[feapp])

    recovered = patch_native_navigation(client_root, work_root=tmp_path)
    repeated = patch_native_navigation(client_root, work_root=tmp_path)

    assert recovered["status"] == "PATCHED"
    assert repeated["status"] == "ALREADY_PATCHED"
    for path, original in originals.items():
        assert path.with_name(path.name + ".native-nav.orig").read_bytes() == original


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
) -> None:
    client_root = _managed_client_root(tmp_path)
    originals = _write_supported_client(client_root)
    manifest_path = (
        tmp_path
        / "local_backend"
        / "installer"
        / native_navigation.COMPATIBILITY_MANIFEST_NAME
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["patched"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        NativeNavigationPatchError,
        match="NATIVE_NAV_PATCH_MISMATCH",
    ):
        patch_native_navigation(client_root, work_root=tmp_path)

    _assert_transaction_rolled_back(originals)
