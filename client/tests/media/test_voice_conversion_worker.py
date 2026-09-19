from __future__ import annotations

from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest


@pytest.mark.parametrize("pitch_shift", [0, 1])
def test_worker_uses_reference_conditioning_source_timeline_and_local_models(tmp_path, monkeypatch, pitch_shift):
    path = Path(__file__).resolve().parents[2] / "tools/soulx_svc_worker.py"
    spec = importlib.util.spec_from_file_location("tested_soulx_worker", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    observed = {"local_models": [], "f0": [], "writes": []}

    class Tensor:
        def __init__(self, data): self.data = np.asarray(data)
        def unsqueeze(self, axis): return Tensor(np.expand_dims(self.data, axis))
        def squeeze(self): return Tensor(self.data.squeeze())
        def to(self, _device): return self
        def float(self): return self
        def cpu(self): return self
        def numpy(self): return self.data

    class LocalModel:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            observed["local_models"].append((path, kwargs))
            return cls()
        def to(self, _device): return self

    class Model:
        def __init__(self, _config):
            self.whisper_encoder = svc.WhisperEncoder()
            self.mel = SimpleNamespace(float=lambda: observed.update(mel_fp32=True))
        def load_state_dict(self, state, strict): assert state == {} and strict is True
        def half(self): observed["fp16"] = True
        def to(self, _device): return self
        def eval(self): return self
        def infer(self, **kwargs):
            observed["infer"] = kwargs
            return kwargs["gt_wav"], pitch_shift

    torch = ModuleType("torch")
    torch.cuda = SimpleNamespace(is_available=lambda: True, empty_cache=lambda: None, manual_seed_all=lambda seed: None)
    torch.from_numpy = Tensor
    torch.load = lambda *_a, **_kw: {"state_dict": {}}
    torch.manual_seed = lambda seed: None
    torch.inference_mode = nullcontext
    svc = ModuleType("soulxsinger.models.soulxsinger_svc")
    svc.SoulXSingerSVC = Model
    modules = {
        "torch": torch,
        "torchaudio": SimpleNamespace(functional=SimpleNamespace(resample=lambda value, *_: value)),
        "transformers": SimpleNamespace(WhisperFeatureExtractor=LocalModel, WhisperModel=LocalModel),
        "omegaconf": SimpleNamespace(OmegaConf=SimpleNamespace(load=lambda _: SimpleNamespace(audio=SimpleNamespace(sample_rate=44100, hop_size=512)))),
        "soulxsinger": ModuleType("soulxsinger"),
        "soulxsinger.models": ModuleType("soulxsinger.models"),
        "soulxsinger.models.soulxsinger_svc": svc,
        "soulxsinger.models.modules": ModuleType("soulxsinger.models.modules"),
        "soulxsinger.models.modules.whisper_encoder": SimpleNamespace(WhisperEncoder=object),
    }
    source, reference, output = tmp_path / "source.wav", tmp_path / "reference.wav", tmp_path / "output.wav"
    # A full-length source and short reference must retain their different roles.
    def read(path, **kwargs):
        assert kwargs == {"dtype": "float32", "always_2d": True}
        return np.ones((44100 * (60 if path == str(source) else 5), 2), dtype=np.float32), 44100
    modules["soundfile"] = SimpleNamespace(read=read, write=lambda *args, **kw: observed["writes"].append((args, kw)))
    for name, module in modules.items(): monkeypatch.setitem(sys.modules, name, module)
    f0 = tmp_path / "source/preprocess/tools/f0_extraction.py"
    f0.parent.mkdir(parents=True)
    f0.write_text("import numpy as np\nclass F0Extractor:\n def __init__(self,*a,**kw): pass\n def process(self,path): return np.ones(10,dtype=np.float32)\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    if pitch_shift:
        with pytest.raises(RuntimeError, match="SOULX_SVC_OUTPUT_INVALID"):
            worker.run(tmp_path, source, reference, output)
        assert observed["writes"] == []
        return
    worker.run(tmp_path, source, reference, output)
    infer = observed["infer"]
    assert infer["gt_wav"].data.shape == (1, 44100 * 60)
    assert infer["pt_wav"].data.shape == (1, 44100 * 5)
    assert {key: infer[key] for key in ("auto_shift", "pitch_shift", "n_steps", "cfg", "use_fp16")} == {
        "auto_shift": False, "pitch_shift": 0, "n_steps": 32, "cfg": 1.0, "use_fp16": True}
    assert observed["fp16"] and observed["mel_fp32"]
    assert all(options == {"local_files_only": True} for _, options in observed["local_models"])
    assert observed["writes"][0][0][1].shape == (44100 * 60,)
    assert observed["writes"][0][1] == {"subtype": "FLOAT"}


def test_adapter_rejects_short_converted_output_before_publication(tmp_path, monkeypatch):
    from runtime.media import voice_conversion as vc
    source, reference, output = [tmp_path / name for name in ("vocals.wav", "reference.wav", "output.wav")]
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    output.write_bytes(b"previous-success")
    monkeypatch.setattr(vc, "_runtime", lambda _env: (tmp_path, Path(sys.executable)))
    def run(command, *_a, **_kw):
        Path(command[command.index("--output") + 1]).write_bytes(b"truncated")
    monkeypatch.setattr(vc, "_run", run)
    monkeypatch.setattr(vc, "_duration", lambda path, _ff: 60.0 if path == source else 5.0)
    with pytest.raises(vc.VoiceConversionError, match="SOULX_SVC_DURATION_MISMATCH"):
        vc.convert_singing_voice(source, reference, output, environment={}, ffmpeg_path=tmp_path / "ffmpeg.exe")
    assert output.read_bytes() == b"previous-success"
    assert list(tmp_path.glob("olivia-vc-*")) == []


def test_mix_receives_only_converted_voice_and_accompaniment(tmp_path, monkeypatch):
    from runtime.media import voice_conversion as vc
    converted, piano, output = [tmp_path / name for name in ("converted.wav", "piano.wav", "mixed.wav")]
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"fixture")
    commands = []
    monkeypatch.setattr(vc, "_duration", lambda *_: 60.0)
    monkeypatch.setattr(vc, "_run", lambda command, *_a, **_kw: commands.append(command))
    vc.mix_song_voice(converted, piano, output, ffmpeg_path=ffmpeg)
    command = commands[0]
    assert [command[index + 1] for index, value in enumerate(command) if value == "-i"] == [str(piano), str(converted)]
    assert "duration=first" in command[command.index("-filter_complex") + 1]
