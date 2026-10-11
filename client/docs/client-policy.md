# Client behavior policy

`GET /v1/client-policy` uses the saved account key and the
`X-Olivia-Client-Version` request header. The response is
`{"version":"review-1","policy":{...}}`. The client fetches at startup and
every ten minutes, with a three-second timeout. The last validated response is
cached as `client-policy.json` in the configuration state directory, scoped to
the running client version. Without a usable response or cache, defaults match
2.2.15.

Unknown policy keys are ignored. An invalid known top-level key falls back to
that key's built-in defaults without discarding other valid keys. Integer values
are clamped; booleans, fractional numbers and non-numbers are invalid.

| Key | Contract and bounds |
| --- | --- |
| `qq_default_voice` | `mode`: `voice_only`, `text_then_voice`, `text_only`; `wait_seconds`: 5–120, default 60. The existing timeout environment override takes priority. Explicit media requests retain their existing handling. |
| `proactive.intervals` | `reserved`, `familiar`, `trusted`, `close`, `committed`, each with `min_minutes` and `max_minutes`: 1–1440, minimum no greater than maximum. Existing tension backoff still applies. |
| `proactive.engagement` | Counts: 1–10000; active-day and habitual-hour-day thresholds: 1–7. Default thresholds preserve the previous engagement classification. |
| `generation_retry.no_retry_codes` | Up to 128 uppercase error-code tokens, each at most 96 characters. |
| `notices` | Error-code-to-text mapping; `DEFAULT` handles unmatched codes. Up to 128 entries, each 1–500 characters without control characters. Text is delivered as a system notice. |
| `media_share` | `photo_daily_limit` and `video_daily_limit`: `null` or integers 0–1000. Both default to `null`. This slice validates and caches these values; daily-limit execution is not connected. |

The relay validates the entire file, including every version override, before
replacing its in-memory last-valid catalog. Invalid edits or a missing file keep
the previous catalog. On cold start without any valid catalog, built-in defaults
apply. `overrides` maps **exact** client version strings to replacements for whole
top-level policy keys; versions without a match use the base policy. Operators
should replace JSON atomically and change its version when changing behavior.

Telemetry stores only the policy version. The client asks for the capability
using `policy_version=1`; the relay only adds its capability ACK for clients that
ask. Uploads omit the field until the capability is confirmed, and omit it again
on schema downgrade or upload failure. Deploy the relay whitelist before
releasing clients that use it.

# Effective friendship

The read-only relationship projection uses the existing engagement receipt
filtering and distinct local interaction dates (UTC+8). It considers today plus
the previous 13 or 20 calendar dates. Duplicate messages on one date count as one
day; future, proactive and unsupported-channel receipts do not count.

Four medium-or-higher relationship levels, non-high tension and five active days
in fourteen permit `friend`. Four high levels, low tension and eight active days
in twenty-one permit `trusted_friend`. Confirmed `close` and `committed` stages
remain unchanged. The projection never writes a stage to the ledger and never
changes nickname, contact, or home permissions.

Both friendship stages use the `trusted` proactive frequency floor; engagement
may raise frequency under the existing off-hour and tone rules. Writer context
and the settings panel use the same projection. Writer context explicitly
distinguishes friendship formed through long-term interaction from confirmed
romantic identity. The panel presents a stage and a qualitative sentence rather
than five level labels or numbers.
