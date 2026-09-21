"""Validated per-letter cover controls; shared by local and remote generation."""
import math

DEFAULTS = {'audio_cover_strength': 0.6, 'cover_noise_strength': 0.25}


def validate(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('COVER_OPTIONS_INVALID')
    result = {**DEFAULTS, **value}
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
           for v in result.values()):
        raise ValueError('COVER_OPTIONS_INVALID')
    return result
