"""Synthetic blind-listening run preparation for MiniMax Music 3."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
from typing import Iterable, Sequence

from minimax_profile import (
    CURRENT_MINIMAX_PROFILE,
    MiniMaxInferenceProfile,
    OFFICIAL_COMFY_MINIMAX_PROFILE,
)
from music_caption import MINIMAX_CAPTION_VERSION, render_minimax_caption
from song_content import (
    PianoTexture,
    SongDynamicArc,
    SongEmotionArc,
    SongEnding,
    SongSemanticPlan,
    VocalDelivery,
)


MUSIC_CALIBRATION_SCHEMA_VERSION = "p03.music-calibration.v1"
MUSIC_CALIBRATION_CASESET_VERSION = "p03.music-cases.v2"
DEFAULT_CALIBRATION_SEEDS = (200717, 1247, 2702, 202608)
_QUICK_CASE_IDS = ("ordinary_reassurance", "conflict_repair")


@dataclass(frozen=True)
class MusicCalibrationCase:
    case_id: str
    category: str
    plan: SongSemanticPlan

    def public_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "duration_seconds": self.plan.duration_seconds,
        }


def _lyrics(first: Sequence[str], second: Sequence[str]) -> str:
    return "\n".join(
        (
            "[Intro]",
            "[Verse]",
            *first,
            "[Chorus]",
            *second,
            "[Outro]",
        )
    )


def _case(
    case_id: str,
    category: str,
    *,
    emotion: SongEmotionArc,
    texture: PianoTexture,
    vocal: VocalDelivery,
    dynamic: SongDynamicArc,
    ending: SongEnding,
    first: Sequence[str],
    second: Sequence[str],
) -> MusicCalibrationCase:
    duration = 40 if len(first) == 6 and len(second) == 6 else 60
    return MusicCalibrationCase(
        case_id=case_id,
        category=category,
        plan=SongSemanticPlan(
            emotion_arc=emotion,
            piano_texture=texture,
            vocal_delivery=vocal,
            dynamic_arc=dynamic,
            ending=ending,
            lyrics=_lyrics(first, second),
            duration_seconds=duration,
        ),
    )


CALIBRATION_CASES = (
    _case(
        "ordinary_reassurance",
        "普通安慰",
        emotion=SongEmotionArc.GENTLE_REASSURANCE,
        texture=PianoTexture.TRANSPARENT_BROKEN_CHORDS,
        vocal=VocalDelivery.CLEAR_LEGATO,
        dynamic=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        first=(
            "灯还亮着你先坐一会",
            "不用急着把答案说完",
            "窗外的风慢慢停下来",
            "我把声音放得轻一点",
            "今晚先照顾眼前一步",
            "剩下的事明天再想吧",
        ),
        second=(
            "你不需要立刻变勇敢",
            "难过也有自己的位置",
            "等呼吸重新变得安稳",
            "再把心事一点点收好",
            "我会认真听你说下去",
            "这一晚就先这样过去",
        ),
    ),
    _case(
        "restrained_loneliness",
        "失落与孤独",
        emotion=SongEmotionArc.RESTRAINED_SADNESS,
        texture=PianoTexture.LYRICAL_ARPEGGIOS,
        vocal=VocalDelivery.CONTAINED_INTIMATE,
        dynamic=SongDynamicArc.SOFT_STEADY_SETTLE,
        ending=SongEnding.LINGERING_PIANO_CADENCE,
        first=(
            "房间安静得只剩钟声",
            "你把今天折进了口袋",
            "有些名字没有被说出",
            "影子却在门边停很久",
            "我不替你解释这份空",
            "只把沉默留得宽一些",
        ),
        second=(
            "夜色不会催你快点好",
            "旧情绪也不必被赶走",
            "等你愿意抬头的时候",
            "会看见窗沿还有微光",
            "我在这里不多问一句",
            "陪你听完最后的钟声",
        ),
    ),
    _case(
        "intimate_daily",
        "亲密日常",
        emotion=SongEmotionArc.CALM_AFFECTION,
        texture=PianoTexture.MEASURED_CHORDAL_VOICING,
        vocal=VocalDelivery.GENTLE_NARRATIVE,
        dynamic=SongDynamicArc.QUIET_GRADUAL_WARMTH,
        ending=SongEnding.SHORT_SETTLED_CADENCE,
        first=(
            "你又忘了把杯子收好",
            "桌角还放着半张便签",
            "我本来想假装没看见",
            "最后还是替你压平了",
            "这些小事没有大道理",
            "却让今天显得很具体",
        ),
        second=(
            "等你回来记得先洗手",
            "唱片也别随便靠着放",
            "我会给你留一盏小灯",
            "但不许把客厅弄太乱",
            "普通一天就这样收尾",
            "好像也已经足够温柔",
        ),
    ),
    _case(
        "conflict_repair",
        "冲突后的修复",
        emotion=SongEmotionArc.SOFT_RECONCILIATION,
        texture=PianoTexture.SPARSE_COUNTERLINE,
        vocal=VocalDelivery.CLEAR_LEGATO,
        dynamic=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        first=(
            "刚才那句话落得太重",
            "我们都没有及时收住",
            "我还记得你转开的脸",
            "也记得自己没有解释",
            "生气不是故事的结尾",
            "只是中间打乱了几拍",
        ),
        second=(
            "现在我把语气放慢些",
            "不要求你立刻原谅我",
            "先把误会一件件说清",
            "再决定明天怎么继续",
            "靠近不是假装没争过",
            "是争过以后仍愿意听",
        ),
    ),
    _case(
        "requested_performance",
        "明确请求演奏",
        emotion=SongEmotionArc.WARM_GRATITUDE,
        texture=PianoTexture.MEASURED_CHORDAL_VOICING,
        vocal=VocalDelivery.QUIET_SONGFUL,
        dynamic=SongDynamicArc.QUIET_GRADUAL_WARMTH,
        ending=SongEnding.COMPLETE_SOFT_CADENCE,
        first=(
            "你说想听一段新的歌",
            "我先把窗边的灯调暗",
            "几个和弦还没有名字",
            "却已经知道往哪里走",
            "这次不写宏大的愿望",
            "只写你刚提起的心情",
        ),
        second=(
            "旋律从很低的地方来",
            "经过一句迟到的问候",
            "再把未说完的话托住",
            "让它们慢慢有了方向",
            "等最后一个和弦落下",
            "你再告诉我听见什么",
        ),
    ),
    _case(
        "spontaneous_motif",
        "主动形成旋律",
        emotion=SongEmotionArc.QUIET_LONGING,
        texture=PianoTexture.LYRICAL_ARPEGGIOS,
        vocal=VocalDelivery.CONTAINED_INTIMATE,
        dynamic=SongDynamicArc.SOFT_GENTLE_RISE_SETTLE,
        ending=SongEnding.LINGERING_PIANO_CADENCE,
        first=(
            "刚才雨点敲过旧窗框",
            "有三个音忽然连起来",
            "像一句话只说到一半",
            "停在你名字前面不走",
            "我把它记进空白谱页",
            "又试着往后多写一行",
            "旋律没有急着变明亮",
            "只是慢慢学会了呼吸",
        ),
        second=(
            "第二遍弹得比刚才轻",
            "低音留出更长的空隙",
            "那些没有寄出的念头",
            "终于找到安静的落点",
            "我不替它规定好结局",
            "也不把回忆写得太满",
            "等余音沿着房间散开",
            "这段旋律就交给你听",
        ),
    ),
)


def calibration_cases(mode: str = "full") -> tuple[MusicCalibrationCase, ...]:
    if mode == "full":
        return CALIBRATION_CASES
    if mode == "quick":
        return tuple(case for case in CALIBRATION_CASES if case.case_id in _QUICK_CASE_IDS)
    raise ValueError("MUSIC_CALIBRATION_MODE_INVALID")


def _profile_with_seed(
    base: MiniMaxInferenceProfile,
    seed: int,
    index: int,
) -> MiniMaxInferenceProfile:
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise ValueError("MUSIC_CALIBRATION_SEED_INVALID")
    return replace(base, name=f"{base.name}-v{index}", seed=seed)


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"music-{timestamp}-{secrets.token_hex(4)}"


def create_music_calibration_run(
    root: Path,
    *,
    mode: str = "full",
    profiles: Iterable[MiniMaxInferenceProfile] = (
        CURRENT_MINIMAX_PROFILE,
        OFFICIAL_COMFY_MINIMAX_PROFILE,
    ),
    seeds: Sequence[int] = DEFAULT_CALIBRATION_SEEDS,
) -> dict[str, object]:
    """Create a local blind run without invoking an LLM or a music model."""

    root = Path(root).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("MUSIC_CALIBRATION_ROOT_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    cases = calibration_cases(mode)
    base_profiles = tuple(profiles)
    if not base_profiles or any(
        not isinstance(profile, MiniMaxInferenceProfile) for profile in base_profiles
    ):
        raise ValueError("MUSIC_CALIBRATION_PROFILES_INVALID")
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("MUSIC_CALIBRATION_SEEDS_INVALID")

    run_id = _run_id()
    run_root = root / run_id
    request_root = run_root / "requests"
    audio_root = run_root / "audio"
    request_root.mkdir(parents=True)
    audio_root.mkdir()

    variants: list[tuple[MusicCalibrationCase, MiniMaxInferenceProfile]] = []
    variant_index = 0
    for case in cases:
        for base in base_profiles:
            for seed in seeds:
                variant_index += 1
                variants.append(
                    (case, _profile_with_seed(base, seed, variant_index))
                )
    secrets.SystemRandom().shuffle(variants)

    public_jobs: list[dict[str, object]] = []
    private_mapping: dict[str, object] = {}
    batch_jobs: list[dict[str, str]] = []
    for index, (case, profile) in enumerate(variants, start=1):
        blind_id = f"sample-{index:04d}"
        request_rel = Path("requests") / f"{blind_id}.json"
        output_rel = Path("audio") / f"{blind_id}.flac"
        request_payload = {
            "max_duration": case.plan.duration_seconds,
            "lyrics": case.plan.lyrics,
            "caption": render_minimax_caption(case.plan),
            "inference_profile": profile.to_dict(),
        }
        _atomic_json(run_root / request_rel, request_payload)
        public_jobs.append(
            {
                "blind_id": blind_id,
                **case.public_dict(),
                "audio": output_rel.as_posix(),
                "status": "pending",
            }
        )
        private_mapping[blind_id] = {
            "case_id": case.case_id,
            "profile": profile.to_dict(),
            "request_sha256": hashlib.sha256(
                json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        batch_jobs.append(
            {
                "request_json": request_rel.as_posix(),
                "output": output_rel.as_posix(),
            }
        )

    manifest = {
        "schema_version": MUSIC_CALIBRATION_SCHEMA_VERSION,
        "caseset_version": MUSIC_CALIBRATION_CASESET_VERSION,
        "caption_version": MINIMAX_CAPTION_VERSION,
        "run_id": run_id,
        "mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "blinded": True,
        "job_count": len(public_jobs),
        "jobs": public_jobs,
    }
    _atomic_json(run_root / "manifest.json", manifest)
    _atomic_json(
        run_root / "private-mapping.json",
        {
            "schema_version": MUSIC_CALIBRATION_SCHEMA_VERSION,
            "caseset_version": MUSIC_CALIBRATION_CASESET_VERSION,
            "caption_version": MINIMAX_CAPTION_VERSION,
            "run_id": run_id,
            "profiles_hidden_until_scoring": True,
            "mapping": private_mapping,
        },
    )
    _atomic_json(run_root / "batch.json", {"jobs": batch_jobs})
    return manifest


__all__ = [
    "CALIBRATION_CASES",
    "DEFAULT_CALIBRATION_SEEDS",
    "MUSIC_CALIBRATION_CASESET_VERSION",
    "MUSIC_CALIBRATION_SCHEMA_VERSION",
    "MusicCalibrationCase",
    "calibration_cases",
    "create_music_calibration_run",
]
