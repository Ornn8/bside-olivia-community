# P02-10 PrivateWorld contract

`private_world_port.py` defines the provider-free `PrivateWorldPort`,
`PrivateWorldSnapshot`, `PrivateWorldControlView`,
`PrivateWorldCharacterView`, and disabled `NullPrivateWorldPort`.

The snapshot stores bounded relationship scores, relationship stage, current
nickname permissions, home access, legacy continuation awareness, and typed
`LocalContinuationFact` records. A continuation fact has a stable identifier,
a short local statement, and one awareness state:

- `control_only`: only the local maintenance layer may read it;
- `pending`: recorded, but the character does not know it yet;
- `character_known`: eligible for the bounded character projection.

Nickname permissions accept short Unicode labels such as Chinese private
nicknames, while rejecting whitespace, controls, duplicates, and unbounded
values. Concrete nickname instances and continuation statements remain local
data and are never committed as fixtures.

The control view can expose all local state to an explicitly authorized local
administrator. The character view removes hidden scores, raw home-access
levels, and every pending/control-only continuation. It exposes only a boolean
home-history permission and character-known facts.

This module does not persist and does not call a provider. It also does not
infer events, render prompts, or update relationship values. P02-11 owns
persistence and P02-13 owns the model-facing projection.
