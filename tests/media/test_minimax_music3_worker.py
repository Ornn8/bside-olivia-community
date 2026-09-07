from __future__ import annotations

from pathlib import Path

import pytest

from music_reply import MiniMaxMusic3Worker
from tools.minimax_profile import (
    CURRENT_MINIMAX_PROFILE,
    MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION,
    MiniMaxInferenceProfile,
    MiniMaxProfileError,
    OFFICIAL_COMFY_MINIMAX_PROFILE,
    minimax_profile_from_mapping,
)
from runtime.media.music_caption import render_minimax_caption
from runtime.media.song_content import (
    PianoTexture,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
)
from tools import minimax_music3_worker as worker


def _lyrics(duration: int) -> str:
    per_verse = 6 if duration == 90 else 8
    first = [f"第一段第{index}句轻轻落下" for index in range(1, per_verse + 1)]
    second = [f"第二段第{index}句慢慢收好" for index in range(1, per_verse + 1)]
    return "\n".join(
        (
            "[Intro]",
            "[Verse]",
            *first,
            "[Interlude]",
            "[Verse]",
            *second,
            "[Outro]",
        )
    )


def _short_lyrics(duration: int) -> str:
    verse_count, chorus_count = {40: (6, 6), 60: (8, 8), 110: (12, 12)}[duration]
    return "\n".join(
        (
            "[Intro]",
            "[Verse]",
            *(f"主歌第{index}句轻轻落下" for index in range(1, verse_count + 1)),
            "[Chorus]",
            *(f"副歌第{index}句温柔收好" for index in range(1, chorus_count + 1)),
            "[Outro]",
        )
    )


def _request(
    duration: int = 40,
    profile: MiniMaxInferenceProfile | None = None,
) -> dict[str, object]:
    plan = SongSemanticPlan(
        emotion_arc=SongEmotionArc.GENTLE_REASSURANCE,
        piano_texture=PianoTexture.TRANSPARENT_BROKEN_CHORDS,
        vocal_delivery=VocalDelivery.CLEAR_LEGATO,
        dynamic_arc=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        lyrics=_short_lyrics(duration),
        duration_seconds=duration,
    )
    request: dict[str, object] = {
        "max_duration": duration,
        "lyrics": plan.lyrics,
        "caption": render_minimax_caption(plan),
    }
    if profile is not None:
        request["inference_profile"] = profile.to_dict()
    return request


def test_current_and_official_profiles_are_explicit_a_b_candidates() -> None:
    assert CURRENT_MINIMAX_PROFILE.to_dict() == {
        "schema_version": MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION,
        "name": "current-1.5",
        "seed": 200717,
        "text_cfg_scale": 1.5,
        "top_k": 50,
        "sampler_cfg_scale": 1.5,
        "steps": 30,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1.0,
    }
    assert OFFICIAL_COMFY_MINIMAX_PROFILE.text_cfg_scale == 1.7
    assert OFFICIAL_COMFY_MINIMAX_PROFILE.sampler_cfg_scale == 1.7
    assert minimax_profile_from_mapping(None) is CURRENT_MINIMAX_PROFILE
    assert minimax_profile_from_mapping({}) is CURRENT_MINIMAX_PROFILE


def test_worker_graph_uses_current_profile_and_zeroed_negative_conditioning() -> None:
    graph = worker._graph(_request(), filename_prefix="audio/test")

    assert graph["4"]["inputs"]["seed"] == 200717
    assert graph["4"]["inputs"]["cfg_scale"] == 1.5
    assert graph["4"]["inputs"]["top_k"] == 50
    assert graph["7"]["inputs"]["steps"] == 30
    assert graph["7"]["inputs"]["cfg"] == 1.5
    assert graph["7"]["inputs"]["sampler_name"] == "euler"
    assert graph["7"]["inputs"]["scheduler"] == "simple"
    assert graph["7"]["inputs"]["denoise"] == 1.0
    assert graph["5"] == {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": ["4", 0]},
    }
    assert graph["7"]["inputs"]["negative"] == ["5", 0]


