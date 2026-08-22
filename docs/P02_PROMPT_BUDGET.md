# P02-05 deterministic prompt budget planner

`prompt_budget.py` plans whole prompt blocks against caller-supplied cost units.
It is pure and content-agnostic: it does not tokenize, render prompts, call a
provider, load a persona, or alter user text.

Required blocks are never dropped: Constitution, Forbidden rules, mode/output
constraints, and the complete user input. If their combined cost exceeds the
limit, planning raises `PromptBudgetExceeded` with code
`PROMPT_REQUIRED_BUDGET_EXCEEDED` and a report; it never returns a partial
required block.

When optional content must be removed, complete blocks are dropped in this
fixed order:

1. history;
2. evidence summaries;
3. Soft Canon;
4. Inferred Traits;
5. private behavior hints;
6. trusted world facts;
7. Public Canon.

Within one section, later items are dropped first so callers can place more
relevant items earlier. `PromptBudgetPlan` keeps included blocks in their
original order. `PromptBudgetReport` contains only identifiers and numeric
costs, not prompt content or private state.

Malformed limits, duplicate identifiers, invalid sections, or invalid costs
raise `PromptBudgetError` with code `PROMPT_BUDGET_INVALID`.
