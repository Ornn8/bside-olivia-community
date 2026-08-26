# B05 Local Streaming ASR

Status: contract and offline adapter implemented; a real fixed Windows CUDA
runtime acceptance run is now recorded in ignored external evidence.  The
default provider remains the truthful text fallback, and native ASR becomes
`AVAILABLE` only when the configured runtime/model and acceptance manifest
pass the provider gate.  No runtime binary, CUDA tree, or model weights are
stored in this worktree.

## Selected supply chain

| Component | Fixed source/version | License | Native streaming / platform evidence | Decision |
| --- | --- | --- | --- | --- |
| NVIDIA Nemotron 3.5 ASR Streaming 0.6B | [HF model](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b), commit `1c8deaecc64b91f034d73e08dd8b64625eb3395d`; GGUF SHA-256 `a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae` | OpenMDW-1.1 | Model card documents cache-aware streaming and configurable 80/160/320/560/1120 ms chunks. [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) documents local WebSocket, Windows source build and CUDA build. The pinned RTX 3080 run is recorded in ignored evidence. | Selected; native status is manifest-gated. |
| NVIDIA NeMo-Speech.cpp runtime | [GitHub](https://github.com/NVIDIA/NeMo-Speech.cpp), official commit `1118951337094db3b362fbf1b27e871696f10590` | Apache-2.0 | Official docs specify `scripts/windows/build.ps1 -Backend cuda -Http`, `/ready`, and `/v1/realtime`. The repository had no published release at selection time; the full commit is therefore pinned and the no-release risk is recorded. | Runtime for the selected model. |
| Qwen3-ASR 0.6B | [HF](https://huggingface.co/Qwen/Qwen3-ASR-0.6B); official [GitHub](https://github.com/QwenLM/Qwen3-ASR) commit `7c6daf77a2421100f5fb066495372c00129d39ff` | Apache-2.0 | Official documentation limits streaming to vLLM and does not provide timestamps in streaming mode; Windows/RTX 3080 native path was not verified. | Rejected for this contract. |
| Parakeet Realtime EOU 120M v1 | [HF model](https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1) | NVIDIA Open Model License | Native cache-aware stream, but English-only, no punctuation/capitalization, and the product Windows path was not verified. | Rejected for language/product scope. |

### OpenMDW-1.1 decision

The [official license](https://openmdw.ai/license/1-1/) grants free-of-charge,
unrestricted dealing in the Model Materials, including copying, modification,
and distribution.  Redistribution must retain the license and copyright/origin
notices.  The license places no restriction on model outputs and has no
copyleft/share-alike requirement.  Its patent/copyright litigation termination
clause and the user’s responsibility to obtain data/model rights remain
material conditions.

The [official FAQ](https://openmdw.ai/faq/) describes OpenMDW-1.1 as a
permissive open-source ML-artifact license with no field, royalty, or geographic
restriction, but notes that version 1.1 is not currently on the SPDX License
List; use `LicenseRef-OpenMDW-1.1` where an SPDX identifier is required.  On
that basis it satisfies this project’s “open-source model” gate for use,
modification, and redistribution, provided the license/NOTICE and rights
provenance are retained.  This is a permissive-license decision, not a claim
that third-party training data rights are automatically cleared.

### Portable CUDA 13.3 build toolchain

The portable build input is NVIDIA's official CUDA 13.3 redistributable
manifest, `redistrib_13.3.0.json`, read from an ignored local transfer root,
for example `.evidence/b05-runtime-transfer/redistrib_13.3.0.json`.
The verified manifest is 47,431 bytes with SHA-256
`507EDDAAB1360336BC0FE17B77552E0B7DFE1E74DA888671C3A2F5FAD7775DB1`.
The manager performs no download: it accepts any non-root local absolute
Windows drive path as the transfer root, checks size, SHA-256, ZIP CRC, and
safe members, then assembles an owned prefix under the selected local root.

The static Windows x86_64 compile closure is the following.  It is deliberately
recorded as a build-toolchain closure, not as native ASR acceptance evidence.

| Package | Version | License | Manifest relative path | Size | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| `cccl` | 13.3.3.3.1 | CCCL EULA | `cccl/windows-x86_64/cccl-windows-x86_64-13.3.3.3.1-archive.zip` | 3,544,189 | `607dcfca31da168171fbdae5b7096ade646c4c2b1e0ff2899077dde0ccbdd6fb` |
| `cuda_crt` | 13.3.33 | CUDA Toolkit | `cuda_crt/windows-x86_64/cuda_crt-windows-x86_64-13.3.33-archive.zip` | 163,110 | `752c528281a06a0ddf89237d760ffd6acde1b9cd59efc35803c2591127ef55f0` |
| `cuda_ctadvisor` | 13.3.33 | CUDA Toolkit | `cuda_ctadvisor/windows-x86_64/cuda_ctadvisor-windows-x86_64-13.3.33-archive.zip` | 863,843 | `8f23724fdc96b7ed12f0c5981f304a500bdcba8532c779d91e861b6d6b34a71b` |
| `cuda_cudart` | 13.3.29 | CUDA Toolkit | `cuda_cudart/windows-x86_64/cuda_cudart-windows-x86_64-13.3.29-archive.zip` | 2,589,792 | `1feb7dd266813ffe8dbc24e115183a5ac35a4795c8d34aca0df85ab616b64d9c` |
| `cuda_nvcc` | 13.3.33 | CUDA Toolkit | `cuda_nvcc/windows-x86_64/cuda_nvcc-windows-x86_64-13.3.33-archive.zip` | 31,959,263 | `8fed1ab69ed4e637ad76baff572579630674df9ff02570777800782ee5bdfbc5` |
| `libcublas` | 13.5.1.27 | CUDA Toolkit | `libcublas/windows-x86_64/libcublas-windows-x86_64-13.5.1.27-archive.zip` | 391,055,517 | `c946e1c825e05895747a95ed4fee18030b08052c09783b9b7b19818fd2e31f58` |
| `libnvfatbin` | 13.3.29 | CUDA Toolkit | `libnvfatbin/windows-x86_64/libnvfatbin-windows-x86_64-13.3.29-archive.zip` | 2,312,812 | `95197dc49b931b2c0fa8bbd30dbb65a9ceb22a8b7af1f84994d6aadd868763a1` |
| `libnvjitlink` | 13.3.33 | CUDA Toolkit | `libnvjitlink/windows-x86_64/libnvjitlink-windows-x86_64-13.3.33-archive.zip` | 274,601,597 | `43bc22509507c138c86885191bb2709b5d23506ea6abdc8bc64d9960e2b63363` |
| `libnvptxcompiler` | 13.3.33 | CUDA Toolkit | `libnvptxcompiler/windows-x86_64/libnvptxcompiler-windows-x86_64-13.3.33-archive.zip` | 48,105,367 | `7bc5ffd885fb96b07fd8a601a3a7ebe06612730ca5453a6dda9197a848e84998` |
| `libnvvm` | 13.3.33 | CUDA Toolkit | `libnvvm/windows-x86_64/libnvvm-windows-x86_64-13.3.33-archive.zip` | 58,567,644 | `e8e48fcceb3ffeb3e421f29fc40252580c6dfd2a841bea3490782233048a5f00` |

The closure is derived from the fixed source's CUDA CMake references to
`CUDA::cudart`, `CUDA::cublas`, and `CUDA::cublasLt`, plus NVCC's compiler,
device-link, CRT, NVVM, CCCL, and `ctadvisor` inputs.  `cuda_nvrtc`, cuFFT,
cuRAND, cuSOLVER, cuSPARSE, NPP, NVTX, profilers, documentation, and the
driver are excluded because the fixed ASR/CUDA source has no hard reference;
the driver remains an installed host prerequisite.  The archive contents have
now been verified, but the closure remains subject to the real build's linker
requirements.  A successful portable-prefix check is never promoted to
`native.asr=AVAILABLE`.

The fixed runtime source is NeMo-Speech.cpp commit
`1118951337094db3b362fbf1b27e871696f10590` (Apache-2.0; no release tag was
available at selection time).  Its required gitlinks are `ggml`
`c03b4e2bcece5134827881af90242086daf75be5` from
`https://github.com/ggml-org/ggml.git` and `third_party/cpp-httplib`
`62d899feac3cf9215a55f2b43da250fdd98d2156` from
`https://github.com/yhirose/cpp-httplib.git`.  The portable management path
copies those fixed sources only from an explicit ignored transfer root.

The current real-build boundary is recorded truthfully: the existing external
VS/MSVC, CMake, Ninja, NVCC 13.3.33, CUDA ABI, and `sm_86` build cache was
incrementally rebuilt only for the affected `nemo_speech_cli` target after the
upstream HTTP patch below.  CUDA and model assets were reused; no duplicate
build or model download was performed.  The resulting executable and model
remain outside Git under the ignored external evidence boundary.

### Fixed upstream HTTP inventory patch

The unpatched runtime had a reproducible ordering bug: `/ready` returned 200,
but `/v1/models` returned HTTP 500 with `resource deadlock would occur`; when
that request came first, the process exited with Windows code `0xC0000409`
before either WebSocket endpoint emitted `session.created`.  A WebSocket-first
run could still stream, which made the failure look like a session problem.

`tools/b05_native_http_snapshot.patch` is a minimal fixed-source snapshot
patch.  It adds one locked `EngineSnapshot` read to `EngineRegistry` and makes
`/v1/models` consume that snapshot instead of taking the registry lock through
several nested getters.  The patch is applied only to the ignored external
source tree; the source diff is retained as the reviewable artifact and the
affected target is rebuilt incrementally.

## Contract and real protocol boundary

The provider contract emits `session`, `ready`, `partial`, `final`, `silence`,
`committed`, `cleared`, `canceled`, `disconnected`, `error`, and `closed`
events.  Session-relative timestamps are monotonic and every event has a
strictly increasing sequence number.  `partial` is the accumulated hypothesis
for the current server item; `final` is the server’s completed transcript.

The production adapter uses `aiohttp` to connect only to a configured loopback
`ws://`/`wss://` server.  It sends little-endian PCM16 binary frames, sends a
documented `session.update`, waits for `session.created` and
`session.updated`, commits with `input_audio_buffer.commit`, and handles
`response.cancel` plus `input_audio_buffer.clear`.  It maps the documented
`conversation.item.input_audio_transcription.delta` and `.completed` events.
There is no loopback fake, prerecorded transcript, offline whole-file
transcription, or chunked-offline implementation behind the native provider.

The text fallback is a separate `text-fallback` capability with `is_asr: false`.
It accepts user text and remains available when native ASR is missing; it never
claims speech recognition or a detected language.

### Native timing boundary for later A/V sync

Native NeMo events may include `audio_processed` in seconds and final
`words[].start`/`words[].end` positions. The B05 adapter preserves these as
session-relative `AsrEvent.audio_ms` and `metadata.word_timestamps` in
milliseconds, with `metadata.audio_timestamp_source=native_audio_processed`.
When an upstream event has no processed-audio position, the adapter retains the
existing session audio position as a truthful fallback; it never invents a
server timestamp. This is the timing interface consumed by later A/V sync and
does not claim visual alignment acceptance by itself.

## Configuration and lifecycle

The default provider is `text-fallback`.  Native selection is explicit through
`ASR_PROVIDER=nemotron-speech-cpp` or the external JSON configuration written by
`tools/asr_manage.py switch --provider nemotron-speech-cpp`.

All runtime, model, cache, and acceptance-evidence roots must be non-root local
absolute Windows drive paths in production. Relative, drive-relative, URL,
UNC, drive-root, and parent-traversal paths are rejected. Installation is an
idempotent offline assembly from an explicit, verified local absolute transfer root: it validates the fixed
NeMo-Speech.cpp/ggml/cpp-httplib revisions, the fixed model SHA-256, and the
official HTTP snapshot patch, then registers the already-built external
runtime.  It does not download weights or invent an ASR implementation; no
weights are vendored. Uninstall requires a matching marker in each managed
runtime/model/cache root, with the exact normalized root and one shared install
operation identity, plus `--apply`; legacy roots without those markers fail
closed. A dry run never deletes external runtime/model assets. The runtime
has no release artifact to pin, so the source commit, upstream submodule pins,
patch, and provenance are part of the install manifest.

Useful commands:

```text
rtk python tools/asr_manage.py status
rtk python tools/asr_manage.py install
rtk python tools/asr_manage.py install --transfer-root "$env:LOCALAPPDATA/BSideOliviaLocal/asr-transfer"
rtk python tools/asr_manage.py install --apply --transfer-root "$env:LOCALAPPDATA/BSideOliviaLocal/asr-transfer"
rtk python tools/asr_manage.py uninstall
rtk python tools/asr_manage.py uninstall --apply
rtk python tools/asr_manage.py cuda-toolchain --action status --cuda-root "$env:LOCALAPPDATA/BSideOliviaLocal/asr/cuda-toolchain"
rtk python tools/asr_manage.py cuda-toolchain --action assemble --apply --cuda-root "$env:LOCALAPPDATA/BSideOliviaLocal/asr/cuda-toolchain" --cuda-manifest "$env:LOCALAPPDATA/BSideOliviaLocal/asr-transfer/redistrib_13.3.0.json" --cuda-transfer-root "$env:LOCALAPPDATA/BSideOliviaLocal/asr-transfer/cuda-13.3-zips"
rtk python tools/asr_manage.py cuda-toolchain --action uninstall --cuda-root "$env:LOCALAPPDATA/BSideOliviaLocal/asr/cuda-toolchain" --apply
rtk python tools/asr_healthcheck.py
rtk python tools/asr_healthcheck.py --probe --require-ready
rtk python tools/healthcheck.py --profile asr
```

The first install command is a plan.  Actual downloads/builds are intentionally
not part of default CI and must remain in an external local absolute directory.

## Truthful readiness gate

`native.asr` is `UNAVAILABLE` until all of these are true:

1. the runtime executable exists and the fixed model exists with its recorded
   SHA-256;
2. the local `/ready` endpoint returns HTTP 200 with JSON `ready: true`;
3. a real native WebSocket round trip has emitted the documented partial/final
   or silence events; and
4. the acceptance manifest records Windows CUDA, the pinned runtime/model
   revisions, `websocket_roundtrip: true`, and an RTX 3080 device.

`ASR_NOT_PROBED`, `ASR_RUNTIME_MISSING`, `ASR_MODEL_MISSING`,
`ASR_MODEL_CORRUPT`, `ASR_NOT_READY`, and `ASR_PROVIDER_UNAVAILABLE` remain
diagnostic states; none is converted into fake ready state.

## Acceptance evidence

Ignored evidence belongs under `.evidence/B05_STREAMING_ASR/` for
worktree-level tests, or under an explicitly configured external evidence root
for runtime acceptance. A complete
acceptance record will include a playable, non-copyright-restricted WAV and
its source/license, absolute path, sample rate, duration, SHA-256, event JSON,
transcript, and metrics.  No original media, private data, model weights, or
secrets may enter Git.

WER is computed after NFKC normalization, case-folding, punctuation/symbol
removal, and whitespace collapse; whitespace-tokenized text uses word tokens,
while no-space text uses characters.  The record must include the reference
transcript and normalization rule.  Metrics include first partial latency,
first final latency, partial-to-final normalized prefix stability, monotonic
timestamps, WER/CER, and RTF.  Required scenarios are silence, short/long audio,
cancel, disconnect, backpressure, missing/corrupt model, unavailable provider,
auto/explicit language, concurrency, and resource release.  RTX 3080 VRAM and
RTF/latency are measured only on the actual machine; without that run the
report says `UNAVAILABLE`.

The latest real low-level record is under the ignored
`.evidence/B05_STREAMING_ASR/native-live-v10-patched-models-first/` and
`native-live-v10-patched-no-models/` directories.  Both request orders returned
HTTP 200 from `/v1/models`, kept the process alive through cleanup, and made
both `/v1/realtime` and `/v1/audio/transcriptions/realtime` emit
`session.created`, 35 partial events, and a Chinese final.  The reference
score was CER 0.10 and WER 0.10.  Across the two orders, primary/alias first
partial latency was 599--652/45--47 ms and final latency was 1015--1068/465--467
ms; peak observed VRAM was 3198 MiB.  Explicit `zh-CN` cancellation produced
`input_audio_buffer.cleared`, disconnect closed before commit, and two seconds
of silence completed with an empty transcript.  A live provider queue with
`max_queue_chunks=1` and zero timeout saturated and returned 32
`ASR_BACKPRESSURE` errors in the models-first record.  The candidate gate
requires CER/WER <= 0.20, a Chinese final, empty silence, and that real queue
signal.  The generated
`evidence/native_acceptance.json` records the fixed revisions, pinned model
hash, CUDA/RTX 3080 device, HTTP statuses, controls, and WebSocket round trip;
the provider hashes the model itself, so a sidecar is optional but cannot
override the pinned SHA.  It is ignored and is not a committed model/runtime
artifact.
