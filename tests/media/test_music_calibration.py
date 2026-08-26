from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import music_calibration
from tools.minimax_profile import (
    CURRENT_MINIMAX_PROFILE,
    OFFICIAL_COMFY_MINIMAX_PROFILE,
)
from music_caption import MINIMAX_CAPTION_VERSION, validate_minimax_caption
from song_content import SongSemanticPlan


def test_calibration_provenance_binds_the_new_short_song_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(music_calibration, "_run_id", lambda: "music-provenance-run")

    manifest = music_calibration.create_music_calibration_run(
        tmp_path,
        mode="quick",
        seeds=(101,),
    )
    run_root = tmp_path / "music-provenance-run"
    mapping = json.loads(
        (run_root / "private-mapping.json").read_text(encoding="utf-8")
    )

    assert manifest["caseset_version"] == "p03.music-cases.v2"
    assert (
        manifest["caption_version"]
        == MINIMAX_CAPTION_VERSION
        == "p03.minimax-caption.v2"
    )
    assert manifest["caseset_version"] != "p03.music-cases.v1"
    assert manifest["caption_version"] != "p03.minimax-caption.v1"
    assert mapping["caseset_version"] == manifest["caseset_version"]
    assert mapping["caption_version"] == manifest["caption_version"]


def test_calibration_cases_are_synthetic_typed_and_cover_six_scenarios() -> None:
    cases = music_calibration.calibration_cases()
    assert [case.case_id for case in cases] == [
        "ordinary_reassurance",
        "restrained_loneliness",
        "intimate_daily",
        "conflict_repair",
        "requested_performance",
        "spontaneous_motif",
    ]
    assert all(isinstance(case.plan, SongSemanticPlan) for case in cases)
    assert {case.plan.duration_seconds for case in cases} == {40, 60}
    assert len(music_calibration.calibration_cases("quick")) == 2


def test_create_quick_calibration_run_is_blind_and_worker_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(music_calibration, "_run_id", lambda: "music-synthetic-run")
    manifest = music_calibration.create_music_calibration_run(
        tmp_path,
        mode="quick",
        seeds=(101, 202),
    )

    run_root = tmp_path / "music-synthetic-run"
    assert manifest["blinded"] is True
    assert manifest["job_count"] == 8
    assert all("profile" not in job and "seed" not in job for job in manifest["jobs"])
    assert not any((run_root / job["audio"]).exists() for job in manifest["jobs"])

    batch = json.loads((run_root / "batch.json").read_text(encoding="utf-8"))
    mapping = json.loads((run_root / "private-mapping.json").read_text(encoding="utf-8"))
    assert len(batch["jobs"]) == len(mapping["mapping"]) == 8
    assert mapping["profiles_hidden_until_scoring"] is True

    for item in batch["jobs"]:
        request_path = run_root / item["request_json"]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert set(request) == {
            "max_duration",
            "lyrics",
            "caption",
            "inference_profile",
        }
        assert validate_minimax_caption(request["caption"], request["max_duration"])
        assert "current_letter" not in request
        assert "reply_text" not in request


def test_full_default_run_has_all_case_profile_seed_combinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(music_calibration, "_run_id", lambda: "music-full-run")
    manifest = music_calibration.create_music_calibration_run(tmp_path)
    assert manifest["job_count"] == 6 * 2 * 4

    mapping = json.loads(
        (tmp_path / "music-full-run" / "private-mapping.json").read_text(
            encoding="utf-8"
        )
    )["mapping"]
    base_names = {
        entry["profile"]["name"].split("-v", 1)[0]
        for entry in mapping.values()
    }
    assert base_names == {
        CURRENT_MINIMAX_PROFILE.name,
        OFFICIAL_COMFY_MINIMAX_PROFILE.name,
    }
    assert {entry["profile"]["seed"] for entry in mapping.values()} == {
        200717,
        1247,
        2702,
        202608,
    }


@pytest.mark.parametrize("mode", ["", "unknown", "FULL"])
def test_invalid_calibration_mode_is_rejected(mode: str) -> None:
    with pytest.raises(ValueError, match="MUSIC_CALIBRATION_MODE_INVALID"):
        music_calibration.calibration_cases(mode)


def test_duplicate_or_invalid_seeds_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MUSIC_CALIBRATION_SEEDS_INVALID"):
        music_calibration.create_music_calibration_run(tmp_path, seeds=(1, 1))
    with pytest.raises(ValueError, match="MUSIC_CALIBRATION_SEED_INVALID"):
        music_calibration.create_music_calibration_run(tmp_path, seeds=(True,))


def test_existing_file_cannot_be_used_as_run_root(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="MUSIC_CALIBRATION_ROOT_INVALID"):
        music_calibration.create_music_calibration_run(blocked)
