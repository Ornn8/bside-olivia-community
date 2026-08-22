# P02-08 reply reviewer contract and adapter

`reply_reviewer.py` defines a provider-neutral review adapter and typed result.
The provider response must match `contracts/reply_review.schema.json` with id
`p02.reply-review.v1`: verdict, bounded violations, four 0-100 consistency
scores, and completed status.

The request contains only candidate text, reply mode/output constraints,
trusted world facts, and caller-supplied short identified reference summaries.
It excludes private behavior values, control views, databases, paths, complete
letter libraries, and provider configuration.

`NullReviewer` and a disabled adapter make no transport call. Transport errors
return `REVIEWER_UNAVAILABLE`; invalid JSON returns
`REVIEWER_RESPONSE_INVALID`. Both results are sanitized and contain no provider
exception, request body, path, credential, prompt, or private evidence.

This module only judges. It does not modify candidate text, retry, rewrite,
persist state, call media, or connect to the reply pipeline.