@pytest.mark.parametrize("duration", [40, 60, 110])
def test_worker_passes_complete_duration_and_original_lyrics_to_comfy(duration):
    request = _request(duration)
    graph = worker._graph(request, filename_prefix="audio/duration")
    assert graph["4"]["inputs"]["max_duration"] == duration
    assert graph["4"]["inputs"]["lyrics"] == request["lyrics"]


def test_worker_rejects_short_lyrics_for_110_seconds_instead_of_repeating_them():
    request = _request(110)
    request["lyrics"] = _short_lyrics(40)
    with pytest.raises(RuntimeError, match="MINIMAX_MUSIC3_LYRICS_INVALID"):
        worker._graph(request, filename_prefix="audio/short")


def test_worker_graph_accepts_official_comfy_profile_without_making_it_default() -> None:
    graph = worker._graph(
        _request(profile=OFFICIAL_COMFY_MINIMAX_PROFILE),
        filename_prefix="audio/official",
    )

    assert graph["4"]["inputs"]["cfg_scale"] == 1.7
    assert graph["7"]["inputs"]["cfg"] == 1.7
    assert CURRENT_MINIMAX_PROFILE.text_cfg_scale == 1.5


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("lyrics", "", "MINIMAX_MUSIC3_LYRICS_REQUIRED"),
        ("caption", "", "MINIMAX_MUSIC3_CAPTION_REQUIRED"),
        ("max_duration", 91, "MINIMAX_MUSIC3_DURATION_INVALID"),
        ("max_duration", True, "MINIMAX_MUSIC3_DURATION_INVALID"),
    ],
)
def test_worker_has_no_fallback_for_missing_or_invalid_inputs(
    field: str,
    value: object,
    error_code: str,
) -> None:
    request = _request()
    request[field] = value
    with pytest.raises(RuntimeError, match=error_code):
        worker._graph(request, filename_prefix="audio/invalid")


@pytest.mark.parametrize(
    ("suffix", "error_code"),
    [
        ("\nNo drums.", "MINIMAX_MUSIC3_CAPTION_NEGATIVE_SYNTAX"),
        ("\nSoft strings enter.", "MINIMAX_MUSIC3_CAPTION_DISALLOWED_TERM"),
        ("\nR&B groove.", "MINIMAX_MUSIC3_CAPTION_DISALLOWED_TERM"),
    ],
)
def test_worker_rejects_caption_drift_before_starting_comfy(
    suffix: str,
    error_code: str,
) -> None:
    request = _request()
    request["caption"] = str(request["caption"]) + suffix
    with pytest.raises(RuntimeError, match=error_code):
        worker._graph(request, filename_prefix="audio/drift")


def test_worker_rejects_legacy_short_lyrics_instead_of_filling_them() -> None:
    request = _request()
    request["lyrics"] = "[Verse]\n只有一句"
    with pytest.raises(RuntimeError, match="MINIMAX_MUSIC3_LYRICS_INVALID"):
        worker._graph(request, filename_prefix="audio/legacy")


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("seed", True, "MINIMAX_PROFILE_SEED_INVALID"),
        ("text_cfg_scale", 0.0, "MINIMAX_PROFILE_TEXT_CFG_SCALE_INVALID"),
        ("top_k", 0, "MINIMAX_PROFILE_TOP_K_INVALID"),
        ("sampler_cfg_scale", 5.1, "MINIMAX_PROFILE_SAMPLER_CFG_SCALE_INVALID"),
        ("steps", 0, "MINIMAX_PROFILE_STEPS_INVALID"),
        ("sampler_name", "dpmpp_2m", "MINIMAX_PROFILE_SAMPLER_INVALID"),
        ("scheduler", "karras", "MINIMAX_PROFILE_SCHEDULER_INVALID"),
        ("denoise", 0.0, "MINIMAX_PROFILE_DENOISE_INVALID"),
    ],
)
def test_profile_validation_rejects_unregistered_sampling_space(
    field: str,
    value: object,
    error_code: str,
) -> None:
    profile = CURRENT_MINIMAX_PROFILE.to_dict()
    profile[field] = value
    with pytest.raises(MiniMaxProfileError, match=error_code):
        minimax_profile_from_mapping(profile)


