# P02-15 Letter generation Persona 2.0 wiring

Persona 2.0 is enabled by default for the local letter pipeline. The legacy
`LetterAdapter._messages()` entry remains a text-letter compatibility path,
but the production reply sequence no longer relies on that method after
triage.

## Shared ReplyContext

`generate_reply()` classifies the delivery mode first and creates one trusted
`ReplyContext`. `ReplyPipeline` uses that same object twice:

1. before generation, to select the matching Persona `MODE_STYLE`, output
   constraints, trusted time, bounded PrivateWorld projection, and memory;
2. after generation, to run the deterministic and optional semantic quality
   gate.

This prevents a spoken or musical reply from being generated with
`text_letter` instructions and checked only after the fact.

## Prepared-message provider boundary

The pipeline converts a raw `ReplyRequest.content` into an immutable
`ReplyRequest.messages` pair before the provider call:

- system: Persona Constitution, compact Linli profile, actual mode and mode
  style, bounded runtime facts, and escaped untrusted history;
- user: the original current letter.

The existing local compatibility bridge still owns legacy raw-content calls.
When a request already contains assembled messages, `ReplyOrchestrator`
selects the bridge's underlying provider directly so the bridge cannot discard
or rebuild the system message.

The orchestrator's cancellation, timeout, idempotency, event stream, and error
semantics are otherwise unchanged. Reviewer transport, rewrite generation,
canonical persistence, media scheduling, and relationship commits remain
separate stages.
