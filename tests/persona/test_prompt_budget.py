import pytest

from runtime.reply.prompt_budget import (
    PromptBudgetError,
    PromptBudgetExceeded,
    PromptBudgetItem,
    PromptSection,
    plan_prompt_budget,
)


def test_required_content_fails_explicitly_instead_of_being_truncated() -> None:
    items = (
        PromptBudgetItem("constitution", PromptSection.CONSTITUTION, 40),
        PromptBudgetItem("forbidden", PromptSection.FORBIDDEN, 20),
        PromptBudgetItem("user", PromptSection.USER_INPUT, 80),
    )

    with pytest.raises(PromptBudgetExceeded) as captured:
        plan_prompt_budget(items, max_units=100)

    assert captured.value.code == "PROMPT_REQUIRED_BUDGET_EXCEEDED"
    assert captured.value.report.required_units == 140
    assert captured.value.report.overflow_units == 40
    assert captured.value.report.dropped_ids == ()


def test_optional_blocks_are_dropped_whole_in_the_fixed_priority_order() -> None:
    items = (
        PromptBudgetItem("constitution", PromptSection.CONSTITUTION, 20),
        PromptBudgetItem("forbidden", PromptSection.FORBIDDEN, 10),
        PromptBudgetItem("mode", PromptSection.MODE_CONSTRAINTS, 10),
        PromptBudgetItem("user", PromptSection.USER_INPUT, 40),
        PromptBudgetItem("history", PromptSection.HISTORY, 15),
        PromptBudgetItem("evidence", PromptSection.EVIDENCE_SUMMARY, 15),
        PromptBudgetItem("soft", PromptSection.SOFT_CANON, 15),
        PromptBudgetItem("inferred", PromptSection.INFERRED_TRAIT, 15),
        PromptBudgetItem("behavior", PromptSection.PRIVATE_BEHAVIOR, 10),
        PromptBudgetItem("world", PromptSection.WORLD_FACT, 10),
        PromptBudgetItem("canon", PromptSection.PUBLIC_CANON, 10),
    )

    plan = plan_prompt_budget(items, max_units=130)

    assert plan.report.used_units == 125
    assert plan.report.dropped_ids == ("evidence", "inferred", "soft")
    assert tuple(item.item_id for item in plan.items) == (
        "constitution",
        "forbidden",
        "mode",
        "user",
        "history",
        "behavior",
        "world",
        "canon",
    )


def test_invalid_budget_descriptors_are_rejected_before_planning() -> None:
    with pytest.raises(PromptBudgetError):
        plan_prompt_budget(
            (PromptBudgetItem("user", PromptSection.USER_INPUT, 10),),
            max_units=0,
        )
    with pytest.raises(PromptBudgetError):
        plan_prompt_budget(
            (
                PromptBudgetItem("duplicate", PromptSection.USER_INPUT, 10),
                PromptBudgetItem("duplicate", PromptSection.HISTORY, 5),
            ),
            max_units=20,
        )
    with pytest.raises(PromptBudgetError):
        PromptBudgetItem("broken", PromptSection.HISTORY, -1)


def test_bounded_blocks_keep_earlier_relevant_items_and_drop_later_whole_items() -> None:
    items = (
        PromptBudgetItem("user", PromptSection.USER_INPUT, 60),
        PromptBudgetItem("world.primary", PromptSection.WORLD_FACT, 25),
        PromptBudgetItem("world.secondary", PromptSection.WORLD_FACT, 25),
    )

    plan = plan_prompt_budget(items, max_units=90)

    assert plan.report.used_units == 85
    assert plan.report.dropped_ids == ("world.secondary",)
    assert plan.report.included_ids == ("user", "world.primary")
