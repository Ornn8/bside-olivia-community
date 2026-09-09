"""Deliver a cover as audio or through the existing reply-video stages."""
from pathlib import Path

from runtime.media.ace_cover import CoverError, generate_cover
from runtime.media.music_reply import (
    MusicReplyError, concat_videos, render_full_face_performance,
    _persist_provider_failure,
    separate_vocals,
    _valid_wave_audio,
)
from runtime.media.media_paths import configured_media_path
from runtime.reply.reply_media import render_reply_video


def render_cover_reply(content, reply_text, output_path, *, source_audio,
                       environment, include_spoken=True, render_video=True,
                       normal_video_path, song_video_path, official_reply_reference_path,
                       tts_config_path, visual_config_path, worker_path, performance_video_path,
                       spoken_action_base_path=None, voice_performance_plan=None,
                       cover_lyrics="", cover_language="unknown", **unused):
    def path(key):
        return configured_media_path(environment, key)

    audio = output_path.with_name(output_path.stem + "-cover.wav") if render_video else output_path
    try:
        metadata = generate_cover(source_audio, audio, environment=environment,
                                  lyrics=cover_lyrics, language=cover_language)
    except CoverError as exc:
        _persist_provider_failure(str(exc), "provider=ace_step_xl; stage=cover; attempts=1", environment)
        raise MusicReplyError(str(exc)) from None
    if not render_video:
        return {**metadata, "reply_structure": "singing_audio"}
    return deliver_cover_video(audio, output_path, environment=environment, metadata=metadata,
        include_spoken=include_spoken, reply_text=reply_text, normal_video_path=normal_video_path,
        song_video_path=song_video_path, official_reply_reference_path=official_reply_reference_path,
        tts_config_path=tts_config_path, visual_config_path=visual_config_path, worker_path=worker_path,
        performance_video_path=performance_video_path, spoken_action_base_path=spoken_action_base_path,
        voice_performance_plan=voice_performance_plan)


def deliver_cover_video(audio, output_path, *, environment, metadata, include_spoken,
                        reply_text, normal_video_path, song_video_path, official_reply_reference_path,
                        tts_config_path, visual_config_path, worker_path, performance_video_path,
                        spoken_action_base_path=None, voice_performance_plan=None):
    """Reuse a finished cover through the same video delivery path as a new job."""
    def path(key):
        return configured_media_path(environment, key)

    common = dict(environment=environment, ffmpeg_path=path("OLIVIA_FFMPEG_EXE"),
                  provider_cache_root=path("OLIVIA_PROVIDER_CACHE_ROOT"))
    vocals = audio.with_name(audio.stem + "-vocals.wav")
    # Separate the completed cover only for mouth conditioning. The source is
    # never separated before ACE, and the final cover audio is never re-voiced.
    if metadata.get("music_stage") != "reused" or not _valid_wave_audio(vocals, ffmpeg_path=path("OLIVIA_FFMPEG_EXE")):
        separate_vocals(audio, vocals, executable=path("OLIVIA_ROFORMER_PYTHON") or path("OLIVIA_ROFORMER_EXE"),
                        model_path=path("OLIVIA_ROFORMER_MODEL_PATH"), config_path=path("OLIVIA_ROFORMER_CONFIG_PATH"),
                        environment=environment, ffmpeg_path=path("OLIVIA_FFMPEG_EXE"))
    render_full_face_performance(
        performance_video_path, vocals, audio, song_video_path,
        latentsync_python_path=path("OLIVIA_LATENTSYNC_PYTHON"),
        latentsync_root=path("OLIVIA_LATENTSYNC_ROOT"), **common)
    if include_spoken:
        if spoken_action_base_path is None or not spoken_action_base_path.is_file():
            raise MusicReplyError("MUSIC_REPLY_SPOKEN_REFERENCE_UNAVAILABLE")
        render_reply_video(reply_text, normal_video_path, tts_config_path=tts_config_path,
                           visual_config_path=visual_config_path, worker_path=worker_path,
                           scene_path=spoken_action_base_path, adaptive_delivery=True,
                           voice_performance_plan=voice_performance_plan,
                           latentsync_python_path=path("OLIVIA_LATENTSYNC_PYTHON"),
                           latentsync_root=path("OLIVIA_LATENTSYNC_ROOT"), **common)
        concat_videos(normal_video_path, song_video_path, output_path,
                      transition_video_path=official_reply_reference_path,
                      ffmpeg_path=path("OLIVIA_FFMPEG_EXE"))
    else:
        import shutil
        shutil.copyfile(song_video_path, output_path)
    return {**metadata, "reply_structure": "normal_video_then_official_transition_then_song_video"
            if include_spoken else "singing_only"}