def test_worker_source_contains_no_music_fallback() -> None:
    source = Path(worker.__file__).read_text(encoding="utf-8").casefold()
    assert "fallback_caption" not in source
    assert "fallback_lyrics" not in source
    assert "soft strings gradually enter" not in source
    assert "subtle cello" not in source
    assert "sparse percussion" not in source


def test_comfy_command_bootstraps_embedded_python_import_path(tmp_path: Path) -> None:
    comfy_root = tmp_path / "comfy"
    command = worker._comfy_command(
        comfy_root=comfy_root,
        port=8899,
        cache_lru=12,
        reserve_vram=0.5,
        generated_root=tmp_path / "output",
        temp_root=tmp_path / "temp",
        vram_mode="dynamic",
    )

    assert command[1:3] == ["-c", worker._COMFY_BOOTSTRAP]
    assert command[3:5] == [str(comfy_root), str(comfy_root / "main.py")]
    assert "sys.path.insert(0, root)" in worker._COMFY_BOOTSTRAP


def test_ar_release_preserves_conditioning_and_precedes_dit() -> None:
    from types import SimpleNamespace

    events = []
    result = object()
    patcher = object()
    clip = SimpleNamespace(patcher=patcher)

    class Encoder:
        @classmethod
        def execute(cls, clip, caption, lyrics, seed, max_duration, cfg_scale, top_k):
            events.append(("AR", caption, lyrics, seed, max_duration, cfg_scale, top_k))
            return result

    namespace = {}
    exec(worker._AR_RELEASE_ADAPTER, namespace)
    namespace["_wrap_ar_release"](
        Encoder, lambda value: events.append(("unload", value))
    )
    actual = Encoder.execute(clip, "caption", "lyrics", 9, 40, 2.0, 50)
    events.append(("DiT", actual))
    assert actual is result
    assert events == [("AR", "caption", "lyrics", 9, 40, 2.0, 50),
                      ("unload", patcher), ("DiT", result)]


@pytest.mark.parametrize("failure", ["signature", "unload_api", "patcher"])
def test_ar_release_rejects_incompatible_api(failure: str) -> None:
    from types import SimpleNamespace

    class Encoder:
        @classmethod
        def execute(cls, clip, caption, lyrics, seed, max_duration, cfg_scale, top_k):
            return object()

    if failure == "signature":
        Encoder.execute = classmethod(lambda cls, clip: None)
    namespace = {}
    exec(worker._AR_RELEASE_ADAPTER, namespace)
    with pytest.raises(RuntimeError, match="^MINIMAX_AR_RELEASE_API_INCOMPATIBLE$"):
        namespace["_wrap_ar_release"](Encoder, None if failure == "unload_api" else lambda _: None)
        Encoder.execute(SimpleNamespace(), "caption", "lyrics", 9, 40, 2.0, 50)


