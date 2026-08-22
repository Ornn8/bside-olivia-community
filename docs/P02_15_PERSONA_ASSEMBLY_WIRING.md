# P02-15 LetterAdapter Persona 2.0 wiring

Set `persona_v2_enabled=true` (or `OLIVIA_PERSONA_V2_ENABLED=true`) to route
`LetterAdapter._messages()` through `PersonaAssembly`. The default remains false,
so the existing adapter path is unchanged until explicitly enabled.

The v2 path loads `persona_v2_file` with DRAFT fallback, creates a text-letter
`ReplyContext`, projects PrivateWorld state into finite character inputs, and
passes MemoryPromptBuilder output as one escaped, untrusted history block. It
does not modify `reply_orchestrator.py`, run a reviewer, or write relationship
state.
