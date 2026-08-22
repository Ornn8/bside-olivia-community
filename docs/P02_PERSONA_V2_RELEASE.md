# Persona 2.0 release activation

Persona 2.0 is enabled by default through
`linli_character/persona_release_v2.json`.

The release payload is no longer treated as complete merely because its JSON
and rights metadata are valid. It must contain a typed character profile,
all declared required facets, and a `MODE_STYLE` declaration for every
enabled reply mode.

## Public status

The loader reports one of three states:

- `READY`: a complete character profile with all required facets and modes;
- `POLICY_ONLY`: valid public rules exist, but the package is not complete
  enough to claim a named character;
- `DRAFT`: the file is missing, unreadable, malformed, schema-invalid, or
  contains a release-blocked declaration.

`POLICY_ONLY` uses only its Constitution rules and a generic unnamed profile.
It never silently presents a governance-only package as Linli.

Stable error codes include:

- `PERSONA_FILE_MISSING`
- `PERSONA_READ_FAILED`
- `PERSONA_JSON_INVALID`
- `PERSONA_SCHEMA_INVALID`
- `PERSONA_SCHEMA_UNAVAILABLE`
- `PERSONA_RIGHTS_BLOCKED`
- `PERSONA_INCOMPLETE`

## Linli release profile

The default release package contains short, independently rewritten
declarations distilled from
`docs/persona-sources/linli-im-private-constitution-1.0.zh-CN.md`.

It covers:

- identity and public background;
- core temperament and independent daily life;
- autonomy, refusal, fatigue, disagreement, and selective attention;
- unfamiliar-knowledge and temporary-tool boundaries;
- concrete, restrained expression instead of generic counselling;
- living nickname language and non-performative relationship expression;
- memory uncertainty without invented shared history;
- separate text-letter, spoken-video, musical-video, and disabled future-IM
  style rules.

The original long source text, private nickname instances, addresses,
relationship history, control protocol, and local-continuation instances are
not injected into the public prompt. The mapping is recorded in
`linli_character/persona_release_provenance_v2.json`.

## Prompt hierarchy

`PersonaAssembly` builds:

1. Constitution;
2. forbidden claims;
3. the compact character profile;
4. trusted mode and output constraints;
5. the current mode style;
6. bounded PrivateWorld behavior;
7. trusted runtime facts;
8. public/soft/inferred declarations;
9. evidence and history as escaped untrusted data;
10. the user message in a separate `user` role.

The compact profile and current mode style are required budget items. History,
evidence, soft canon, inference, private behavior, world facts, and additional
public canon are dropped as whole blocks before any required item is
truncated.

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

## Release checklist

- run `python -m pytest -q`;
- run `python baseline_hardening_scan.py --mode all`;
- confirm Persona health is `READY/persona_v2`;
- confirm the default payload contains every required facet and mode;
- confirm the source document and private instances are absent from the
  assembled prompt;
- confirm only synthetic fixtures exist in Persona and PrivateWorld tests;
- confirm duplicate delivery and recovery leave one ledger event;
- confirm export/reset/delete require explicit local invocation;
- confirm no model, original media, credential, user data, or private state is
  included in the release.

## Rollback

Set `OLIVIA_PERSONA_V2_ENABLED=false` or set `persona_v2_enabled` to `false`
in the local LLM configuration. This restores the legacy Persona path without
deleting replies, memory, or PrivateWorld data. A corrupt or rights-blocked
v2 payload falls back to DRAFT. An incomplete but otherwise valid payload is
restricted to `POLICY_ONLY`.
