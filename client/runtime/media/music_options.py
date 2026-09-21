"""Public original-music controls; never accept model paths or adapter weights."""
import json
import math

DEFAULTS = dict(caption='', guidance_scale=7.0, inference_steps=50, bpm=68,
                keyscale='Bb major', timesignature='4', seed=200717,
                use_cot=False, thinking=False, use_adg=True)
KEYS = tuple(f'{note} {mode}' for note in ('C','C#','Db','D','D#','Eb','E','F','F#','Gb','G','G#','Ab','A','A#','Bb','B') for mode in ('major','minor'))

def validate(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('MUSIC_OPTIONS_INVALID')
    result = {**DEFAULTS, **value}
    for name in ('use_cot', 'thinking', 'use_adg'):
        if type(result[name]) is not bool: raise ValueError('MUSIC_OPTIONS_INVALID')
    for name, lo, hi, integer in (('guidance_scale',1,15,False),('inference_steps',8,100,True),('bpm',30,240,True),('seed',0,2147483647,True)):
        v = result[name]
        if type(v) not in (int,float) or not math.isfinite(v) or not lo <= v <= hi or (integer and type(v) is not int):
            raise ValueError('MUSIC_OPTIONS_INVALID')
    if not isinstance(result['caption'],str) or len(result['caption'])>3000 or '\x00' in result['caption']:
        raise ValueError('MUSIC_OPTIONS_INVALID')
    if result['keyscale'] not in KEYS or result['timesignature'] not in ('2','3','4','6'):
        raise ValueError('MUSIC_OPTIONS_INVALID')
    return result

def from_environment(environment):
    return validate(json.loads(environment.get('OLIVIA_ORIGINAL_MUSIC_OPTIONS','{}')))

def generation_parameters(value, default_caption):
    options=validate(value)
    cot=options.pop('use_cot')
    options['caption']=options['caption'].strip() or default_caption
    return dict(options, duration=110, dcw_enabled=False,
                use_cot_metas=cot,use_cot_caption=cot,use_cot_language=cot,use_cot_lyrics=False)
