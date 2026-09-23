import json
from runtime.media.song_content import _planner_contract, _plan_from_lyrics_response, SongContentPlan
from runtime.media.song_plan_cache import cached_song_plan
from runtime.media.music_caption import render_minimax_caption
from runtime.media import original_song
from runtime import remote_pipeline

def test_suno_style_survives_cache_and_reaches_cloud(tmp_path,monkeypatch):
    data={'verse':['窗外的风轻轻吹']*20,'chorus':['把这首歌唱给你']*20,'style':'Warm jazz, piano and bass, gentle female vocals, soft resolving ending'}
    semantic=_plan_from_lyrics_response(json.dumps(data),240)
    plan=SongContentPlan(semantic.emotion_arc.value,semantic.lyrics,render_minimax_caption(semantic),240,semantic,data['style'])
    path=tmp_path/'plan.json'
    assert cached_song_plan(path,'letter','reply',240,lambda:plan).suno_style==data['style']
    assert cached_song_plan(path,'letter','reply',240,lambda:(_ for _ in ()).throw(AssertionError('replanned'))).suno_style==data['style']
    assert 'three keys' in _planner_contract(240) and 'style' in _planner_contract(240)
    monkeypatch.setattr(original_song,'cached_song_plan',lambda *a:plan)
    monkeypatch.setattr(remote_pipeline,'capabilities',lambda env:{'original_music_provider':'suno_v6'})
    calls=[]
    monkeypatch.setattr(remote_pipeline,'generate',lambda kind,payload,*a,**kw:calls.append(payload) or {})
    env={'OLIVIA_GPU_ROUTE':'remote'}
    original_song.render_original_reply('letter','reply',tmp_path/'song.wav',environment=env,render_video=False)
    assert calls[-1]['music_options']['caption']==data['style']
    env['OLIVIA_ORIGINAL_MUSIC_OPTIONS']=json.dumps({'caption':'user rock style','duration':210})
    original_song.render_original_reply('letter','reply',tmp_path/'song2.wav',environment=env,render_video=False)
    assert calls[-1]['music_options']['caption']=='user rock style' and calls[-1]['music_options']['duration']==210

def test_unlisted_photo_location_does_not_match_preset():
    from runtime.image_reply import _scene_matches_location, SYSTEM
    assert _scene_matches_location('none','海边露营地')
    assert not _scene_matches_location('music_room','海边露营地')
    assert not _scene_matches_location('cafe','海边露营地')
    assert '已知地点也可以选none' in SYSTEM
