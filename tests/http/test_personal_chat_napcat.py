from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest


def _shell(root: Path) -> Path:
    shell = root / "personal-chat" / "napcat" / "component" / "v4.18.28" / "NapCat.synthetic.Shell"
    shell.mkdir(parents=True)
    (shell / "napcat.bat").write_text("@echo off\n", encoding="utf-8")
    return shell


def test_napcat_source_is_pinned_to_publisher_release() -> None:
    from runtime.personal_chat import napcat_installer as module

    assert module.NAPCAT_VERSION == "v4.18.28"
    assert module.NAPCAT_URL == (
        "https://github.com/NapNeko/NapCatQQ/releases/download/"
        "v4.18.28/NapCat.Shell.Windows.Node.zip"
    )
    assert module.NAPCAT_SHA256 == "fb64fa3b036ad2df1a5d7c204c482694c20e4b763978c8a4968fd3474c05b4a8"
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
    config_dir = tmp_path / "personal-chat" / "napcat" / "workdir" / "config"
    config = json.loads((config_dir / "onebot11.json").read_text(encoding="utf-8"))
    webui = json.loads((config_dir / "webui.json").read_text(encoding="utf-8"))
    assert not (shell / "config" / "onebot11.json").exists()
    servers = config["network"]["websocketServers"]
    assert len(servers) == 1
    assert servers[0]["enable"] is True
    assert servers[0]["host"] == "127.0.0.1"
    assert servers[0]["port"] == 3001
    assert servers[0]["token"] == token
    assert webui["host"] == "127.0.0.1"
    assert webui["port"] == 6099
    assert isinstance(webui["token"], str) and len(webui["token"]) >= 12
    assert token_file.read_text(encoding="utf-8") == "ciphertext"


def test_napcat_launch_passes_isolated_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer as module

    shell = _shell(tmp_path)
    monkeypatch.setattr(module, "prepare_onebot", lambda _root: (shell, "synthetic-token"))
    seen: dict[str, object] = {}

    class _Process:
        pass

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        return _Process()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.os, "name", "nt", raising=False)
    module.launch_shell(tmp_path)
    assert seen["args"] == ["cmd.exe", "/c", str(shell / "napcat.bat")]
    assert seen["cwd"] == str(shell)
    assert seen["env"]["NAPCAT_WORKDIR"] == str(tmp_path / "personal-chat" / "napcat" / "workdir")


def test_napcat_login_page_opens_only_loopback_webui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.personal_chat import napcat_installer as module

    _shell(tmp_path)
    config = tmp_path / "personal-chat" / "napcat" / "workdir" / "config"
    config.mkdir(parents=True)
    (config / "webui.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": 6099, "token": "a token/with spaces"}),
        encoding="utf-8",
    )
    seen: list[str] = []
    monkeypatch.setattr(module, "webui_available", lambda _root: True)
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


def test_napcat_public_status_exposes_only_sanitized_error(tmp_path: Path) -> None:
    from runtime.personal_chat import napcat_installer as module

    status = module.public_status(tmp_path, {"napcat_state": "FAILED", "napcat_error": "NAPCAT_DOWNLOAD_FAILED"})
    assert status["error"] == "NAPCAT_DOWNLOAD_FAILED"


def test_account_specific_onebot_config_is_required_after_login(tmp_path: Path) -> None:
    from runtime.personal_chat import napcat_installer as module

    _shell(tmp_path)
    config = tmp_path / "personal-chat" / "napcat" / "workdir" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "onebot11.json").write_text("{}", encoding="utf-8")
    assert module.account_config_ready(tmp_path, "123456789") is False
    (config / "onebot11_123456789.json").write_text("{}", encoding="utf-8")
    assert module.account_config_ready(tmp_path, "123456789") is True
