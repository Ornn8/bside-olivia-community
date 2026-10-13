"""Validated per-letter cover controls; shared by local and remote generation."""
import math

DEFAULTS = {'audio_cover_strength': 0.6, 'cover_noise_strength': 0.25}
# Her own performance over a new arrangement (cloud YuE2); the arrangement words are the user's.
ENGINES = ('yue2',)
ARRANGEMENT_LIMIT = 200


def validate(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys() - {'engine', 'arrangement'}:
        raise ValueError('COVER_OPTIONS_INVALID')
    if 'engine' in value and value['engine'] not in ENGINES:
        raise ValueError('COVER_OPTIONS_INVALID')
    if 'arrangement' in value and ('engine' not in value or not isinstance(value['arrangement'], str)
                                   or len(value['arrangement'].strip()) > ARRANGEMENT_LIMIT):
        raise ValueError('COVER_OPTIONS_INVALID')
    result = {**DEFAULTS, **{k: v for k, v in value.items() if k in DEFAULTS}}
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
           for v in result.values()):
        raise ValueError('COVER_OPTIONS_INVALID')
    return {**result, **{k: value[k] for k in ('engine', 'arrangement') if k in value}}


def without_engine(value):
    """The options a service without YuE2 covers accepts: it makes the voice-swap cover."""
    return {k: v for k, v in value.items() if k not in ('engine', 'arrangement')}
