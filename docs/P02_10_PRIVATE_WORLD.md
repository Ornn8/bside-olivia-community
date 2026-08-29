# P02-10 PrivateWorld contract

`private_world_port.py` defines the provider-free `PrivateWorldPort`,
`PrivateWorldSnapshot`, `PrivateWorldControlView`,
`PrivateWorldCharacterView`, and disabled `NullPrivateWorldPort`.

The schema v3 snapshot stores bounded relationship scores, relationship stage,
append-only `intimacy_grants`, the 7-day growth window, current nickname
permissions, home access, legacy continuation awareness, and typed
`LocalContinuationFact` records. Each grant has a stable identifier, bounded
tier, and private statement; its statement never enters the character view. A
continuation fact has a stable identifier, a short local statement, and one
awareness state:

- `control_only`: only the local maintenance layer may read it;
- `pending`: recorded, but the character does not know it yet;
- `character_known`: eligible for the bounded character projection.

Nickname permissions accept short Unicode labels such as Chinese private
nicknames, while rejecting whitespace, controls, duplicates, and unbounded
values. Concrete nickname instances and continuation statements remain local
data and are never committed as fixtures.

The control view can expose all local state to an explicitly authorized local
administrator. The character view removes hidden scores, raw home-access
levels, grant statements, growth-window fields, and every pending/control-only
continuation. It exposes only a boolean home-history permission,
character-known facts, and the highest bounded `granted_intimacy` tier. The
full stage, growth, and append-only rules are recorded in
[`P02_17_INTIMACY_MODEL.md`](P02_17_INTIMACY_MODEL.md).

This module does not persist and does not call a provider. It also does not
infer events, render prompts, or update relationship values. P02-11 owns
persistence and P02-13 owns the model-facing projection.

## Runtime health projection

`/health?profile=core` exposes the optional local runtime as
`providers.private_world`. Its public payload is validated by
`contracts/private_world_runtime_health.schema.json` and contains only
availability, provider kind, a stable reason code, schema/migration markers,
aggregate counts, probe state, and `network_called=false`. It never exposes a
database path, snapshot text, nicknames, relationship scores, raw home-access
levels, continuation data, or reply text.

The runtime reports `available/sqlite` only after the current snapshot can be
read and parsed. Storage, JSON, schema, or snapshot semantic failures report
`unavailable/none` with `PRIVATE_WORLD_STORAGE_UNAVAILABLE`; normal reply
delivery stays pending for later recovery.
