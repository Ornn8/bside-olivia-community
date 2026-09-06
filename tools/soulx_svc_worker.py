"""Offline FP16 adapter for pinned SoulX-Singer-SVC; no upstream file mutation."""
from __future__ import annotations

import argparse
import gc
import importlib.util
import os
from pathlib import Path
import sys


def run(root: Path, source: Path, reference: Path, output: Path) -> None:
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1")
    sys.path.insert(0, str(root / "source"))
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio
    from omegaconf import OmegaConf
    from transformers import WhisperFeatureExtractor, WhisperModel
    from soulxsinger.models import soulxsinger_svc as svc
    from soulxsinger.models.modules.whisper_encoder import WhisperEncoder

    if not torch.cuda.is_available():
        raise RuntimeError("SOULX_SVC_CUDA_UNAVAILABLE")

    class LocalWhisperEncoder(WhisperEncoder):
        def __init__(self, device=None):
            self.fe = WhisperFeatureExtractor.from_pretrained(
                root / "models/whisper-base", local_files_only=True)
            self.model = WhisperModel.from_pretrained(
                root / "models/whisper-base", local_files_only=True).to(device or "cpu")

    svc.WhisperEncoder = LocalWhisperEncoder
    config = OmegaConf.load(root / "source/soulxsinger/config/soulxsinger.yaml")
    spec = importlib.util.spec_from_file_location(
        "soulx_f0", root / "source/preprocess/tools/f0_extraction.py")
    f0_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(f0_module)
    # Release the pitch extractor before allocating the singing model on GPU.
    extractor = f0_module.F0Extractor(
        str(root / "models/preprocess/rmvpe/rmvpe.pt"), device="cuda",
        is_half=False, target_sr=config.audio.sample_rate,
        hop_size=config.audio.hop_size, verbose=False)
    source_f0 = extractor.process(str(source))
    reference_f0 = extractor.process(str(reference))
    del extractor
    gc.collect()
    torch.cuda.empty_cache()

    model = svc.SoulXSingerSVC(config)
    checkpoint = torch.load(root / "models/official/model-svc.pt",
                            map_location="cpu", weights_only=False)
    model.load_state_dict(checkpoint["state_dict"], strict=True)
    del checkpoint
    gc.collect()
    model.half()
    model.mel.float()
    model = model.to("cuda").eval()
    model.whisper_encoder.model.to("cuda")
    torch.manual_seed(200717)
    torch.cuda.manual_seed_all(200717)

    def load(path):
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
        tensor = torch.from_numpy(audio.mean(axis=1)).unsqueeze(0)
        if rate != config.audio.sample_rate:
            tensor = torchaudio.functional.resample(tensor, rate, config.audio.sample_rate)
        return tensor.to("cuda")

    with torch.inference_mode():
        generated, shift = model.infer(
            pt_wav=load(reference), gt_wav=load(source),
            pt_f0=torch.from_numpy(reference_f0).unsqueeze(0).to("cuda"),
            gt_f0=torch.from_numpy(source_f0).unsqueeze(0).to("cuda"),
            auto_shift=False, pitch_shift=0, n_steps=32, cfg=1.0, use_fp16=True)
    audio = generated.squeeze().float().cpu().numpy()
    if shift != 0 or not np.isfinite(audio).all() or not audio.size or np.max(np.abs(audio)) == 0:
        raise RuntimeError("SOULX_SVC_OUTPUT_INVALID")
    # Preserve unclipped floating-point samples for the final two-stem mix.
    sf.write(str(output), audio, config.audio.sample_rate, subtype="FLOAT")


def main():
    parser = argparse.ArgumentParser()
    for key in ("runtime", "source", "reference", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    run(args.runtime, args.source, args.reference, args.output)


if __name__ == "__main__":
    main()
