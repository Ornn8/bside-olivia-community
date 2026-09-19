from pathlib import Path
import json
import sys
import wave

import pytest

from runtime.media.voice_conversion import VoiceConversionError, convert_singing_voice


def test_missing_converter_never_publishes_original_vocals(tmp_path):
    vocals = tmp_path / "vocals.wav"
    vocals.write_bytes(b"original singer")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"target")
    output = tmp_path / "converted.wav"
    with pytest.raises(VoiceConversionError, match="SOULX_SVC_UNAVAILABLE"):
        convert_singing_voice(vocals, reference, output, environment={}, ffmpeg_path=None)
    assert not output.exists()


def test_conversion_uses_reference_and_offline_child_output(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    entry = root / "source/soulxsinger/models/soulxsinger_svc.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# fixture")
    # Fake external provider, exercising the real process/file boundary.
    worker = root / "fake_worker.py"
    worker.write_text('''
import sys, os, json, pathlib, wave
a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
assert pathlib.Path(a['--reference']).read_bytes() == b'target voice'
assert os.environ['HF_HUB_OFFLINE'] == '1'
p = pathlib.Path(a['--output'])
with wave.open(str(p), 'wb') as w:
    w.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
    w.writeframes(b'\\x01\\x00' * 44100)
''', encoding="utf-8")
    monkeypatch.setattr("runtime.media.voice_conversion._worker_path", lambda: worker)
    source, reference, output = [tmp_path / n for n in ("source.wav", "ref.wav", "result.wav")]
    source.write_bytes(b"source voice")
    reference.write_bytes(b"target voice")
    convert_singing_voice(source, reference, output, environment={
        "OLIVIA_SOULX_SVC_ROOT": str(root), "OLIVIA_SOULX_SVC_PYTHON": sys.executable,
    }, ffmpeg_path=None)
    with wave.open(str(output)) as result:
        assert result.getnframes() == 44100
    assert output.read_bytes() != source.read_bytes()
