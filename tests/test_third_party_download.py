from __future__ import annotations

import hashlib
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading

import pytest


SCRIPT = Path(__file__).parents[1] / "tools" / "download_third_party.py"
SPEC = importlib.util.spec_from_file_location("download_third_party", SCRIPT)
assert SPEC and SPEC.loader
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


class _FixtureHandler(BaseHTTPRequestHandler):
    payload = b"fixture third-party payload\n"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args):
        return


@pytest.fixture()
def fixture_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/payload.bin"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture()
def external_data_root():
    with tempfile.TemporaryDirectory(prefix="bside-third-party-") as root:
        yield Path(root)


def _manifest(tmp_path: Path, source_url_value: str, **overrides):
    payload = _FixtureHandler.payload
    item = {
        "id": "fixture",
        "source_url": source_url_value,
        "version": "1.0.0",
        "license": "MIT",
        "target_path": "third_party/fixture.bin",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    item.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "items": [item]}), encoding="utf-8")
    return path


def test_dry_run_never_contacts_source_or_writes(tmp_path, external_data_root, capsys):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin")
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root)]) == 0
    assert not (external_data_root / "third_party").exists()
    output = capsys.readouterr().out
    assert "validated 1 item(s); mode=dry-run" in output
    assert "DRY-RUN fixture -> third_party/fixture.bin" in output
    assert str(external_data_root) not in output


def test_install_requires_explicit_license_acceptance(tmp_path, external_data_root):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin")
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root), "--install"]) == 2
    assert not (external_data_root / "third_party").exists()


def test_hash_mismatch_leaves_no_target(fixture_url, tmp_path, external_data_root):
    manifest = _manifest(tmp_path, fixture_url, sha256="f" * 64)
    data_root = external_data_root
    assert download.main(["--manifest", str(manifest), "--data-root", str(data_root), "--install", "--accept-licenses"]) == 2
    assert not (data_root / "third_party" / "fixture.bin").exists()
    assert not list(data_root.rglob(".download-*"))


def test_loopback_download_verifies_and_installs(fixture_url, tmp_path, external_data_root, capsys):
    manifest = _manifest(tmp_path, fixture_url)
    data_root = external_data_root
    assert download.main(["--manifest", str(manifest), "--data-root", str(data_root), "--install", "--accept-licenses"]) == 0
    assert (data_root / "third_party" / "fixture.bin").read_bytes() == _FixtureHandler.payload
    assert str(external_data_root) not in capsys.readouterr().out


def test_matching_existing_target_is_reused_without_download(tmp_path, external_data_root):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin")
    target = external_data_root / "third_party" / "fixture.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(_FixtureHandler.payload)
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root), "--install", "--accept-licenses"]) == 0
    assert target.read_bytes() == _FixtureHandler.payload


def test_mismatching_existing_target_is_not_overwritten(tmp_path, external_data_root):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin")
    target = external_data_root / "third_party" / "fixture.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user file")
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root), "--install", "--accept-licenses"]) == 2
    assert target.read_bytes() == b"user file"


def test_path_escape_fails_closed(tmp_path, external_data_root):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin", target_path="../outside.bin")
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root), "--dry-run"]) == 2


def test_data_root_ancestor_of_repository_is_rejected(tmp_path):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin")
    repo_parent = SCRIPT.parents[1].parent
    assert download.main(["--manifest", str(manifest), "--data-root", str(repo_parent), "--dry-run"]) == 2


def test_credential_url_is_rejected_without_secret_in_output(tmp_path, external_data_root, capsys):
    sentinel_value = "fixture-sentinel-123"
    url = f"https://user:{sentinel_value}@example.invalid/file?token={sentinel_value}"
    manifest = _manifest(tmp_path, url)
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root), "--dry-run"]) == 2
    output = capsys.readouterr().err
    assert sentinel_value not in output
    assert url not in output


def test_missing_source_license_or_hash_fails_closed(tmp_path, external_data_root):
    for field, value in (("source_url", ""), ("license", ""), ("sha256", "")):
        manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin", **{field: value})
        assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root), "--dry-run"]) == 2


def test_unknown_manifest_fields_fail_closed(tmp_path, external_data_root):
    manifest = _manifest(tmp_path, "https://downloads.example.invalid/payload.bin", unexpected="value")
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root)]) == 2

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert download.main(["--manifest", str(manifest), "--data-root", str(external_data_root)]) == 2
