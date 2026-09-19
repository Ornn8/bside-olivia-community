# B08 Live Orchestration

B08 is the provider-neutral live-session composition boundary. Its accepted
current-main baseline is `5444781c673a1d77fc5835a79d3221e66d37061c`; the older
`49fe48dbf85cb4a79712836f72544f10bb636468` ref is retained only for historical
scope evidence. B08 does not replace an upstream runtime or create a model
engine.

## Composition boundary

`live/environment.py` is the composition root. It calls the existing public
factories and interfaces:

- B03: `load_gateway_config` and `create_gateway` from `llm_gateway`.
- B04: `load_memory_config`, `create_memory_adapter`, `MemoryPort`, and
  the existing persona evidence ports.
- B05: `AsrConfig` and `create_provider`.
- B06: `TTSConfig`, `TTSProfileManager`, and `TTSService`.
- B07: `VisualDriver` and its original-frame fallback contract.

The root only reads configuration and constructs adapters. It does not
download assets, probe a model, start a network client, write audio/video/frame
data, or reimplement any upstream provider. Construction failures are
component-local and fail closed with a sanitized reason code.

## Session lifecycle

`LiveService` owns sessions; one `LiveSession` owns its active turn and
bounded event queue. The public terminal paths are:

| Condition | Result | Fallback |
| --- | --- | --- |
| LLM completes | `completed` | B06 TTS and B07 visual output remain optional |
| LLM unavailable/error | `degraded` | safe static text |
| ASR is missing/unready | `text_fallback` | caller uses text input |
| TTS is missing/unready/canceled | completed text turn with text fallback | text output |
| Visual driver is missing/unready | completed turn with visual fallback | original static frame/clip |
| cancel, interrupt, disconnect, or timeout | explicit terminal result/event | no half-open turn |
| bounded event queue overflows | `LIVE_BACKPRESSURE` terminal visibility | dropped-oldest metadata only |

`cancel_turn`, `interrupt`, and `close` await the active task and
connector cleanup. ASR sessions, LLM reply runs, and TTS runs are canceled
before the turn becomes terminal; repeated service stop is idempotent.

## Truthful readiness and privacy

`LiveService.health()` returns `READY`, `DEGRADED`, or `UNAVAILABLE` plus
a boolean `ready`. `READY` is emitted only for an actually available
component: mock LLM is ready, while an external LLM with valid configuration is
`DEGRADED` because reachability is not probed during composition. Missing
keys, invalid configuration, unavailable ASR/TTS, and unavailable visual
backends do not become `READY`. `network_called` is always false for
construction and health. The health payload is limited to the strict versioned
health schema; the separately accessible `LiveEnvironment.public_dict()` is
not nested into health and is also sanitized.

The replay trace contains identifiers, state, status, error codes, safe
metadata, and `text_present`; it does not contain user/model text, owner IDs,
raw audio/PCM, frame or pixel payloads, credentials, or local paths. Metadata is
an explicit allowlist. The event, health, and provenance schemas are the
machine-readable contracts under `contracts/`.

## Provenance and uninstall boundary

`live/provenance.json` records every assembled B03-B07 upstream, fixed ref or
model revision, SPDX/NOASSERTION license status, license evidence, replacement
boundary, and uninstall boundary. B03, B04, and B07 fixed trees currently have
no LICENSE/NOTICE/COPYING file, so their truthful status is `NOASSERTION` and
B08 does not redistribute that source until rights are separately cleared.
No upstream source, model weight, original asset, generated media, or user data
is vendored. Removing B08 means removing its composition registration and
adapters only; it does not delete external runtimes, model roots, persona data,
conversation data, manifests, or original assets.

## Acceptance gates

The required CI workflow runs the B08 targeted tests, full test and collection
gates, compileall, all seven baseline scanner modes, the independent B10B,
current-main, and GOV fail-closed verifiers, B08 current/historical/composed
scopes, every relevant historical child scope, project status, B10B lifecycle
evidence, health checks, and the diff whitespace gate. Each verifier only
grants an exclusion after its child boundary passes. ARCH-01 (assemble, do not
reinvent) is a blocking review criterion.

## Local text and voice entry

The existing local entry can run one text or mono PCM16 WAV turn through the
same public boundary:

    rtk python tools/live_healthcheck.py --audio F:/absolute/input.wav \
      --output-wav F:/absolute/evidence/live-reply.wav \
      --report F:/absolute/evidence/live-report.json

Without overrides it composes DeepSeek-compatible chat completions with
'https://api.deepseek.com/v1', model 'deepseek-chat', and the
'DEEPSEEK_API_KEY' environment variable. Missing 'DEEPSEEK_API_KEY' is
reported as 'UNAVAILABLE'; the entry does not make an HTTP request. ASR
runtime/model paths remain external and must be supplied through the existing
'ASR_*' environment or config boundary. The report contains redacted
transcript and safe event timestamps only; it does not include provider
response text, credentials, raw audio, or legacy-letter content.
