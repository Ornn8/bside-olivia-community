from __future__ import annotations

from pathlib import Path

import pytest

from installer import start_local


def test_start_local_configures_private_world_under_install_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Olivia Local"
    backend = root / "local_backend"
    backend.mkdir(parents=True)
    (backend / "local_server.py").write_text("# synthetic", encoding="utf-8")
    (backend / "original_client_server.py").write_text(
        "# synthetic",
        encoding="utf-8",
    )
    client = root / "app" / "synthetic" / "Olivia.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"synthetic")
    observed: dict[str, object] = {}

    monkeypatch.setattr(start_local, "_active_backend", lambda: backend)
    health = iter(("UNAVAILABLE", "READY", "READY", "READY"))
    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(
        start_local,
        "_server_backend_id",
        lambda _port: start_local._backend_id(backend, root.resolve()),
    )
    monkeypatch.setattr(start_local, "_load_dpapi_key", lambda _path: "")
    monkeypatch.setattr(start_local, "_client_executable", lambda _root: client)
    monkeypatch.setattr(
        start_local,
        "_repair_client_frontend",
        lambda _root, _port: "ALREADY_PATCHED",
    )

    class Process:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            return 0

    monkeypatch.setattr(start_local.subprocess, "Popen", lambda *_args, **_kwargs: Process())

    def fake_call(command, *, cwd, env):
        observed.update({"command": command, "cwd": cwd, "env": env})
        return 0

    monkeypatch.setattr(start_local.subprocess, "call", fake_call)

    assert start_local.main(["--install-root", str(root)]) == 0

    environment = observed["env"]
    data_root = root.resolve() / "data"
    assert environment["OLIVIA_LOCAL_DATA_ROOT"] == str(data_root)
    assert environment["OLIVIA_PRIVATE_WORLD_ENABLED"] == "1"
    assert environment["OLIVIA_PRIVATE_WORLD_DB"] == str(
        data_root / "private_world" / "private_world.sqlite3"
    )
    assert Path(environment["OLIVIA_PRIVATE_WORLD_DB"]).is_absolute()
