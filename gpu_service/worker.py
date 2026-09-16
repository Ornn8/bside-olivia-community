"""Run one task using the existing isolated model workers."""
import json
import os
from pathlib import Path
import sys


def run(spec):
    kind, data, profile = spec['kind'], spec['input'], spec['profile']
    env = {**os.environ, **profile.get('environment', {}), 'OLIVIA_GPU_ROUTE': 'local',
           'CUDA_VISIBLE_DEVICES': os.environ.get('CUDA_VISIBLE_DEVICES', '0')}
    env.setdefault('OLIVIA_LOCAL_DATA_ROOT', spec['job'])
    os.environ.update(env)
    output = Path(spec['output'])
    media = output.with_suffix('.mp4' if kind in ('video', 'lipsync') else '.wav')
    def path(name):
        value = profile.get(name)
        return Path(value) if value else None
    if kind in ('tts', 'video'):
        from runtime.media.voice_direction import TextOnlyVoicePlan, VoicePerformancePlan
        value = data.get('voice_plan', {'reply_text': data['text']})
        if set(value) == {'reply_text'}:
            plan = TextOnlyVoicePlan(**value)
        elif 'profile' in value:
            plan = VoicePerformancePlan.from_dict(value)
        else:
            plan = VoicePerformancePlan.from_music_dict(value)
        if plan.spoken_text != data['text']:
            raise ValueError('VOICE_TEXT_MISMATCH')
        from runtime.reply.reply_media import render_reply_audio, render_reply_video
        if kind == 'tts':
            render_reply_audio(data['text'], media, tts_config_path=path('tts_config_path'),
                               voice_performance_plan=plan, environment=env)
        else:
            render_reply_video(data['text'], media, tts_config_path=path('tts_config_path'),
                visual_config_path=path('visual_config_path'), worker_path=path('worker_path'),
                scene_path=Path(data['scene_asset']), latentsync_python_path=path('latentsync_python_path'),
                latentsync_root=path('latentsync_root'), voice_performance_plan=plan,
                adaptive_delivery=data.get('adaptive_delivery', False),
                enforce_content_gate=data.get('enforce_content_gate', False),
                environment=env, ffmpeg_path=path('ffmpeg_path'),
                provider_cache_root=path('provider_cache_root'))
    elif kind == 'cover':
        from runtime.media.ace_cover import generate_cover
        generate_cover(Path(data['source_asset']), media, environment=env,
                       lyrics=data.get('lyrics', ''), language=data.get('language', 'unknown'))
    elif kind == 'original':
        from runtime.media.ace_cover import generate_ace
        from runtime.media.original_song import original_paths, CAPTION
        generate_ace(None, media, environment=env, paths=original_paths(env),
            lyrics=data['lyrics'], language='zh', task_type='text2music',
            parameters={'caption': CAPTION, 'duration': 110, 'bpm': 68,
                        'keyscale': 'Bb major', 'timesignature': '4'})
    elif kind == 'lipsync':
        from runtime.media.latentsync_reply import render_latentsync_video
        render_latentsync_video(Path(data['scene_asset']), Path(data['audio_asset']), media,
            python_path=path('latentsync_python_path'), latentsync_root=path('latentsync_root'),
            environment=env, ffmpeg_path=path('ffmpeg_path'),
            provider_cache_root=path('provider_cache_root'))
    elif kind == 'separate':
        from runtime.media.music_reply import separate_vocals
        separate_vocals(Path(data['audio_asset']), media, environment=env,
            executable=path('executable'), model_path=path('model_path'),
            config_path=path('config_path'), ffmpeg_path=path('ffmpeg_path'))
    else:
        raise ValueError('CAPABILITY_UNAVAILABLE')
    if not media.is_file() or not media.stat().st_size:
        raise ValueError('OUTPUT_MISSING')
    from runtime.media.music_reply import _media_duration_seconds
    duration = _media_duration_seconds(media, required_streams=('0:a:0','0:v:0') if kind in ('video','lipsync') else ('0:a:0',), ffmpeg_path=path('ffmpeg_path'))
    if duration is None or duration <= 0:
        raise ValueError('OUTPUT_INVALID')
    media.replace(output)


if __name__ == '__main__':
    run(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
