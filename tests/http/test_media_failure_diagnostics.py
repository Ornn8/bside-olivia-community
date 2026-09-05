import ast
import asyncio
import json
from pathlib import Path
import re
from types import MappingProxyType, SimpleNamespace

import pytest

from music_reply import _persist_provider_failure


@pytest.mark.parametrize("failure_stage", ["prepare", "voice_plan", "render"])
@pytest.mark.parametrize("message", ["VOICE_PLAN_NOT_READY", "private letter text and key at C:/private"])
def test_media_failure_logs_only_stage_type_and_safe_code(tmp_path, failure_stage, message):
    source = Path(__file__).resolve().parents[2] / "local_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_record_media_job_failure", "_render_media_job"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == names
    asset = tmp_path / "asset"
    asset.write_bytes(b"fixture")
    letter = {"letter_id": "fixture", "music_duration_seconds": 60}
    def fail():
        cause_message = "SONG_PLAN_SCHEMA_INVALID" if message == "VOICE_PLAN_NOT_READY" else "private cause"
        raise ValueError(message) from TypeError(cause_message)
    async def plan(*args):
        if failure_stage == "voice_plan":
            fail()
        return object()
    def render(*args, **kwargs):
        if failure_stage == "render":
            fail()
        return {}
    env = {"OLIVIA_LOCAL_DATA_ROOT": str(tmp_path)}
    namespace = {
        "Mapping": dict, "Path": Path, "json": json, "_re": re,
        "_persist_provider_failure": _persist_provider_failure,
        "asyncio": asyncio, "store": SimpleNamespace(letters=[letter]),
        "media_semaphore": asyncio.Semaphore(1), "_persist_media_state": lambda: None,
        "MappingProxyType": MappingProxyType, "_os": SimpleNamespace(environ=env),
        "_local_data_root": lambda environment: tmp_path,
        "require_breeze_hardware": fail if failure_stage == "prepare" else lambda: None,
        "configured_media_path": lambda *args: asset,
        "ReplyMode": SimpleNamespace(SPOKEN_VIDEO=SimpleNamespace(value="spoken"), MUSICAL_VIDEO=SimpleNamespace(value="music")),
        "_music_voice_plan_for_letter": plan, "_current_music_performance": lambda env: asset,
        "render_musical_reply": render, "letters_adapter": SimpleNamespace(gateway=None),
        "ReplyMediaError": RuntimeError, "MusicReplyError": RuntimeError,
        "VoiceDirectionError": RuntimeError, "GatewayError": RuntimeError,
        "contract": SimpleNamespace(letter_detail_media_error_metadata=lambda code: None),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    asyncio.run(namespace["_render_media_job"]("fixture", "private input", "private reply", "music"))
    raw = (tmp_path / "logs/media-provider.jsonl").read_text()
    record = json.loads(raw)
    details = json.loads(record["diagnostic"])
    assert details["stage"] == failure_stage
    assert details["exception_type"] == "ValueError"
    assert details.get("candidate_code") == (message if message == "VOICE_PLAN_NOT_READY" else None)
    assert details["cause_exception_type"] == "TypeError"
    assert details.get("cause_candidate_code") == ("SONG_PLAN_SCHEMA_INVALID" if message == "VOICE_PLAN_NOT_READY" else None)
    assert "private" not in raw
    assert letter["media_error_code"] == "MEDIA_PROVIDER_UNAVAILABLE"
