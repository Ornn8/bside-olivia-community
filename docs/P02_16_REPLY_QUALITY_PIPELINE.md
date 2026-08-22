# P02-16 Reply quality pipeline

`ReplyPipeline` wraps the existing orchestrator without changing its request or
event lifecycle. The orchestrator produces a private candidate; the bounded
quality gate produces the only canonical text returned by the pipeline.

`generate_reply` writes `reply_text`, marks `COMPLETED`, updates memory, and
schedules text/video/music projection only after acceptance. A blocked candidate
never reaches those sinks. Persisted review metadata is limited to quality status
and violation codes; reviewer prompts, hidden state, and private evidence are not
stored. The default reviewer is disabled and clean deterministic output degrades
open; deterministic violations fail closed when no rewriter is configured.
