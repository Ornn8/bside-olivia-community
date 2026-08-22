# P02 Reply quality pipeline

`ReplyPipeline` wraps the existing orchestrator without changing its request or
event lifecycle. The orchestrator produces a private candidate; the bounded
quality gate produces the only canonical text returned by the pipeline.

For a configured non-mock Provider, semantic review and one-shot rewrite are
enabled automatically unless disabled by the documented environment controls.
An unconfigured or mock profile keeps `NullReviewer` and the deterministic
fallback, so offline smoke tests and explicit local fallback remain truthful.

`generate_reply` writes `reply_text`, marks `COMPLETED`, updates memory, and
schedules text/video/music projection only after acceptance. A blocked
candidate never reaches those sinks. Persisted review metadata is limited to
quality status and violation codes; reviewer prompts, hidden state, user
excerpt, model response, and private evidence are not stored.

A clean deterministic reply may degrade open when the reviewer is unavailable.
A deterministic hard violation requires the one available rewrite and fails
closed if the rewrite is unavailable or remains invalid. The first candidate,
review, optional rewrite, and final review all use the same `ReplyContext`.
