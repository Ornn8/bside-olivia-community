# Repeatable daily video moments

A generic `moment` is a share of the currently observed live place. Its source
may have started on an earlier date. Bath, wake, meal and other specific event
eligibility rules are unchanged; a specific event takes precedence while it is
available.

`life_video_moments` stores the share's `event_id`, original `source_id`, local
day, ordinal, capture timestamp and reservation timestamp. The opaque share ID
is safe for the remote event ID contract; consumers resolve the source through
the table, never by parsing the ID. Candidate refreshes reuse an unreserved
share. Current-place checks, factual recall and delivery reservations use the
original source. Existing queues with original source IDs remain readable.

A reservation is recorded before authoring or queueing a new generic share.
The next share is eligible 30 minutes later, with at most four reservations per
source per local calendar day (UTC+08:00, the existing daily-life timezone).
Midnight resets the daily count but does not reset the cooldown. Existing
delivery reservations for the original source count towards both limits.
Reservations are conservative: an interrupted or unsuccessful authoring attempt
still consumes its slot. Source freshness continues to follow the existing
world rules.

The worker serializes shared-video authoring and persists a preparation before
the writer call. Replays use the existing queued job; an interrupted writer is
not automatically called again for the same share. Generation retains the
existing receipt and remote event-ID deduplication contract.

If the photo planner selects a video that cannot enter the queue, that turn
requests a photo with `requested_image=true` and no `video_moments`. The photo
has a separate stable output/receipt path, hence a new request ID. Retries reuse
that receipt, including an accepted submission whose response was lost. The
original skipped request remains intact. Reconnect recovery checks for an
already queued video before starting a fallback; ambiguous photo delivery is
not resent. Photo generation and transport can still fail under the existing
failure policy; a fallback request alone is not proof of delivery.

Telemetry schema 2 adds optional `daily_video_result` on media observations:
`queued`, `source_unavailable`, `scene_unavailable`, `consent_required`,
`capability_unavailable`, `photo_fallback`, or `other`. The first outcome and a
subsequent acknowledged photo fallback are separate, deduplicated observations
on the same anonymized trace. They contain no source IDs, prompts or content.
Schema 1 omits the field; schema 3 also retains it. The telemetry receiver must
accept this optional field before rollout.

Review decisions before rollout: confirm conservative reservation accounting
and receiver compatibility. This change does not include deployment or release.
Rollback is a code revert; the additive local table can be left in place.
