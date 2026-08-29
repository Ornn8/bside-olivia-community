# P02-17 Intimacy model

The intimacy model separates relationship stage, bounded contact permission,
and hidden evidence. It does not infer a relationship from prose, message
frequency, gifts, confessions, or inactivity.

## Stage ceiling

| Relationship stage | Maximum intimacy tier |
| --- | --- |
| `unknown / acquaintance / familiar` | `none` |
| `close` | `light_contact` |
| `committed` | `close_contact` |

The ceiling is an upper bound, not an instruction to use that tier. A contact
request is permission to consider a response; it does not require contact or
require reaching the ceiling.

## Growth rules

| Accepted event | Bounded score effect | Weekly cost |
| --- | --- | --- |
| boundary respected | trust `+1`, comfort `+1`; familiarity `+1` while quota remains | 1 point |
| conflict | trust `-2`, comfort `-2`, tension `+3` | none |
| repair | trust `+1`, comfort `+1`, tension `-2` | none |
| explicitly confirmed stage | set the supplied stage; on change, closeness `+5` and familiarity `+3` | none |
| authorized intimacy grant | append the typed grant; closeness `+2` while quota remains | 2 points |

Scores remain within `0..100`. The growth window is 7 days with a maximum of
6 points. When the quota is exhausted, non-growth effects still apply; a valid
grant is still appended, but the optional closeness growth is skipped.
Semantically equivalent events within 24 hours are deduplicated before any
effect is applied.

Canonical reply delivery, high-frequency messaging, gifts, repeated phrases,
confessions, and inactivity have no relationship effect. Stage changes require
an explicit confirmation command with evidence; ordinary communication never
auto-upgrades or auto-downgrades a stage.

## Append-only intimacy

Intimacy grants are append-only shared facts. The command surface has no grant
revoke operation, and reducer events cannot lower or rewrite a persisted grant.
A new grant must have a unique identifier, stay at or below the stage ceiling,
and pass the local command authorization boundary. The private grant statement
remains control-only; persona projection receives only `intimacy_ceiling` and
the highest `granted_intimacy` tier.

## Constitution split and migration

The earlier `constitution.respectful_relationship` combined product-safety
promises with a blanket rejection of relationship commitment. It is replaced
by four narrower rules:

- `constitution.no_product_promise` keeps the real-world and product-dependency
  boundary;
- `constitution.relationship_may_commit` permits only explicitly confirmed
  in-character relationship progress;
- `constitution.intimacy_on_request` prevents unsolicited physical contact;
- `constitution.intimacy_not_reversible` preserves already granted intimacy as
  shared history.

The runtime default is `persona_release_v2.json`, whose project-authored source
is `P02.LINLI.CONSTITUTION`. `persona_v2.json` is also a supported configurable
input and retains its historical equivalent source id `P02.CONSTITUTION`; both
provenance registries record the split explicitly. Existing real-person,
crisis-safety, autonomy, private-world, hidden-field, relationship-boundary,
no-obligatory-uplift, and text-mode protections remain in force.
