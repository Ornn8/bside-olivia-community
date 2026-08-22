# B06 local TTS acceptance

This note is a sanitized acceptance record. Private model directories, reference audio, generated WAV files, and raw provider logs remain local-only and are not part of this note.

## Verified scope

- Branch: `codex/b06-local-tts`
- Baseline at verification: `0ddfa281`
- Provider: local CosyVoice3 `Fun-CosyVoice3-0.5B-2512`
- License evidence: local runtime `LICENSE` begins with Apache License; local model README declares `license: apache-2.0`.
- B05 boundary: no B05 HTTP/event wiring or integrated claim was added.
- Asset policy: profile install records external references only; `external_assets_copied=false` and lifecycle uninstall dry-run reported `external_assets_deleted=false`.

## Final real gate

One ordinary PowerShell invocation performed profile install and then one-process real acceptance. The provider used `text_frontend=none`, offline flags, D:-drive TEMP/TMP/Numba cache, and local-only model/reference assets.

Final gate result: `PASS`, `fail_count=0`, `skip_count=0`.

All gates were true:

- provider health and Apache-2.0 registration
- short sentence request
- long sentence request (3 sentence units)
- cancel after first audio packet
- continuous request 1
- continuous request 2 after cancellation
- semantic ASR
- CUDA availability
- GPU resource sampling

## Audio and packet evidence

All completed outputs were verified as mono PCM16 WAV, 24 kHz, non-empty, unclipped, and not truncated.

| case | sentence/chunk count | duration | first packet ms | last packet ms | terminal ms | peak dBFS | silence ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| short | 2 / 4 | 13.28 s | 23188.761 | 34086.289 | 34259.879 | -0.2662 | 0.33769767 |
| long | 3 / 7 | 33.28 s | 7287.147 | 29873.634 | 30264.940 | -2.4713 | 0.28657602 |
| continuous 1 | 1 / 2 | 9.36 s | 6638.664 | 8398.563 | 8526.150 | -2.6831 | 0.41920406 |
| continuous 2 | 1 / 2 | 9.80 s | 6242.979 | 8211.519 | 8342.742 | -3.3289 | 0.36046344 |

Every completed WAV had `clipped_samples=0`, `has_audio=true`, `truncated=false`; leading/trailing silence was measured rather than hidden. The cancel case emitted one real packet at `6294.958 ms`, returned `status=cancelled` / `error_code=TTS_CANCELLED`, and did not write a WAV.

Semantic ASR used the already-local Whisper `base` checkpoint and in-memory PCM16 WAV decoding/resampling. It returned `PASS` on the required semantic tokens `本地` and `测试` (traditional/simplified equivalence normalized); ASR device was CUDA.

## RTX 3080 evidence

- `nvidia-smi`: NVIDIA GeForce RTX 3080, 10,240 MiB total.
- Peak sampled memory: 7,624 MiB; peak GPU utilization: 93%; peak temperature: 49 C.
- Torch: CUDA available, peak allocated 5,050.62 MiB, peak reserved 5,452 MiB.
- The runtime also reported that its ONNXRuntime speech-tokenizer execution provider was CPU-only in this environment. The acceptance claim is therefore Torch/CUDA model execution on RTX 3080, not an unverified claim that every auxiliary subcomponent used CUDA.

## Lifecycle and fallback

Real profile lifecycle evidence covered `doctor` (`HEALTHY`), `disable`, disabled synthesis (`text_fallback`, `TTS_DISABLED`), `customize` (`speed=1.1`), `enable`, and uninstall dry-run. Missing/disabled providers remain truthful unavailable/fallback states; no text fallback is emitted for cancellation.

## Tests and boundary

- `tests/tts`: `5 passed`.
- Required-ci targeted pytest (`governance`, `http`, `packaging`, `tts`, baseline tests): `67 passed`, `failures=0`, `errors=0`, `skipped=0`.
- Required-ci full pytest: `140 passed`, `failures=0`, `errors=0`, `skipped=0`.
- Required-ci collection: `140 collected`.
- `compileall`, baseline hardening `all` plus every individual mode, P01 hardening scan, project status, core health, and `git diff --check`: all PASS.
- B02, B04, B06, P01, and B10A scope scanners: all PASS with B06 composition enabled. Clean-state simulation PASS; unrelated dirty-path simulation FAIL; forced B06 failure propagates FAIL to P01 and B10A.

No model weights, reference audio, generated media, credentials, or absolute private asset paths are included in this note.
