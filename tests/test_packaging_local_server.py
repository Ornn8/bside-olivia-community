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
    assert "runtime/memory/private_world_runtime.py" in packaged_paths
    assert "runtime/memory/private_world_delivery.py" in packaged_paths
    assert "runtime/memory/private_world_projection.py" in packaged_paths
    assert "runtime/memory/conversation_memory_identity.py" in packaged_paths
    assert "runtime/memory/conversation_memory_delivery.py" in packaged_paths
    assert "runtime/memory/conversation_memory_outbox.py" in packaged_paths
    assert "runtime/memory/companion_memory_context.py" in packaged_paths
    assert "runtime/memory/conversation_memory_admin.py" in packaged_paths
    assert "runtime/memory/conversation_memory_port.py" in packaged_paths
    assert "runtime/memory/conversation_memory_runtime.py" in packaged_paths
    assert "runtime/memory/local_memory.py" in packaged_paths
    assert "runtime/memory/mem0_memory.py" in packaged_paths
    assert "runtime/memory/memory.py" in packaged_paths
    assert "runtime/memory/memory_port.py" in packaged_paths
    assert "runtime/memory/memory_prompt.py" in packaged_paths
    assert "conversation_memory_delivery.py" in packaged_paths
    assert "conversation_memory_outbox.py" in packaged_paths
    assert "conversation_memory_runtime.py" in packaged_paths
    assert "runtime/media/music_caption.py" in packaged_paths
    assert "runtime/media/latentsync_reply.py" in packaged_paths
    assert "runtime/media/song_content.py" in packaged_paths
    assert "runtime/media/music_reply.py" in packaged_paths
    assert "runtime/media/voice_direction.py" in packaged_paths
    assert "runtime/reply/reply_pipeline.py" in packaged_paths
    assert "runtime/reply/reply_reviewer.py" in packaged_paths
    assert "runtime/reply/reply_context.py" in packaged_paths
    assert "runtime/reply/reply_model_quality.py" in packaged_paths
    assert "runtime/reply/reply_orchestrator.py" in packaged_paths
    assert "runtime/persona/persona_loader.py" in packaged_paths
    assert "runtime/persona/persona_assembly.py" in packaged_paths
    assert "runtime/persona/persona_provider.py" in packaged_paths
    assert "runtime/private_world/ledger.py" in packaged_paths
    assert "reply_pipeline.py" in packaged_paths
    assert "reply_reviewer.py" in packaged_paths
    assert "reply_context.py" in packaged_paths
    assert "persona_loader.py" in packaged_paths
    assert "persona_assembly.py" in packaged_paths
    assert "persona_provider.py" in packaged_paths
    assert "music_caption.py" not in packaged_paths
    assert "runtime/reply/reply_delivery.py" in packaged_paths
    assert "runtime/reply/reply_media.py" in packaged_paths
    assert "reply_delivery.py" in packaged_paths
    assert "reply_media.py" in packaged_paths
    assert not {
        "conversation_memory_identity.py",
        "private_world_delivery.py",
        "private_world_projection.py",
        "private_world_runtime.py",
    } & packaged_paths
    assert "mem0_capability_install.py" in packaged_paths
    assert "mem0_embedding_install.py" in packaged_paths
    assert "original_client_capability_api.py" in packaged_paths
    assert "original_client_setup_api.py" in packaged_paths
    assert "original_client_update_api.py" in packaged_paths
    assert "patch_webplayer.py" in packaged_paths
    assert {
        "linli_character/persona_release_v2.json",
        "linli_character/persona_release_provenance_v2.json",
        "contracts/persona_v2.schema.json",
        "contracts/persona_v2_provenance.schema.json",
        "contracts/third_party_manifest.example.json",
        "contracts/third_party_manifest.schema.json",
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
            "import conversation_memory_admin, mem0_memory; "
            "import mem0_embedding_install, original_client_companion_mutation_backend; "
            "import conversation_memory_delivery, conversation_memory_outbox; "
            "import runtime.memory.conversation_memory_delivery as delivery_impl; "
            "import runtime.memory.conversation_memory_outbox as outbox_impl; "
            "assert conversation_memory_delivery is delivery_impl; "
            "assert conversation_memory_outbox is outbox_impl; "
            "import companion_memory_context, conversation_memory_admin, conversation_memory_port, conversation_memory_runtime; "
            "import local_memory, mem0_memory, memory, memory_port, memory_prompt; "
            "import runtime.memory.companion_memory_context as companion_memory_context_impl; "
            "import runtime.memory.conversation_memory_admin as conversation_memory_admin_impl; "
            "import runtime.memory.conversation_memory_port as conversation_memory_port_impl; "
            "import runtime.memory.conversation_memory_runtime as conversation_memory_runtime_impl; "
            "import runtime.memory.local_memory as local_memory_impl; "
            "import runtime.memory.mem0_memory as mem0_memory_impl; "
            "import runtime.memory.memory as memory_impl; "
            "import runtime.memory.memory_port as memory_port_impl; "
            "import runtime.memory.memory_prompt as memory_prompt_impl; "
            "assert companion_memory_context is companion_memory_context_impl; "
            "assert conversation_memory_admin is conversation_memory_admin_impl; "
            "assert conversation_memory_port is conversation_memory_port_impl; "
            "assert conversation_memory_runtime is conversation_memory_runtime_impl; "
            "assert local_memory is local_memory_impl; "
            "assert mem0_memory is mem0_memory_impl; "
            "assert memory is memory_impl; "
            "assert memory_port is memory_port_impl; "
            "assert memory_prompt is memory_prompt_impl; "
            "import importlib.util; "
            "assert importlib.util.find_spec('conversation_memory_identity') is None; "
            "assert importlib.util.find_spec('private_world_delivery') is None; "
            "assert importlib.util.find_spec('private_world_projection') is None; "
            "assert importlib.util.find_spec('private_world_runtime') is None; "
            "assert importlib.util.find_spec('music_caption') is None; "
            "import runtime.memory.conversation_memory_identity; "
            "import runtime.memory.private_world_delivery; "
            "import runtime.memory.private_world_projection; "
            "import runtime.memory.private_world_runtime; "
            "import reply_context, reply_model_quality, reply_orchestrator, reply_pipeline, reply_reviewer; "
            "import runtime.reply.reply_context; "
            "import runtime.reply.reply_model_quality, runtime.reply.reply_orchestrator, runtime.reply.reply_pipeline, runtime.reply.reply_reviewer; "
            "assert reply_context is runtime.reply.reply_context; "
            "assert reply_model_quality is runtime.reply.reply_model_quality; "
            "assert reply_orchestrator is runtime.reply.reply_orchestrator; "
            "assert reply_pipeline is runtime.reply.reply_pipeline; "
            "assert reply_reviewer is runtime.reply.reply_reviewer; "
            "import persona_loader, persona_assembly, persona_provider; "
            "import runtime.persona.persona_loader as persona_loader_impl; "
            "import runtime.persona.persona_assembly as persona_assembly_impl; "
            "import runtime.persona.persona_provider as persona_provider_impl; "
            "assert persona_loader is persona_loader_impl; "
            "assert persona_assembly is persona_assembly_impl; "
            "assert persona_provider is persona_provider_impl; "
            "import private_world_port, private_world_commands, private_world_ledger; "
            "import runtime.private_world.port, runtime.private_world.commands, runtime.private_world.ledger; "
            "assert private_world_port is runtime.private_world.port; "
            "assert private_world_commands is runtime.private_world.commands; "
            "assert private_world_ledger is runtime.private_world.ledger; "
            "import music_reply, original_client_server, runtime.media.music_caption, song_content, voice_direction; "
            "import latentsync_reply; "
            "from runtime.media import latentsync_reply as canonical_latentsync_reply; "
            "from runtime.media import music_reply as canonical_music_reply; "
            "from runtime.media import song_content as canonical_song_content; "
            "from runtime.media import voice_direction as canonical_voice_direction; "
            "assert latentsync_reply is canonical_latentsync_reply; "
            "assert music_reply is canonical_music_reply; "
            "assert song_content is canonical_song_content; "
            "assert voice_direction is canonical_voice_direction; "
            "import reply_delivery, reply_media; "
            "from runtime.reply import reply_delivery as canonical_delivery; "
            "from runtime.reply import reply_media as canonical_media; "
            "assert reply_delivery is canonical_delivery; "
            "assert reply_media is canonical_media; "
            "import local_server",
        ],
        cwd=tmp_path,
        env=installed_environment,
    )
    _run(
        [
            str(interpreter),
            "-c",
            "from pathlib import Path; from local_memory import load_memory_config; "
            "from mem0_memory import load_mem0_config; from memory import Memory; "
            "root = Path(__import__('memory').__file__).resolve().parents[2]; "
            "assert Path(Memory().path) == root / 'memory_store.json'; "
            "assert load_memory_config(environ={}).data_root == root / '.olivia_data' / 'memory'; "
            "assert load_mem0_config(environ={}).data_root == root / '.olivia_data' / 'memory' / 'mem0'",
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
            "from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime; "
            "root = Path(__import__('persona_loader').__file__).resolve().parents[2]; "
            "loaded = load_persona(root / 'linli_character' / 'persona_release_v2.json'); "
            "assert loaded.ready and loaded.snapshot.status == 'READY'; "
            "assembly = assemble_persona(loaded.snapshot, ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime(2026, 8, 25, tzinfo=timezone.utc))), user_input='synthetic normal letter', max_units=10000); "
            "assert assembly.persona_status == 'READY' and 'Persona status is DRAFT' not in assembly.system_content",
        ],
        cwd=tmp_path,
        env=installed_environment,
    )
