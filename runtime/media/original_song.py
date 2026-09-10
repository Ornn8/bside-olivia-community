"""Approved original-song preset, independent of source-conditioned covers."""
from pathlib import Path

from runtime.media.ace_cover import CoverError, cover_paths, generate_ace
from runtime.media.media_paths import configured_media_path
from runtime.media.music_reply import MusicReplyError
from runtime.media.song_content import plan_song_content
from runtime.media.song_plan_cache import cached_song_plan

CAPTION = """linli_voice. 一首亲近、抒情的中文女声与原声三角钢琴歌曲。110 秒。
情绪从安静的欣赏，发展为温暖的感谢，最后落在从容的亲近感。采用自然的小房间录音质感。
女声以中低音区为主，轻柔、温暖地演唱，旋律自然、富有叙述感。咬字清晰，长音克制，节奏像交谈，句尾准确。主歌和副歌保持一致的个人化表达。
全曲只用一架原声三角钢琴伴奏。突出原声三角钢琴的声音，钢琴清晰、饱满，琴键触感与自然共鸣清楚可闻。人声轻柔地融入钢琴，演唱期间钢琴仍保持鲜明的存在感。开头轻柔，中段略增加和弦厚度与音符密度，最后回归平静。
0～6 秒前奏；6～54 秒主歌；54～102 秒副歌；102～110 秒钢琴尾奏，自然衰减。"""


def original_paths(environment):
    paths = cover_paths(environment)
    paths["voice_lora"] = configured_media_path(environment, "OLIVIA_ACE_ORIGINAL_LORA")
    # A model-only component declares its config file for installer verification;
    # PEFT loads the containing adapter directory.
    if paths["voice_lora"] is not None and paths["voice_lora"].name == "adapter_config.json":
        paths["voice_lora"] = paths["voice_lora"].parent
    return paths


def original_configured(environment):
    from runtime.media.ace_cover import ace_paths_configured
    return ace_paths_configured(original_paths(environment))


def render_original_reply(content, reply_text, output_path, *, environment,
                          duration_seconds=110, gateway=None, render_video=True,
                          include_spoken=True, **video_options):
    if duration_seconds != 110:
        raise MusicReplyError("MUSIC_DURATION_UNSUPPORTED")
    if not original_configured(environment):
        raise MusicReplyError("ORIGINAL_RUNTIME_UNAVAILABLE")
    audio = output_path.with_name(output_path.stem + "-original.wav") if render_video else output_path
    try:
        plan = cached_song_plan(audio.parent / (audio.stem + "-song-plan.private.json"),
            content, reply_text, 110,
            lambda: plan_song_content(content, reply_text, 110, **({"gateway": gateway} if gateway is not None else {})))
    except Exception as exc:
        raise MusicReplyError("SONG_CONTENT_UNAVAILABLE") from exc
    try:
        metadata = generate_ace(None, audio, environment=environment, paths=original_paths(environment),
            lyrics=plan.lyrics, language="zh", task_type="text2music",
            parameters={"caption": CAPTION, "duration": 110, "bpm": 68,
                        "keyscale": "Bb major", "timesignature": "4"})
    except CoverError as exc:
        raise MusicReplyError(str(exc)) from None
    if not render_video:
        return {**metadata, "reply_structure": "original_song_audio"}
    from runtime.media.cover_reply import deliver_cover_video
    return deliver_cover_video(audio, output_path, environment=environment, metadata=metadata,
        include_spoken=include_spoken, reply_text=reply_text, separate_for_lipsync=False, **video_options)
