from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError("packaging subprocess failed")


def test_wheel_installs_every_module_needed_to_import_local_server(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=ROOT,
        env=environment,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        packaged_paths = frozenset(archive.namelist())
    assert "private_world_runtime.py" in packaged_paths
    assert "music_caption.py" in packaged_paths
    assert "conversation_memory_identity.py" in packaged_paths
    assert "mem0_embedding_install.py" in packaged_paths
    assert {
        "linli_character/persona_release_v2.json",
        "linli_character/persona_release_provenance_v2.json",
        "contracts/persona_v2.schema.json",
        "contracts/persona_v2_provenance.schema.json",
    } <= packaged_paths
    assert not {
        "linli_character/persona_v2.json",
        "linli_character/provenance_v2.json",
        "linli_character/system_prompt.md",
        "linli_character/persona_config.json",
    } & packaged_paths
    assert not any(
        path.startswith(".private/")
        or path.endswith("linli-im-private-constitution-1.0.zh-CN.md")
        for path in packaged_paths
    )
    environment_dir = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment_dir)
    interpreter = environment_dir / "Scripts" / "python.exe"
    _run(
        [str(interpreter), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=tmp_path,
        env=environment,
    )
    installed_environment = environment.copy()
    installed_environment["OLIVIA_LOCAL_DATA_ROOT"] = str(tmp_path / "data")
    _run(
        [
            str(interpreter),
            "-c",
            "import conversation_memory_identity, conversation_memory_admin, mem0_memory; "
            "import mem0_embedding_install, original_client_companion_mutation_backend; "
            "import original_client_server, music_caption, song_content; import local_server",
        ],
        cwd=tmp_path,
        env=installed_environment,
    )
    _run(
        [
            str(interpreter),
            "-c",
            "from datetime import datetime, timezone; from pathlib import Path; "
            "from persona_assembly import assemble_persona; "
            "from persona_loader import load_persona; "
            "from reply_context import ReplyContext, ReplyMode, TrustedTime; "
            "root = Path(__import__('persona_loader').__file__).resolve().parent; "
            "loaded = load_persona(root / 'linli_character' / 'persona_release_v2.json'); "
            "assert loaded.ready and loaded.snapshot.status == 'READY'; "
            "assembly = assemble_persona(loaded.snapshot, ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime(2026, 8, 25, tzinfo=timezone.utc))), user_input='synthetic normal letter', max_units=10000); "
            "assert assembly.persona_status == 'READY' and 'Persona status is DRAFT' not in assembly.system_content",
        ],
        cwd=tmp_path,
        env=installed_environment,
    )
