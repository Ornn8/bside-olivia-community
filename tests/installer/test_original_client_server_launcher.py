from __future__ import annotations

import json
from pathlib import Path

import pytest

import installer.start_local as start_local


def _installation(tmp_path: Path, *, with_entrypoint: bool = True) -> Path:
    root = tmp_path / "installed"
    backend = root / "local_backend"
    (backend / "installer").mkdir(parents=True)
    (backend / "local_server.py").write_text("# fixture", encoding="utf-8")
    if with_entrypoint:
        (backend / "original_client_server.py").write_text(
            "# fixture",
            encoding="utf-8",
        )
    (backend / "installer" / "full-patch-manifest.json").write_text(
        json.dumps({"client_version": "0.0.9.615"}),
        encoding="utf-8",
    )
    client = root / "app" / "0.0.9.615" / "Olivia.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"fixture")
    return root


def test_launcher_starts_combined_server_before_original_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _installation(tmp_path)
    health = iter(("UNAVAILABLE", "READY", "READY"))
    commands: list[list[str]] = []
    client_commands: list[list[str]] = []

    class Process:
        @staticmethod
        def poll():
            return None

    def popen(command, **_kwargs):
        commands.append([str(value) for value in command])
        return Process()

    def call(command, **_kwargs):
        client_commands.append([str(value) for value in command])
        return 0

    monkeypatch.setattr(start_local, "_health", lambda _port: next(health))
    monkeypatch.setattr(
        start_local,
        "_backend_executable",
        lambda: Path("pythonw-fixture.exe"),
    )
    monkeypatch.setattr(start_local.subprocess, "Popen", popen)
    monkeypatch.setattr(start_local.subprocess, "call", call)

    result = start_local.main(["--install-root", str(root), "--port", "8899"])

    assert result == 0
    assert len(commands) == 1
    backend_command = commands[0]
    assert backend_command[:2] == ["pythonw-fixture.exe", "-c"]
    assert "sys.path.insert(0, backend)" in backend_command[2]
    assert backend_command[3:] == [
        str(root / "local_backend"),
        str(root / "local_backend" / "original_client_server.py"),
    ]
    assert client_commands[0][0].endswith("Olivia.exe")
    assert not backend_command[-1].endswith("local_server.py")


def test_launcher_refuses_payload_without_combined_entrypoint(
    tmp_path: Path,
    capsys,
) -> None:
    root = _installation(tmp_path, with_entrypoint=False)

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PATCH_PAYLOAD_INCOMPLETE"


def test_launcher_refuses_unrelated_http_health_payload(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = _installation(tmp_path)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        start_local.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "launcher must not start a backend when the port serves an unrelated JSON payload"
        ),
    )

    assert start_local.main(["--install-root", str(root)]) == 2
    assert capsys.readouterr().out.strip() == "PORT_CONFLICT"


def test_health_accepts_the_versioned_core_contract(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "schema_version": 1,
                        "contract_version": "b02.v1",
                        "profile": "core",
                        "status": "HEALTHY",
                        "required_checks": {
                            "core.health": "available",
                            "core.session": "available",
                            "letters.read": "available",
                            "music.catalog": "available",
                        },
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(start_local, "urlopen", lambda *_args, **_kwargs: Response())

    assert start_local._health(8899) == "READY"
