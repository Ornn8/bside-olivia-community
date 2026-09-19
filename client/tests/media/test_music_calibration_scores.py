from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import music_calibration
from tools.music_calibration_scores import (
    MusicCalibrationScore,
    MusicCalibrationScoreError,
    music_calibration_progress,
    reveal_music_calibration_results,
    submit_music_calibration_score,
)


def _score(
    blind_id: str,
    *,
    good: bool = True,
    hard_fail: bool = False,
) -> MusicCalibrationScore:
    return MusicCalibrationScore(
        blind_id=blind_id,
        piano_carries_arrangement=2 if good else 1,
        extra_instruments_severity=0 if good else 2,
        rnb_soul_groove_severity=0 if good else 2,
        vocal_ornament_severity=0 if good else 2,
        mandarin_clarity=2 if good else 1,
        restrained_lyricism=2 if good else 1,
        emotion_match=2 if good else 1,
        lyrics_complete_and_singable=2 if good else 1,
        ending_complete=2 if good else 1,
        lyrics_incomplete=hard_fail,
        notes="synthetic blind score",
    )


def _prepared_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(music_calibration, "_run_id", lambda: "music-score-run")
    manifest = music_calibration.create_music_calibration_run(
        tmp_path,
        mode="quick",
        seeds=(101,),
    )
    root = tmp_path / "music-score-run"
    for job in manifest["jobs"]:
        audio = root / job["audio"]
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"synthetic flac fixture")
    return root


def test_score_submission_is_idempotent_and_progress_is_blind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_run(tmp_path, monkeypatch)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    blind_id = manifest["jobs"][0]["blind_id"]
    score = _score(blind_id)

    assert submit_music_calibration_score(root, score) is True
    assert submit_music_calibration_score(root, score) is False
    progress = music_calibration_progress(root)
    assert progress == {
        "total": 4,
        "scored": 1,
        "remaining": 3,
        "ready_to_reveal": False,
    }
    scores = json.loads((root / "scores.json").read_text(encoding="utf-8"))
    assert "profile" not in json.dumps(scores).casefold()
    assert "seed" not in json.dumps(scores).casefold()


def test_score_requires_generated_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(music_calibration, "_run_id", lambda: "music-no-audio")
    manifest = music_calibration.create_music_calibration_run(
        tmp_path,
        mode="quick",
        seeds=(101,),
    )
    root = tmp_path / "music-no-audio"
    with pytest.raises(
        MusicCalibrationScoreError,
        match="MUSIC_CALIBRATION_AUDIO_NOT_READY",
    ):
        submit_music_calibration_score(
            root,
            _score(manifest["jobs"][0]["blind_id"]),
        )


def test_results_reveal_only_after_all_scores_and_recommend_eligible_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_run(tmp_path, monkeypatch)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    mapping = json.loads(
        (root / "private-mapping.json").read_text(encoding="utf-8")
    )["mapping"]

    first = manifest["jobs"][0]["blind_id"]
    submit_music_calibration_score(root, _score(first))
    with pytest.raises(
        MusicCalibrationScoreError,
        match="MUSIC_CALIBRATION_SCORING_INCOMPLETE",
    ):
        reveal_music_calibration_results(root)

    for job in manifest["jobs"][1:]:
        blind_id = job["blind_id"]
        profile_name = mapping[blind_id]["profile"]["name"]
        good = profile_name.startswith("official-comfy-1.7")
        submit_music_calibration_score(root, _score(blind_id, good=good))
    first_profile = mapping[first]["profile"]["name"]
    replacement = _score(
        first,
        good=first_profile.startswith("official-comfy-1.7"),
    )
    submit_music_calibration_score(root, replacement)

    result = reveal_music_calibration_results(root)
    assert result["blinded"] is False
    assert result["scored_count"] == 4
    assert len(result["samples"]) == 4
    assert result["recommendation"]["profile"] == "official-comfy-1.7"
    assert result["recommendation"]["seeds"] == [101]
    assert result["recommendation"]["production_change_allowed"] is True
    assert reveal_music_calibration_results(root) == result


def test_hard_failure_blocks_affected_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_run(tmp_path, monkeypatch)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for index, job in enumerate(manifest["jobs"]):
        submit_music_calibration_score(
            root,
            _score(job["blind_id"], hard_fail=index == 0),
        )
    result = reveal_music_calibration_results(root)
    assert any(
        summary["hard_fail_count"] > 0
        for summary in result["profile_summary"].values()
    )


def test_tampered_request_is_detected_before_reveal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_run(tmp_path, monkeypatch)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for job in manifest["jobs"]:
        submit_music_calibration_score(root, _score(job["blind_id"]))
    target = root / "requests" / f"{manifest['jobs'][0]['blind_id']}.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(
        MusicCalibrationScoreError,
        match="MUSIC_CALIBRATION_REQUEST_TAMPERED",
    ):
        reveal_music_calibration_results(root)


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("piano_carries_arrangement", 3, "MUSIC_CALIBRATION_SCORE_VALUE_INVALID"),
        ("extra_instruments_severity", True, "MUSIC_CALIBRATION_SCORE_VALUE_INVALID"),
        ("lyrics_incomplete", 1, "MUSIC_CALIBRATION_HARD_FAIL_VALUE_INVALID"),
        ("notes", "x" * 501, "MUSIC_CALIBRATION_SCORE_NOTES_INVALID"),
    ],
)
def test_score_contract_rejects_invalid_fields(
    field: str,
    value: object,
    error_code: str,
) -> None:
    kwargs = _score("sample-0001").to_dict()
    kwargs.pop("schema_version")
    kwargs[field] = value
    with pytest.raises(ValueError, match=error_code):
        MusicCalibrationScore(**kwargs)
