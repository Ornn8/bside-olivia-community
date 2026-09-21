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
from contextlib import contextmanager


@contextmanager
def singing_only(decoder, enabled):
    """The original rank-128 adapter concatenates structure then singing (64 each)."""
    if not enabled:
        yield
        return
    import torch
    saved = []
    try:
        with torch.no_grad():
            for layer in decoder.modules():
                for name in decoder.active_adapters:
                    if hasattr(layer, 'lora_B') and name in layer.lora_B:
                        weight = layer.lora_B[name].weight
                        if weight.shape[1] != 128:
                            raise RuntimeError('ORIGINAL_ADAPTER_LAYOUT_INVALID')
                        saved.append((weight, weight[:, :64].clone()))
                        weight[:, :64].zero_()
        if not saved:
            raise RuntimeError('ORIGINAL_ADAPTER_NOT_LOADED')
        yield
    finally:
        with torch.no_grad():
            for weight, original in saved:
                weight[:, :64].copy_(original)


CAPTION = ("linli_voice, pop ballad, gentle and tender female singing in a natural "
           "low-to-mid vocal register, restrained delivery and a soft chorus. "
           "Pure solo acoustic piano accompaniment. No repeated high-note belting.")


def check_offline_assets(root: Path) -> None:
    """Validate only this pipeline's XL, text encoder and VAE; never install at request time."""
    checkpoints = root / 'checkpoints'
    model = checkpoints / 'acestep-v15-xl-sft'
    try:
        index = json.loads((model / 'model.safetensors.index.json').read_text(encoding='utf-8'))
        shards = set(index['weight_map'].values())
        if not shards or any(Path(name).name != name for name in shards):
            raise ValueError('Invalid shards')
        required = [model / name for name in shards]
        required += [model / 'config.json', model / 'silence_latent.pt',
                     checkpoints / 'Qwen3-Embedding-0.6B/model.safetensors',
                     checkpoints / 'Qwen3-Embedding-0.6B/config.json',
                     checkpoints / 'vae/diffusion_pytorch_model.safetensors',
                     checkpoints / 'vae/config.json']
        if any(not path.is_file() or path.stat().st_size == 0 for path in required):
            raise ValueError('Missing assets')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('ACE_OFFLINE_ASSETS_MISSING') from exc


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


def load_models(root, request, *, resident=False):
    import torch
    from peft import PeftModel
    from torchao.quantization import Int8WeightOnlyConfig, quantize_
    from acestep.handler import AceStepHandler
    check_offline_assets(root)
    handler = AceStepHandler()
    # The upstream check also downloads default Turbo/LM models unused by this
    # fixed XL pipeline. Required files were validated above; stay offline.
    handler._ensure_models_present = lambda **kwargs: None
    status, ready = handler.initialize_service(
        project_root=str(root), config_path="acestep-v15-xl-sft", device="cuda",
        use_flash_attention=False, compile_model=False, offload_to_cpu=True,
        offload_dit_to_cpu=True, quantization=None, use_mlx_dit=False)
    if not ready:
        raise RuntimeError("COVER_MODEL_LOAD_FAILED")
    handler.text_encoder.to(dtype=torch.bfloat16)
    handler.model.decoder = PeftModel.from_pretrained(
        handler.model.decoder.to("cpu"), request["voice_lora"], is_trainable=False).to(dtype=torch.bfloat16)
    if request.get('_resident_adapters'):
        for name,path in request['_resident_adapters'].items():
            handler.model.decoder.load_adapter(path,adapter_name=name,is_trainable=False)
        handler.model.decoder.to(dtype=torch.bfloat16)
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
    if resident:
        handler.offload_to_cpu=False
        handler.offload_dit_to_cpu=False
        handler.model.to('cuda')
        handler.text_encoder.to('cuda')
        handler.vae.to('cuda')
    return handler, quantized, layers


