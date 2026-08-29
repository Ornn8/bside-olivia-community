# P02-04 ReplyContext contract

`runtime/reply/reply_context.py` defines the immutable, provider-free input shared by reply
pipeline stages. It does not call a provider, render media, load a persona,
review a reply, or commit private state. The matching JSON Schema is
`contracts/reply_context.schema.json` with id `p02.reply-context.v2`.

Version 2 makes `intimacy_request`, `intimacy_ceiling`, and
`granted_intimacy` required, bounded enum fields. Runtime producers always
emit them with fail-closed `none` defaults. A stored or external v1 payload is
not valid against the v2 schema until those defaults are added; there is no
implicit inference from prose or hidden scores.

## Public API

- `ReplyContext.create(...)` accepts a `ReplyMode`, timezone-aware
  `TrustedTime`, identified `TrustedWorldFact` values, a bounded
  `PrivateBehaviorView`, an explicit `IntimacyRequest`, and
  `OutputConstraints`.
- `PrivateBehaviorView` contains only finite relationship projections, the
  boolean `home_history_allowed`, and typed `KnownContinuationFact` values
  already marked character-known. It rejects raw home-access levels, scores,
  control awareness, arbitrary dictionaries, duplicates, and unbounded
  statements. Its intimacy ceiling and granted tier are bounded to
  `none`, `light_contact`, or `close_contact`.
- `ReplyModeAdapter` preserves the legacy wire values: `text` maps to
  `text_letter`, while `video` maps to `spoken_video`. Both spoken and musical
  video serialize back to `video`.
- `future_im` has no legacy wire value and is rejected unless application
  state explicitly passes `future_im_enabled=True`.

Private history, letters, prompts, local paths, credentials, provider state,
pending plans, and control-only facts are not accepted by the contract.

## Output invariants

| Mode | Wire mode | Output channel |
| --- | --- | --- |
| `text_letter` | `text` | `letter` |
| `spoken_video` | `video` | `spoken_text` |
| `musical_video` | `video` | `spoken_text` |
| `future_im` | none | `instant_message` |

All outputs are plain text and reject control markup. Spoken and musical video
also reject stage directions. Python validation and the public JSON Schema
encode the same rules.

Invalid values raise `ReplyContextError` with code
`REPLY_CONTEXT_INVALID`. Unsupported or disabled modes raise
`UnsupportedReplyMode` with code `REPLY_MODE_UNSUPPORTED`.
