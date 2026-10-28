"""Prepare the verified original frontend for one macOS Electron session."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit
import zipfile

from installer.patch_letter_stickers import patch_letter_stickers
from installer.patch_native_locale import patch_native_locale
from installer.patch_native_navigation import patch_native_navigation
from patch_companion_settings import patch_companion_settings
from patch_feapp import _safe_extract, patch_feapp
from patch_webplayer import patch_webplayer


ORIGINAL_FEAPP_SHA256 = "c88f1dd4cb7c95e4902d74dd0c247962ffd65559e3907497b416078d3a6698b5"
ORIGINAL_WEBPLAYER_SHA256 = "565b5e3e113c2a9dfb90d5fa4f2a0ccda9b0151c118ae3365e6ee0c8624a451d"
CLIENT_VERSION = "0.0.9.627"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rewrite_backend_urls(root: Path, port: int, proxy_origin: str) -> None:
    parsed = urlsplit(proxy_origin)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("ORIGINAL_FRONTEND_PROXY_INVALID")
    local_http = f"http://127.0.0.1:{port}"
    local_ws = f"ws://127.0.0.1:{port}/ws"
    http_count = 0
    ws_count = 0
    for path in root.rglob("*"):
        if path.suffix not in {".html", ".js"} or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        http_count += content.count(local_http)
        ws_count += content.count(local_ws)
        changed = content.replace(local_http, proxy_origin)
        changed = changed.replace(local_ws, f"ws://127.0.0.1:{parsed.port}/ws")
        if changed != content:
            path.write_text(changed, encoding="utf-8")
    if http_count < 2 or ws_count != 1:
        raise ValueError("ORIGINAL_FRONTEND_ENDPOINT_PATCH_INVALID")



def prepare(source: Path, output: Path, port: int, proxy_origin: str) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    if not 1 <= port <= 65535:
        raise ValueError("ORIGINAL_FRONTEND_PORT_INVALID")
    if _sha256(source / "feapp.dat") != ORIGINAL_FEAPP_SHA256:
        raise ValueError("ORIGINAL_FRONTEND_FEAPP_INVALID")
    if _sha256(source / "webplayer.dat") != ORIGINAL_WEBPLAYER_SHA256:
        raise ValueError("ORIGINAL_FRONTEND_WEBPLAYER_INVALID")
    if (output / "frontend").exists() or (output / "webplayer").exists():
        raise ValueError("ORIGINAL_FRONTEND_OUTPUT_EXISTS")

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="original-client-", dir=output) as temporary:
        stage = Path(temporary)
        client = stage / "app" / CLIENT_VERSION
        resources = client / "resources"
        studio = client / "plugins" / "Studio"
        container = client / "plugins" / "Container"
        manifest_dir = stage / "local_backend" / "installer"
        for directory in (resources, studio, container, manifest_dir):
            directory.mkdir(parents=True)
        for name in ("feapp.dat", "webplayer.dat"):
            shutil.copy2(source / name, resources / name)
        shutil.copy2(source / "NutStudioUI.dll", studio / "NutStudioUI.dll")
        shutil.copy2(source / "NutContainerPlugin.dll", container / "NutContainerPlugin.dll")
        shutil.copy2(source / "native-navigation-compatibility.json", manifest_dir / "native-navigation-compatibility.json")

        patch_native_navigation(client, work_root=stage)
        patch_feapp(resources / "feapp.dat", f"ws://127.0.0.1:{port}/ws", work_root=resources)
        patch_companion_settings(resources / "feapp.dat", f"http://127.0.0.1:{port}", work_root=resources)
        patch_letter_stickers(resources / "feapp.dat")
        patch_native_locale(resources / "feapp.dat")
        patch_webplayer(resources / "webplayer.dat", work_root=resources)

        for archive_name, directory_name in (("feapp.dat", "frontend"), ("webplayer.dat", "webplayer")):
            directory = output / directory_name
            directory.mkdir()
            with zipfile.ZipFile(resources / archive_name) as archive:
                _safe_extract(archive, directory)
        _rewrite_backend_urls(output / "frontend", port, proxy_origin)

    return {"status": "READY", "client_version": CLIENT_VERSION,
            "frontend_members": sum(path.is_file() for path in (output / "frontend").rglob("*"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--proxy-origin", required=True)
    arguments = parser.parse_args()
    print(json.dumps(prepare(arguments.source, arguments.output, arguments.port, arguments.proxy_origin)), flush=True)
