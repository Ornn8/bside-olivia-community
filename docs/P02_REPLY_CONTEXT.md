# P02-04 ReplyContext contract

`reply_context.py` defines the immutable, provider-free input passed between
future reply-pipeline stages. It does not call a provider, render media, load a
persona, review a reply, or commit private state. The matching JSON Schema is
`contracts/reply_context.schema.json` with id `p02.reply-context.v1`.

## Public API

- `ReplyContext.create(...)` accepts a `ReplyMode`, timezone-aware
  `TrustedTime`, identified `TrustedWorldFact` values, a finite
  `PrivateBehaviorView`, and explicit `OutputConstraints`.
- `ReplyModeAdapter` preserves the legacy wire values: `text` maps to
  `text_letter`, while `video` maps to `spoken_video`. Both spoken and musical
  video serialize back to `video`.
- `future_im` has no legacy wire value and is rejected unless application
  state explicitly passes `future_im_enabled=True`.

Private behavior crosses this boundary only as finite enums. Raw scores,
private history, letters, prompts, local paths, credentials, and provider state
are not accepted by the contract.

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
