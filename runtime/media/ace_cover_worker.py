"""Isolated ACE-Step XL worker; all installation paths come from its job request."""
from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import uuid


CAPTION = ("linli_voice, pop ballad, gentle and tender female singing in a natural "
           "low-to-mid vocal register, restrained delivery and a soft chorus. "
           "Pure solo acoustic piano accompaniment. No repeated high-note belting.")


def transcribe_cover_source(source: Path, model_path: Path, ffmpeg: str) -> tuple[str, str]:
    """Read source lyrics locally without loading ACE or generating any music."""
    if not model_path.is_file():
        raise RuntimeError("COVER_LYRICS_REQUIRED")
    import numpy as np
    import torch
    import whisper
    torch.set_num_threads(4)
    model = whisper.load_model(str(model_path), device="cuda")
    try:
        decoded = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-threads", "4",
                                  "-i", str(source), "-f", "f32le", "-ar", "16000", "-ac", "1", "pipe:1"],
                                 capture_output=True, check=True, timeout=180,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = model.transcribe(np.frombuffer(decoded.stdout, dtype=np.float32).copy(), language=None, temperature=0,
                                  condition_on_previous_text=False, word_timestamps=True,
                                  hallucination_silence_threshold=2)
        lines = [segment["text"].strip() for segment in result.get("segments", [])
                 if segment.get("no_speech_prob", 0) < .6 and segment.get("avg_logprob", 0) > -1]
        lyrics = "\n".join(line for line in lines if line)
        if not lyrics:
            raise RuntimeError("COVER_LYRICS_REQUIRED")
        return lyrics, result.get("language", "unknown")
    finally:
        del model
        gc.collect()
        torch.cuda.empty_cache()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    job = args.request.parent

    if request.get("operation") == "transcribe":
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        lyrics, language = transcribe_cover_source(Path(request["source"]), Path(request["asr_model"]), request["ffmpeg"])
        Path(request["output"]).write_text(json.dumps({"lyrics": lyrics, "language": language}, ensure_ascii=False), encoding="utf-8")
        return

    def progress(stage: str, **fields) -> None:
        partial = job / "progress.partial.json"
        partial.write_text(json.dumps({"stage": stage, "updated_at": time.time(), **fields}), encoding="utf-8")
        partial.replace(job / "progress.json")

    progress("loading")
    root = Path(request["runtime_root"])
    os.environ.update(HF_HOME=str(job / "hf-cache"), HF_HUB_OFFLINE="1",
                      TRANSFORMERS_OFFLINE="1", ACESTEP_CHECKPOINTS_DIR=str(root / "checkpoints"),
                      ACESTEP_VAE_DECODE_CHUNK_SIZE="64")
    sys.path.insert(0, str(root / "source"))
    import numpy as np
    import soundfile as sf
    import torch
    from peft import PeftModel
    from torchao.quantization import Int8WeightOnlyConfig, quantize_
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    torch.set_num_threads(4)
    if not torch.cuda.is_available():
        raise RuntimeError("COVER_CUDA_UNAVAILABLE")
    source = Path(request["source"])
    info = sf.info(source)
    duration = info.frames / info.samplerate
    if duration <= 0:
        raise RuntimeError("COVER_AUDIO_INVALID")
    lyrics = request.get("lyrics", "").strip()
    language = request.get("language", "unknown")
    if not lyrics:
        progress("transcribing")
        lyrics, language = transcribe_cover_source(source, Path(request["asr_model"]), request["ffmpeg"])
    progress("loading_model")
    handler = AceStepHandler()
    status, ready = handler.initialize_service(
        project_root=str(root), config_path="acestep-v15-xl-sft", device="cuda",
        use_flash_attention=False, compile_model=False, offload_to_cpu=True,
        offload_dit_to_cpu=True, quantization=None, use_mlx_dit=False)
    if not ready:
        raise RuntimeError("COVER_MODEL_LOAD_FAILED")
    handler.text_encoder.to(dtype=torch.bfloat16)
    handler.model.decoder = PeftModel.from_pretrained(
        handler.model.decoder.to("cpu"), request["voice_lora"], is_trainable=False).to(dtype=torch.bfloat16)
    handler.model.eval()
    layers = [m for m in handler.model.decoder.modules() if hasattr(m, "lora_A") and len(m.lora_A)]
    if not layers:
        raise RuntimeError("COVER_LORA_NOT_LOADED")
    for layer in layers:
        layer.scale_layer(1.0)
    quantize_(handler.model.decoder, Int8WeightOnlyConfig(), filter_fn=lambda m, n:
              isinstance(m, torch.nn.Linear) and not {"lora_A", "lora_B"}.intersection(n.split(".")))
    quantized = [m for m in handler.model.decoder.modules()
                 if isinstance(m, torch.nn.Linear) and hasattr(m.weight, "tensor_impl")]
    if not quantized or not all(m.weight.tensor_impl.get_plain()[0].dtype == torch.int8 for m in quantized):
        raise RuntimeError("COVER_INT8_NOT_LOADED")
    if not all(m.lora_A["default"].weight.dtype == torch.bfloat16 for m in layers):
        raise RuntimeError("COVER_LORA_DTYPE_INVALID")
    params = GenerationParams(
        task_type="cover", src_audio=str(source), reference_audio=request["reference"],
        caption=CAPTION, lyrics=lyrics, vocal_language=language, duration=duration,
        audio_cover_strength=.8, cover_noise_strength=.08, inference_steps=50,
        guidance_scale=7., shift=1., infer_method="ode", sampler_mode="euler", seed=200717,
        thinking=False, use_cot_metas=False, use_cot_caption=False,
        use_cot_language=False, use_cot_lyrics=False, dcw_enabled=False)
    params.instruction = handler.generate_instruction(task_type="cover")
    (job / "generation.private.json").write_text(json.dumps(params.to_dict(), ensure_ascii=False), encoding="utf-8")
    progress("generating", duration_seconds=duration)
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    raw_output = job / "raw" / uuid.uuid4().hex
    with torch.inference_mode():
        result = generate_music(handler, None, params, GenerationConfig(
            batch_size=1, use_random_seed=False, seeds=[200717], audio_format="flac"), save_dir=str(raw_output),
            progress=lambda value, **_: progress("decoding" if value >= .8 else "generating", duration_seconds=duration))
    if not result.success:
        print(str(result.error), file=sys.stderr, flush=True)
        raise RuntimeError("COVER_GENERATION_FAILED")
    files = list(raw_output.rglob("*.flac"))
    if len(files) != 1:
        raise RuntimeError("COVER_OUTPUT_INVALID")
    audio, rate = sf.read(files[0], always_2d=True, dtype="float32")
    if not len(audio) or not np.isfinite(audio).all():
        raise RuntimeError("COVER_OUTPUT_INVALID")
    # Decode validation only; no content-based rejection or regeneration.
    sf.write(request["output"], audio, rate, subtype="PCM_16")
    progress("completed", duration_seconds=len(audio) / rate,
             generation_seconds=time.monotonic() - started,
             peak_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
             quantized_layers=len(quantized), lora_layers=len(layers),
             lyrics_source="provided" if request.get("lyrics") else "asr_unverified")


if __name__ == "__main__":
    main()
