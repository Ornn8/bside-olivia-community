from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest


def _shell(root: Path) -> Path:
    shell = root / "personal-chat" / "napcat" / "bootstrap" / "NapCat.synthetic.Shell"
    shell.mkdir(parents=True)
    (shell / "napcat.bat").write_text("@echo off\n", encoding="utf-8")
    return shell


def test_napcat_source_is_pinned_to_publisher_release() -> None:
    from runtime.personal_chat import napcat_installer as module

    assert module.NAPCAT_VERSION == "v4.18.28"
    assert module.NAPCAT_URL == (
        "https://github.com/NapNeko/NapCatQQ/releases/download/"
        "v4.18.28/NapCat.Shell.Windows.OneKey.zip"
    )
    assert module.NAPCAT_SHA256 == "fa365537039e9ec29730166f3f624eb147074be18be64d1981a03f35ecb2a2af"
    assert module._allowed_download_url(module.NAPCAT_URL) is True
    assert module._allowed_download_url("https://example.invalid/NapCat.zip") is False


def test_napcat_archive_rejects_path_traversal(tmp_path: Path) -> None:
    from runtime.personal_chat.napcat_installer import NapCatSetupError, _safe_extract

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.exe", b"bad")
    destination = tmp_path / "out"
    with pytest.raises(NapCatSetupError, match="NAPCAT_ARCHIVE_INVALID"):
        _safe_extract(archive, destination)
    assert not (tmp_path / "escape.exe").exists()


def test_managed_onebot_config_is_loopback_only_and_uses_dpapi_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import original_client_setup_api
    from runtime.personal_chat import napcat_installer as module

    shell = _shell(tmp_path)
    monkeypatch.setattr(original_client_setup_api, "_dpapi_protect", lambda _value: "ciphertext")
    monkeypatch.setattr(
        original_client_setup_api,
        "_dpapi_unprotect",
        lambda _value: json.dumps({"token": "synthetic-managed-token-1234567890"}),
    )

    token_file = tmp_path / "personal-chat" / "napcat" / "onebot-token.dpapi"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("ciphertext", encoding="utf-8")

    found, token = module.prepare_onebot(tmp_path)
    assert found == shell
    assert token == "synthetic-managed-token-1234567890"
    config = json.loads((shell / "config" / "onebot11.json").read_text(encoding="utf-8"))
    servers = config["network"]["websocketServers"]
    assert len(servers) == 1
    assert servers[0]["enable"] is True
    assert servers[0]["host"] == "127.0.0.1"
    assert servers[0]["port"] == 3001
    assert servers[0]["token"] == token
    assert token_file.read_text(encoding="utf-8") == "ciphertext"


def test_napcat_login_page_opens_only_loopback_webui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer as module

    shell = _shell(tmp_path)
    config = shell / "config"
    config.mkdir(exist_ok=True)
    (config / "webui.json").write_text(
        json.dumps({"port": 6099, "token": "a token/with spaces"}),
        encoding="utf-8",
    )
    seen: list[str] = []
    monkeypatch.setattr(module.webbrowser, "open", lambda url, new=0: seen.append(url) or True)

    assert module.open_login_page(tmp_path) is True
    assert seen == ["http://127.0.0.1:6099/webui/?token=a%20token%2Fwith%20spaces"]


def test_napcat_public_status_discovers_completed_install(tmp_path: Path) -> None:
    from runtime.personal_chat import napcat_installer as module

    assert module.public_status(tmp_path, {"napcat_state": "IDLE"})["installed"] is False
    _shell(tmp_path)
    status = module.public_status(tmp_path, {"napcat_state": "INSTALLER_OPENED"})
    assert status["installed"] is True
    assert status["state"] == "READY"
    assert status["managed"] is True