def test_ar_release_bootstrap_wraps_dynamic_builtin_without_changing_files(tmp_path: Path) -> None:
    import subprocess
    import sys

    comfy = tmp_path / "comfy"
    comfy.mkdir()
    (comfy / "__init__.py").write_text("")
    (comfy / "model_management.py").write_text(
        "events = []\ndef unload_model_and_clones(patcher):\n    events.append(('unload', patcher))\n"
    )
    extras = tmp_path / "comfy_extras"
    extras.mkdir()
    node = extras / "nodes_minimax_music.py"
    node.write_text(
        "from comfy.model_management import events\n"
        "conditioning = object()\n"
        "class MiniMaxMusic3TextEncode:\n"
        "    @classmethod\n"
        "    def execute(cls, clip, caption, lyrics, seed, max_duration, cfg_scale, top_k):\n"
        "        events.append(('AR', clip.patcher))\n"
        "        return conditioning\n"
    )
    original = node.read_bytes()
    entry = tmp_path / "main.py"
    entry.write_text(
        "import importlib.util\nfrom types import SimpleNamespace\n"
        "from comfy.model_management import events\n"
        "from importlib.machinery import SourceFileLoader\n"
        f"spec = importlib.util.spec_from_file_location('builtin_node', {str(node)!r})\n"
        "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
        "result = module.MiniMaxMusic3TextEncode.execute(SimpleNamespace(patcher=7), 'c', 'l', 9, 40, 2.0, 50)\n"
        "events.append(('DiT', 7))\nassert result is module.conditioning\n"
        "assert events == [('AR', 7), ('unload', 7), ('DiT', 7)]\n"
        "assert SourceFileLoader.exec_module.__name__ == 'exec_module'\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", worker._COMFY_BOOTSTRAP, str(tmp_path), str(entry)],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert node.read_bytes() == original


def test_ar_failure_does_not_unload_or_hide_original_error() -> None:
    from types import SimpleNamespace

    events = []
    class Encoder:
        @classmethod
        def execute(cls, clip, caption, lyrics, seed, max_duration, cfg_scale, top_k):
            raise ValueError("synthetic AR failure")
    namespace = {}
    exec(worker._AR_RELEASE_ADAPTER, namespace)
    namespace["_wrap_ar_release"](Encoder, lambda _: events.append("unload"))
    with pytest.raises(ValueError, match="synthetic AR failure"):
        Encoder.execute(SimpleNamespace(patcher=object()), "c", "l", 9, 40, 2.0, 50)
    assert events == []


def test_ar_unload_failure_has_stable_error_without_input_logging(capsys) -> None:
    from types import SimpleNamespace

    class Encoder:
        @classmethod
        def execute(cls, clip, caption, lyrics, seed, max_duration, cfg_scale, top_k):
            return object()
    def broken_unload(patcher):
        raise ValueError("private synthetic detail")
    namespace = {}
    exec(worker._AR_RELEASE_ADAPTER, namespace)
    namespace["_wrap_ar_release"](Encoder, broken_unload)
    with pytest.raises(RuntimeError, match="^MINIMAX_AR_RELEASE_FAILED$") as error:
        Encoder.execute(SimpleNamespace(patcher=object()), "private caption", "private lyric", 9, 40, 2.0, 50)
    assert error.value.__suppress_context__ is True
    assert capsys.readouterr() == ("", "")


def test_118_second_generation_timeouts_cover_both_model_phases() -> None:
    adapter = MiniMaxMusic3Worker(
        python_path=Path("python.exe"),
        worker_path=Path("worker.py"),
        comfy_root=Path("comfy"),
    )

    assert worker._INFERENCE_TIMEOUT_SECONDS >= 7200.0
    assert adapter.timeout_seconds >= worker._INFERENCE_TIMEOUT_SECONDS + 300.0


def test_history_poll_retries_transient_timeout_while_server_is_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = tmp_path / "generated" / "audio"
    generated.mkdir(parents=True)
    source = generated / "song.flac"
    source.write_bytes(b"audio")
    calls = 0

    def request_json(_url: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("busy GPU kernel")
        return {
            "prompt": {
                "outputs": {"9": {"audio": [{"filename": source.name, "subfolder": "audio"}]}}
            }
        }

    class AliveProcess:
        @staticmethod
        def poll() -> None:
            return None

    monkeypatch.setattr(worker, "_request_json", request_json)
    monkeypatch.setattr(worker, "_gpu_sample", lambda: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)
    output = tmp_path / "song.flac"

    worker._copy_result(
        base_url="http://127.0.0.1:1",
        prompt_id="prompt",
        generated_root=tmp_path / "generated",
        output=output,
        gpu_samples=[],
        server_process=AliveProcess(),
    )

    assert calls == 2
    assert output.read_bytes() == b"audio"
