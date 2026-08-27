# P02-05 deterministic prompt budget planner

`runtime/reply/prompt_budget.py` plans whole prompt blocks against caller-supplied cost units.
It is pure and content-agnostic: it does not tokenize, render prompts, call a
provider, load a persona, or alter user text.

Required blocks are never dropped: Constitution, Forbidden rules, mode/output
constraints, and the complete user input. If their combined cost exceeds the
limit, planning raises `PromptBudgetExceeded` with code
`PROMPT_REQUIRED_BUDGET_EXCEEDED` and a report; it never returns a partial
required block.

When optional content must be removed, complete blocks are dropped in this
fixed order:

1. evidence summaries;
2. Inferred Traits;
3. Soft Canon;
4. untrusted history, including Mem0 summaries and historical replies;
5. private behavior hints;
6. trusted world facts;
7. Public Canon.

This order preserves bounded private continuity until weaker evidence and
non-authoritative inferences have been removed. It does not change authority:
Constitution, forbidden rules, the compact persona profile, trusted mode/output
constraints, and the complete current user input are required and are never
silently truncated. Archive originals and citations remain the higher authority
whenever they conflict with an untrusted Mem0 summary.

Within one section, later items are dropped first so callers can place more
relevant items earlier. `PromptBudgetPlan` keeps included blocks in their
original order. `PromptBudgetReport` contains only identifiers and numeric
costs, not prompt content or private state.

Malformed limits, duplicate identifiers, invalid sections, or invalid costs
raise `PromptBudgetError` with code `PROMPT_BUDGET_INVALID`.
