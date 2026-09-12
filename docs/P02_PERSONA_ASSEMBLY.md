# P02-06 PersonaAssembly

`runtime.persona.persona_assembly` is the single pure assembly entrypoint for Persona 2.0. The root `persona_assembly.py` remains a compatibility alias for existing callers.
It accepts a typed `PersonaSnapshot`, `ReplyContext`, exact user input, and
optional typed untrusted fragments. It returns two chat messages and the
P02-05 budget report. It does not call a provider, persist state, review a
reply, or connect to the server.

The system message retains explicit authority tiers. Stable Constitution,
Forbidden rules, profile, mode/output constraints, mode style and selected
Public/Community/Inferred/Uncertainty declarations form the prefix. The required
`runtime_time` block follows them; examples, private behavior, trusted world
facts, evidence and history form the changing context. Factual grounding remains
at the end. This ordering keeps changing clocks and relationship state from
breaking the fixed persona prefix. It does not promote reference data into policy.
User input is always a separate user-role message and never enters a system block.

History and evidence are JSON-encoded, marked `untrusted`, and escape angle
brackets before entering system content. The budget planner drops them as
whole blocks; required Constitution, Forbidden, mode/output constraints, and
the complete user input are never truncated.

When the loader returns `DRAFT`, assembly uses a small generic Constitution
that prohibits invented identity and shared history. It does not promote the
snapshot to READY or copy blocked declarations into the prompt.


The READY production reply pipeline and direct LetterAdapter generation enable
`relationship_expression_enabled`. Five writer-facing grades become independent
expression tendencies (familiarity, self-disclosure, ease, distance, tension).
Unknown axes are omitted; unknown does not imply an initial or cold relationship.
The typed state, confirmed stage, action permissions, known continuations, active
boundaries and acknowledged affection are unchanged. No model call or state write
is added. Generic assembly retains the opt-in parameter for compatibility.

The writer's `nickname_use.has_authorized_history` reports whether a current
nickname grant exists, not a blanket decision about a new form of address.
Concrete labels and direction remain grounded in the authorization source:
user-to-character and character-to-user addressing are distinct. The character
may accept or refuse a newly proposed name according to her persona and active
boundaries; current acceptance does not establish past authorization or expand
relationship/contact permissions. The stored enum and grant/revoke flow remain
unchanged. This writer field is also covered by private-state leakage checks.

`runtime_time` uses the required MODE_CONSTRAINTS budget section. It retains the
exact UTC trusted time and Shanghai character-local time, even when optional
world context is cropped. DRAFT/POLICY_ONLY remain unnamed generic identities.
