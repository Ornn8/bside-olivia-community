# P02-06 PersonaAssembly

`runtime.persona.persona_assembly` is the single pure assembly entrypoint for Persona 2.0. The root `persona_assembly.py` remains a compatibility alias for existing callers.
It accepts a typed `PersonaSnapshot`, `ReplyContext`, exact user input, and
optional typed untrusted fragments. It returns two chat messages and the
P02-05 budget report. It does not call a provider, persist state, review a
reply, or connect to the server.

The system message uses a fixed hierarchy: Constitution, fixed Forbidden
rules, mode/output constraints, finite private behavior hints, trusted world
facts, Public Canon, Community Soft Canon, Inferred/Uncertainty declarations,
mode-specific style, evidence summaries, then history. User input is always a
separate user-role message and never enters a system block.

History and evidence are JSON-encoded, marked `untrusted`, and escape angle
brackets before entering system content. The budget planner drops them as
whole blocks; required Constitution, Forbidden, mode/output constraints, and
the complete user input are never truncated.

When the loader returns `DRAFT`, assembly uses a small generic Constitution
that prohibits invented identity and shared history. It does not promote the
snapshot to READY or copy blocked declarations into the prompt.
