# Local cover runtime

New music replies use source-conditioned ACE-Step 1.5 XL. Upload an audio file
before sending a singing or speech-and-singing reply. The independent video
switch controls delivery, not music content. TTS2250 is unchanged.

The default runtime is relative to the installation's local data root:

```
capabilities/ace-cover/
  source/acestep/
  checkpoints/acestep-v15-xl-sft/
  checkpoints/                   # Other ACE text encoder and VAE assets
  checkpoints/large-v3-turbo.pt  # Optional if source lyrics are supplied
  runtime/python.exe
  voice/checkpoint-5000/adapter_config.json
  voice/checkpoint-5000/adapter_model.safetensors
  reference/reference-vocals-30s.wav
```

Installers may override `OLIVIA_ACE_ROOT`, `OLIVIA_ACE_PYTHON`,
`OLIVIA_ACE_VOICE_LORA`, `OLIVIA_ACE_REFERENCE`, and `OLIVIA_ACE_ASR_MODEL`.
Relative overrides resolve against `OLIVIA_PROJECT_ROOT`, never the shell's
working directory. No developer drive or experiment directory is a default.
Use the tested upstream revision `ca1e85fe9430179831e6bc6be790c332190a3866`.
The runtime needs torch, torchao, PEFT, soundfile and the upstream ACE dependencies;
automatic source transcription additionally needs local Whisper weights.
Install `runtime/media/requirements-ace-cover-asr.txt` into that runtime for
source transcription; the initial ACE experiment environment did not contain it.
Existing MiniMax offline bundles do not contain these new assets.

Uploads are decoded locally to `cover-inputs/<opaque-id>/source.wav`. Only the ID,
source lyrics, and language are attached to the letter. No source means no music
generation, and no fallback to text-to-music. Duration comes from the decoded
audio without a 300-second truncation. Source lyrics can be supplied, or locally
transcribed; transcription is not a promise of correct lyrics. Failed/empty
transcription requests source lyrics instead of inventing them.

Generation uses cover strength 0.8, noise 0.08, 50 steps, guidance 7, shift 1,
ODE/Euler and seed 200717. Only the timbre LoRA is loaded at scale 1.0; decoder
base linear weights are INT8, LoRA weights BF16, with CPU/DiT offload. Auxiliary
LM, CoT metadata and artist/style LoRAs are disabled. No initial separation or
second voice-conversion pass is performed. Video delivery separates the finished
cover for the existing lip-sync renderer's vocal conditioning; playback retains
the untouched cover mix. Pure audio delivery needs no separator or lip-sync runtime.

`media/<output-stem>-cover-stages` holds local progress and private provider
artifacts. Failed generation is not automatically retried. A completed cover
with matching source/reference/LoRA/worker fingerprints is reused if a later
video stage fails. The external worker has a 30-minute process-tree timeout.
The public progress response contains only stage/status/error codes. Raw source
lyrics and worker logs are private files and must not be added to public diagnostics.

Validation: the product audio renderer completed a 262-second source-conditioned
cover. A 15-second excerpt then passed real output-vocal separation (about 16 s),
lip-sync and composition (about 130 s), and full video decode. The full original
audio also passed the local transcription path (about 25 s, lyrics not manually
verified). Combined-mode dispatch is covered by stage tests. New model assets
have not yet been packaged/published with this integration.
