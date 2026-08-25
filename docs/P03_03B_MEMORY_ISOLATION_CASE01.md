# P03-03B Memory Isolation Case01

`memory_isolation_case01.run_case01` is the public, one-case synthetic tracer
for the private-memory isolation experiment. Callers supply the manifest path,
a fresh opaque `namespace`, memory factory, generator, two evaluators, Persona
authority, output path, and validation mode. The runner passes that namespace
unchanged to `memory_factory`.

This tracer accepts only `validation_mode=synthetic_validation`; it cannot be
used to label callback output as a real-provider validation result.

The manifest must contain exactly 60 train items. The tracer rebuilds memory
from those 60 originals, then adds only the first test original selected by
the smallest `split_sequence`. It invokes the blind persona evaluator before
opening the held-out reference; only the reference evaluator receives that
reference.

This arm is factually `private_world_arm=fixed_disabled`: the runner API has
no PrivateWorld input and does not pass PrivateWorld data to `generator`.
Generator and evaluator callbacks remain caller-owned; this tracer proves only
the arguments it supplies, not what callback closures may independently use.

The caller-owned report is either `completed` or `unavailable`, as specified
by `contracts/memory_isolation_case01_report.schema.json`. It contains only
counts, IDs, status, scores, and stable error codes; it excludes corpus text,
generated replies, reference text, exception messages, secrets, and absolute
paths.

`run_case01` does not run the 19-prefix series, contact a real provider, or
enable PrivateWorld state changes.

## Prefix19 runner

`memory_isolation_case01.run_prefix19` is the corresponding public runner for
case01 through case19. It accepts the same caller-owned callbacks
and requires exactly 60 train and 19 test items, sorted by the manifest's
`split_sequence`, `source_date`, and `source_order` fields. It derives fresh
namespaces as `<namespace>:case01` through `<namespace>:case19`; every case
rebuilds the 60 train originals and then ingests only its test-original prefix.

It calls the generator once for the current prefix item, with only
`persona_authority`, selected evidence, and that original. Generated replies
are never ingested. Blind persona evaluation occurs before reference handling.
Only text references are opened for the reference evaluator; video references
are reported as `not_evaluated_media` and are never opened or uploaded.

The same report schema accepts the completed 19-case aggregate. Its per-case
records are limited to IDs, namespace, counts, statuses, finite scores, hard
violation counts, and reference status. Synthetic fixtures use
`synthetic_validation`; a caller-owned, Git-ignored real run must use
`private_local_validation`. The default remains
`private_world_arm=fixed_disabled`. A caller running a separate controlled
PrivateWorld arm may explicitly declare
`private_world_arm=controlled_projection`; the runner preserves that label in
completed and unavailable reports. This declaration does not provide
PrivateWorld state to callbacks or verify caller-owned projection behavior.

Persona metrics are limited to the eight declared authority axes; held-out
comparison is limited to `style_score` and `focus_score`. Unknown metric names
are rejected so callback-controlled text cannot become a report key. A failed
run writes only its redacted `failed_case`, rather than a partial case array
whose count could disagree with its contents.
