# Persona 2.0 release activation

Persona 2.0 is enabled by default through
`linli_character/persona_release_v2.json`. This file contains only short,
redistributable Constitution declarations. The auditable
`persona_v2.json` source registry remains separate and is never selected as
the release payload because it intentionally contains quarantined records.

## Runtime sequence

1. `LetterAdapter` loads and validates the release payload.
2. `PersonaAssembly` creates the system policy and a separate user message.
3. `ReplyPipeline` generates a private candidate and applies the deterministic
   and optional reviewer quality gate, with a global maximum of one rewrite.
4. Only accepted canonical text is persisted and sent to text, speech, video,
   or music projections.
5. The canonical delivery records one idempotent PrivateWorld event. Media
   retry or rerender never commits another relationship event.

PrivateWorld scores and control state never enter the model. Only the bounded
behavior projection and currently authorized nicknames may enter character
context. Pending and control-only continuation events remain hidden.

## Health and degradation

The LLM health profile reports Persona as `READY/persona_v2` when the release
payload validates. Missing, unreadable, invalid, or rights-blocked payloads
report `DRAFT` with one stable code:

- `PERSONA_FILE_MISSING`
- `PERSONA_READ_FAILED`
- `PERSONA_JSON_INVALID`
- `PERSONA_SCHEMA_INVALID`
- `PERSONA_SCHEMA_UNAVAILABLE`
- `PERSONA_RIGHTS_BLOCKED`

The health path performs no provider request. A reviewer outage allows only a
deterministically clean reply as `accepted_degraded`; hard violations still
fail closed.

## Release checklist

- run `python -m pytest -q`;
- run `python baseline_hardening_scan.py --mode all`;
- confirm the Persona health object is `READY/persona_v2`;
- confirm only synthetic fixtures exist in Persona and PrivateWorld tests;
- confirm duplicate delivery and recovery leave one ledger event;
- confirm export/reset/delete require explicit local invocation;
- confirm no model, original media, credential, user data, or private state is
  included in the release.

## Rollback

Set `OLIVIA_PERSONA_V2_ENABLED=false` or set `persona_v2_enabled` to `false`
in the local LLM configuration. This restores the legacy Persona path without
deleting replies, memory, or PrivateWorld data. A corrupt v2 payload also
falls back to DRAFT instead of bypassing validation.