def run(request_path, *, cache=None, resident_options=None):
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    request.update(resident_options or {})
    job = Path(request_path).parent

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
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    torch.set_num_threads(4)
    if not torch.cuda.is_available():
        raise RuntimeError("COVER_CUDA_UNAVAILABLE")
    task_type = request.get("task_type", "cover")
    if task_type not in {"cover", "text2music"}:
        raise RuntimeError("ACE_TASK_INVALID")
    source = Path(request["source"]) if task_type == "cover" else None
    if source is not None:
        info = sf.info(source)
        duration = info.frames / info.samplerate
    else:
        duration = request["duration"]
        if duration != 110 or not request.get("lyrics", "").strip():
            raise RuntimeError("ORIGINAL_PLAN_INVALID")
    if duration <= 0:
        raise RuntimeError("COVER_AUDIO_INVALID")
    lyrics = request.get("lyrics", "").strip()
    language = request.get("language", "unknown")
    if not lyrics:
        progress("transcribing")
        lyrics, language = transcribe_cover_source(source, Path(request["asr_model"]), request["ffmpeg"])
    progress("loading_model")
    if cache is not None and 'models' in cache:
        handler,quantized,layers=cache['models']
    else:
        handler,quantized,layers=load_models(root,request,resident=cache is not None)
        if cache is not None:cache['models']=(handler,quantized,layers)
    if cache is not None:
        handler.model.decoder.set_adapter(request.get('_resident_adapter','default'))
    needs_lm = task_type == 'text2music' and any(request.get(k, False) for k in
        ('thinking', 'use_cot_metas', 'use_cot_caption', 'use_cot_language'))
    music_lm = None
    lm_path = root / 'checkpoints' / 'acestep-5Hz-lm-1.7B'
    if needs_lm or (request.get('_preload_only') and lm_path.is_dir()):
        if cache is not None and 'music_lm' in cache:
            music_lm = cache['music_lm']
        else:
            if not lm_path.is_dir():
                raise RuntimeError('ORIGINAL_PLANNING_MODEL_MISSING')
            from acestep.llm_inference import LLMHandler
            music_lm = LLMHandler()
            _, ready = music_lm.initialize(checkpoint_dir=str(root / 'checkpoints'),
                lm_model_path=lm_path.name, backend='pt', device='cuda',
                offload_to_cpu=False, dtype=torch.bfloat16)
            if not ready:
                raise RuntimeError('ORIGINAL_PLANNING_MODEL_LOAD_FAILED')
            if cache is not None:
                cache['music_lm'] = music_lm
    if cache is not None and request.get('_preload_only'):
        progress('ready')
        return
    params = GenerationParams(
        task_type=task_type, src_audio=str(source) if source is not None else None, reference_audio=request["reference"],
        caption='' if task_type == 'cover' else request.get("caption", CAPTION), lyrics=lyrics, vocal_language=language, duration=duration,
        **({key: request[key] for key in ("bpm", "keyscale", "timesignature")} if task_type == "text2music" else {}),
        audio_cover_strength=request.get('audio_cover_strength', .6) if task_type == 'cover' else .8,
        cover_noise_strength=request.get('cover_noise_strength', .25) if task_type == 'cover' else .08, inference_steps=50,
        guidance_scale=7., shift=1., infer_method="ode", sampler_mode="euler", seed=200717,
        thinking=False, use_cot_metas=False, use_cot_caption=False,
        use_cot_language=False, use_cot_lyrics=False, dcw_enabled=False)
    if task_type == 'text2music':
        for name, default in dict(guidance_scale=7., inference_steps=50, seed=200717,
                thinking=False, use_cot_metas=False, use_cot_caption=False,
                use_cot_language=False, use_adg=True).items():
            setattr(params, name, request.get(name, default))
    params.instruction = handler.generate_instruction(task_type=task_type)
    (job / "generation.private.json").write_text(json.dumps(params.to_dict(), ensure_ascii=False), encoding="utf-8")
    progress("generating", duration_seconds=duration)
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    raw_output = job / "raw" / uuid.uuid4().hex
    with singing_only(handler.model.decoder, task_type == 'text2music'), torch.inference_mode():
        torch.manual_seed(params.seed)
        torch.cuda.manual_seed_all(params.seed)
        result = generate_music(handler, music_lm if needs_lm else None, params, GenerationConfig(
            batch_size=1, use_random_seed=False, seeds=[params.seed], audio_format="flac"), save_dir=str(raw_output),
            progress=lambda value, *args, **_: progress("decoding" if value >= .8 else "generating", duration_seconds=duration))
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
             lora_enabled=True,
             lyrics_source="provided" if request.get("lyrics") else "asr_unverified")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('request',type=Path)
    args=parser.parse_args()
    run(args.request)


if __name__ == "__main__":
    main()
