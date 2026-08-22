"""Deterministic, content-agnostic prompt budget planning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class PromptBudgetError(ValueError):
    code = "PROMPT_BUDGET_INVALID"


class PromptBudgetExceeded(PromptBudgetError):
    code = "PROMPT_REQUIRED_BUDGET_EXCEEDED"

    def __init__(self, report: "PromptBudgetReport") -> None:
        super().__init__(self.code)
        self.report = report


class PromptSection(str, Enum):
    CONSTITUTION = "constitution"
    FORBIDDEN = "forbidden"
    MODE_CONSTRAINTS = "mode_constraints"
    USER_INPUT = "user_input"
    PRIVATE_BEHAVIOR = "private_behavior"
    WORLD_FACT = "world_fact"
    PUBLIC_CANON = "public_canon"
    HISTORY = "history"
    EVIDENCE_SUMMARY = "evidence_summary"
    SOFT_CANON = "soft_canon"
    INFERRED_TRAIT = "inferred_trait"


_REQUIRED = frozenset(
    {
        PromptSection.CONSTITUTION,
        PromptSection.FORBIDDEN,
        PromptSection.MODE_CONSTRAINTS,
        PromptSection.USER_INPUT,
    }
)
_DROP_ORDER = (
    PromptSection.HISTORY,
    PromptSection.EVIDENCE_SUMMARY,
    PromptSection.SOFT_CANON,
    PromptSection.INFERRED_TRAIT,
    PromptSection.PRIVATE_BEHAVIOR,
    PromptSection.WORLD_FACT,
    PromptSection.PUBLIC_CANON,
)
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


@dataclass(frozen=True)
class PromptBudgetItem:
    item_id: str
    section: PromptSection
    cost_units: int

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not _ID_RE.fullmatch(self.item_id):
            raise PromptBudgetError("item_id must be a stable identifier")
        if not isinstance(self.section, PromptSection):
            raise PromptBudgetError("section is invalid")
        if type(self.cost_units) is not int or self.cost_units < 0:
            raise PromptBudgetError("cost_units must be a non-negative integer")


@dataclass(frozen=True)
class PromptBudgetReport:
    max_units: int
    input_units: int
    required_units: int
    used_units: int
    overflow_units: int
    included_ids: tuple[str, ...]
    dropped_ids: tuple[str, ...]


@dataclass(frozen=True)
class PromptBudgetPlan:
    items: tuple[PromptBudgetItem, ...]
    report: PromptBudgetReport


def plan_prompt_budget(
    items: tuple[PromptBudgetItem, ...], *, max_units: int
) -> PromptBudgetPlan:
    if type(max_units) is not int or max_units < 1:
        raise PromptBudgetError("max_units must be a positive integer")
    candidates = tuple(items)
    item_ids = [item.item_id for item in candidates]
    if len(item_ids) != len(set(item_ids)):
        raise PromptBudgetError("item identifiers must be unique")
    required_units = sum(item.cost_units for item in candidates if item.section in _REQUIRED)
    input_units = sum(item.cost_units for item in candidates)
    if required_units > max_units:
        report = PromptBudgetReport(
            max_units=max_units,
            input_units=input_units,
            required_units=required_units,
            used_units=required_units,
            overflow_units=required_units - max_units,
            included_ids=tuple(item.item_id for item in candidates if item.section in _REQUIRED),
            dropped_ids=(),
        )
        raise PromptBudgetExceeded(report)
    kept = [True] * len(candidates)
    dropped_ids: list[str] = []
    used_units = input_units
    for section in _DROP_ORDER:
        for index in range(len(candidates) - 1, -1, -1):
            item = candidates[index]
            if used_units <= max_units:
                break
            if item.section is section:
                kept[index] = False
                used_units -= item.cost_units
                dropped_ids.append(item.item_id)
        if used_units <= max_units:
            break
    included = tuple(item for index, item in enumerate(candidates) if kept[index])
    report = PromptBudgetReport(
        max_units=max_units,
        input_units=input_units,
        required_units=required_units,
        used_units=used_units,
        overflow_units=0,
        included_ids=tuple(item.item_id for item in included),
        dropped_ids=tuple(dropped_ids),
    )
    return PromptBudgetPlan(included, report)
