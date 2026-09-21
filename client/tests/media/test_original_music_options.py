import json
import pytest
from runtime.media.music_options import validate,generation_parameters
from runtime.music_settings import MusicSettings

def test_defaults_and_custom_parameters():
    defaults=generation_parameters({},'default caption')
    assert defaults['guidance_scale']==7 and defaults['use_adg'] is True
    assert defaults['thinking'] is False and defaults['dcw_enabled'] is False
    custom=generation_parameters({'caption':'轻快的爵士钢琴','bpm':120,'use_cot':True,'thinking':True,'seed':42},'unused')
    assert custom['caption']=='轻快的爵士钢琴' and custom['bpm']==120 and custom['seed']==42
    assert custom['use_cot_caption'] and custom['thinking'] and not custom['use_cot_lyrics']

@pytest.mark.parametrize('value',[{'guidance_scale':float('nan')},{'guidance_scale':True},{'inference_steps':1000},{'seed':-1},{'bpm':68.5},{'use_cot':'false'},{'voice_lora':'/tmp/model'},{'dcw_enabled':True},{'keyscale':'invalid'},{'caption':'a'*3001}])
def test_reject_invalid_or_private_parameters(value):
    with pytest.raises(ValueError):validate(value)

def test_persistence_and_failed_save_preserves_settings(tmp_path):
    env={};settings=MusicSettings(tmp_path,env);settings.load()
    settings.save({'caption':'自定义','guidance_scale':9})
    again=MusicSettings(tmp_path,{});again.load()
    assert again.status()['options']['caption']=='自定义'
    with pytest.raises(Exception):again.save({'bpm':999})
    assert again.status()['options']['guidance_scale']==9
    settings.path.write_text('{broken',encoding='utf-8');again.load()
    assert again.status()['error_code']=='MUSIC_SETTINGS_UNAVAILABLE'
