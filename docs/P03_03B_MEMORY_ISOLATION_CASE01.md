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

This narrow slice does not run the 19-prefix series, contact a real provider,
or enable PrivateWorld state changes.
