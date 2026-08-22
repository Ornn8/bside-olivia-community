"""Blind score capture, reveal, and recommendation for local music calibration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping

from music_calibration import MUSIC_CALIBRATION_SCHEMA_VERSION


MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION = "p03.music-calibration-score.v1"
_BLIND_ID = re.compile(r"^sample-[0-9]{4}$")
_VARIANT_SUFFIX = re.compile(r"-v[0-9]+$")
_GOOD_FIELDS = (
    "piano_carries_arrangement",
    "mandarin_clarity",
    "restrained_lyricism",
    "emotion_match",
    "lyrics_complete_and_singable",
    "ending_complete",
)
_BAD_FIELDS = (
    "extra_instruments_severity",
    "rnb_soul_groove_severity",
    "vocal_ornament_severity",
)
_SCORE_FIELDS = (*_GOOD_FIELDS, *_BAD_FIELDS)
_HARD_FAIL_FIELDS = (
    "lyrics_incomplete",
    "abrupt_cut",
    "file_corrupt",
)


class MusicCalibrationScoreError(RuntimeError):
    """Stable local calibration state error."""


@dataclass(frozen=True)
class MusicCalibrationScore:
    blind_id: str
    piano_carries_arrangement: int
    extra_instruments_severity: int
    rnb_soul_groove_severity: int
    vocal_ornament_severity: int
    mandarin_clarity: int
    restrained_lyricism: int
    emotion_match: int
    lyrics_complete_and_singable: int
    ending_complete: int
    lyrics_incomplete: bool = False
    abrupt_cut: bool = False
    file_corrupt: bool = False
    notes: str = ""
    schema_version: str = MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION:
            raise ValueError("MUSIC_CALIBRATION_SCORE_SCHEMA_UNSUPPORTED")
        if not isinstance(self.blind_id, str) or not _BLIND_ID.fullmatch(self.blind_id):
            raise ValueError("MUSIC_CALIBRATION_BLIND_ID_INVALID")
        for field in _SCORE_FIELDS:
            value = getattr(self, field)
            if type(value) is not int or not 0 <= value <= 2:
                raise ValueError("MUSIC_CALIBRATION_SCORE_VALUE_INVALID")
        for field in _HARD_FAIL_FIELDS:
            if type(getattr(self, field)) is not bool:
                raise ValueError("MUSIC_CALIBRATION_HARD_FAIL_VALUE_INVALID")
        if not isinstance(self.notes, str) or len(self.notes) > 500 or any(
            ord(character) < 32 and character not in {"\n", "\t"}
            for character in self.notes
        ):
            raise ValueError("MUSIC_CALIBRATION_SCORE_NOTES_INVALID")

    @property
    def hard_failed(self) -> bool:
        return any(getattr(self, field) for field in _HARD_FAIL_FIELDS)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "blind_id": self.blind_id,
            **{field: getattr(self, field) for field in _SCORE_FIELDS},
            **{field: getattr(self, field) for field in _HARD_FAIL_FIELDS},
            "notes": self.notes,
        }


def _load_json(path: Path, code: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MusicCalibrationScoreError(code) from exc
    if not isinstance(value, dict):
        raise MusicCalibrationScoreError(code)
    return value


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_root(path: Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_RUN_NOT_FOUND")
    return root


def _manifest(root: Path) -> dict[str, object]:
    manifest = _load_json(
        root / "manifest.json",
        "MUSIC_CALIBRATION_MANIFEST_INVALID",
    )
    if manifest.get("schema_version") != MUSIC_CALIBRATION_SCHEMA_VERSION:
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
    return manifest


def _job_index(manifest: Mapping[str, object]) -> dict[str, dict[str, object]]:
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list):
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
    index: dict[str, dict[str, object]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
        blind_id = job.get("blind_id")
        if not isinstance(blind_id, str) or not _BLIND_ID.fullmatch(blind_id):
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
        if blind_id in index:
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
        index[blind_id] = job
    return index


def _load_scores(root: Path) -> dict[str, dict[str, object]]:
    path = root / "scores.json"
    if not path.exists():
        return {}
    payload = _load_json(path, "MUSIC_CALIBRATION_SCORES_INVALID")
    if payload.get("schema_version") != MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION:
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_SCORES_INVALID")
    scores = payload.get("scores")
    if not isinstance(scores, dict):
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_SCORES_INVALID")
    return {str(key): value for key, value in scores.items() if isinstance(value, dict)}


def submit_music_calibration_score(
    run_root: Path,
    score: MusicCalibrationScore,
) -> bool:
    """Store one blind score; identical retries are idempotent."""

    if not isinstance(score, MusicCalibrationScore):
        raise TypeError("MUSIC_CALIBRATION_TYPED_SCORE_REQUIRED")
    root = _run_root(run_root)
    if (root / "results.json").exists():
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_ALREADY_REVEALED")
    manifest = _manifest(root)
    jobs = _job_index(manifest)
    job = jobs.get(score.blind_id)
    if job is None:
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_SAMPLE_NOT_FOUND")
    audio = job.get("audio")
    if not isinstance(audio, str):
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MANIFEST_INVALID")
    audio_path = (root / audio).resolve()
    if not audio_path.is_relative_to(root) or not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_AUDIO_NOT_READY")

    scores = _load_scores(root)
    encoded = score.to_dict()
    current = scores.get(score.blind_id)
    if current == encoded:
        return False
    scores[score.blind_id] = encoded
    _atomic_json(
        root / "scores.json",
        {
            "schema_version": MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION,
            "run_id": manifest.get("run_id"),
            "scores": scores,
        },
    )
    return True


def music_calibration_progress(run_root: Path) -> dict[str, int | bool]:
    root = _run_root(run_root)
    jobs = _job_index(_manifest(root))
    scores = _load_scores(root)
    scored = sum(1 for blind_id in jobs if blind_id in scores)
    return {
        "total": len(jobs),
        "scored": scored,
        "remaining": len(jobs) - scored,
        "ready_to_reveal": scored == len(jobs),
    }


def _request_digest(root: Path, job: Mapping[str, object]) -> str:
    blind_id = job.get("blind_id")
    request_path = (root / "requests" / f"{blind_id}.json").resolve()
    if not request_path.is_relative_to(root) or not request_path.is_file():
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_REQUEST_TAMPERED")
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_REQUEST_TAMPERED") from exc
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _mean(rows: list[dict[str, object]], field: str) -> float:
    return round(sum(int(row[field]) for row in rows) / len(rows), 4)


def _summary(rows: list[dict[str, object]]) -> dict[str, object]:
    means = {field: _mean(rows, field) for field in _SCORE_FIELDS}
    hard_fail_count = sum(
        1
        for row in rows
        if any(bool(row[field]) for field in _HARD_FAIL_FIELDS)
    )
    eligible = (
        hard_fail_count == 0
        and means["extra_instruments_severity"] <= 0.5
        and means["rnb_soul_groove_severity"] <= 0.5
        and all(means[field] >= 1.5 for field in _GOOD_FIELDS)
    )
    quality = round(
        sum(means[field] for field in _GOOD_FIELDS)
        - sum(means[field] for field in _BAD_FIELDS),
        4,
    )
    return {
        "sample_count": len(rows),
        "means": means,
        "hard_fail_count": hard_fail_count,
        "eligible": eligible,
        "quality_score": quality,
    }


def reveal_music_calibration_results(run_root: Path) -> dict[str, object]:
    """Reveal profiles only after every blind sample has a score."""

    root = _run_root(run_root)
    existing = root / "results.json"
    if existing.exists():
        return _load_json(existing, "MUSIC_CALIBRATION_RESULTS_INVALID")
    manifest = _manifest(root)
    jobs = _job_index(manifest)
    scores = _load_scores(root)
    if set(scores) != set(jobs):
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_SCORING_INCOMPLETE")
    private = _load_json(
        root / "private-mapping.json",
        "MUSIC_CALIBRATION_MAPPING_INVALID",
    )
    mapping = private.get("mapping")
    if not isinstance(mapping, dict) or set(mapping) != set(jobs):
        raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MAPPING_INVALID")

    revealed_rows: list[dict[str, object]] = []
    by_profile: dict[str, list[dict[str, object]]] = {}
    by_profile_seed: dict[str, list[dict[str, object]]] = {}
    for blind_id, job in jobs.items():
        mapped = mapping.get(blind_id)
        if not isinstance(mapped, dict):
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MAPPING_INVALID")
        if mapped.get("request_sha256") != _request_digest(root, job):
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_REQUEST_TAMPERED")
        profile = mapped.get("profile")
        if not isinstance(profile, dict):
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MAPPING_INVALID")
        name = profile.get("name")
        seed = profile.get("seed")
        if not isinstance(name, str) or type(seed) is not int:
            raise MusicCalibrationScoreError("MUSIC_CALIBRATION_MAPPING_INVALID")
        base_profile = _VARIANT_SUFFIX.sub("", name)
        row = {
            **scores[blind_id],
            "case_id": job.get("case_id"),
            "base_profile": base_profile,
            "seed": seed,
        }
        revealed_rows.append(row)
        by_profile.setdefault(base_profile, []).append(row)
        by_profile_seed.setdefault(f"{base_profile}:{seed}", []).append(row)

    profile_summary = {
        key: _summary(rows)
        for key, rows in sorted(by_profile.items())
    }
    seed_summary = {
        key: _summary(rows)
        for key, rows in sorted(by_profile_seed.items())
    }
    eligible_profiles = [
        (name, data)
        for name, data in profile_summary.items()
        if data["eligible"]
    ]
    eligible_profiles.sort(
        key=lambda item: (-float(item[1]["quality_score"]), item[0])
    )
    recommended_profile = eligible_profiles[0][0] if eligible_profiles else None
    recommended_seeds: list[int] = []
    if recommended_profile is not None:
        candidates = [
            (key, data)
            for key, data in seed_summary.items()
            if key.startswith(recommended_profile + ":") and data["eligible"]
        ]
        candidates.sort(
            key=lambda item: (-float(item[1]["quality_score"]), item[0])
        )
        recommended_seeds = [int(key.rsplit(":", 1)[1]) for key, _ in candidates]

    result = {
        "schema_version": MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION,
        "run_id": manifest.get("run_id"),
        "blinded": False,
        "scored_count": len(revealed_rows),
        "samples": revealed_rows,
        "profile_summary": profile_summary,
        "seed_summary": seed_summary,
        "recommendation": {
            "profile": recommended_profile,
            "seeds": recommended_seeds,
            "production_change_allowed": bool(
                recommended_profile and recommended_seeds
            ),
        },
    }
    _atomic_json(existing, result)
    return result


__all__ = [
    "MUSIC_CALIBRATION_SCORE_SCHEMA_VERSION",
    "MusicCalibrationScore",
    "MusicCalibrationScoreError",
    "music_calibration_progress",
    "reveal_music_calibration_results",
    "submit_music_calibration_score",
]
