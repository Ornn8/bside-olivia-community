import importlib.util
from pathlib import Path

import numpy as np


def _worker():
    path = Path(__file__).resolve().parents[2] / 'tools' / 'soulx_svc_worker.py'
    spec = importlib.util.spec_from_file_location('soulx_svc_worker_for_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contour(hz, n=400):
    values = np.full(n, float(hz))
    values[::7] = 0.0  # unvoiced frames are ignored
    return values


def test_a_source_an_octave_below_her_is_raised_one_octave():
    assert _worker().octave_shift(contour(169), contour(331)) == 12


def test_a_source_in_her_range_is_not_shifted():
    assert _worker().octave_shift(contour(339), contour(331)) == 0
    assert _worker().octave_shift(contour(280), contour(331)) == 0


def test_a_source_an_octave_above_is_lowered_and_short_takes_are_left_alone():
    assert _worker().octave_shift(contour(660), contour(331)) == -12
    assert _worker().octave_shift(contour(169, n=30), contour(331)) == 0
