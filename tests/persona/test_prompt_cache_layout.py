import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re

import pytest

from runtime.persona.persona_assembly import assemble_persona, UntrustedFragment
from runtime.persona.persona_loader import load_persona
from runtime.reply.prompt_budget import PromptBudgetExceeded
from runtime.reply.reply_context import BehaviorLevel, PrivateBehaviorView, ReplyContext, ReplyMode, TrustedTime


def test_changing_clock_relationship_and_life_preserves_fixed_persona_prefix():
    snapshot = load_persona(Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json").snapshot
    results = []
    for minute, trust in ((1, BehaviorLevel.LOW), (2, BehaviorLevel.HIGH)):
        context = ReplyContext.create(
            ReplyMode.TEXT_LETTER,
            trusted_time=TrustedTime(datetime(2026, 9, 10, 8, minute, tzinfo=timezone.utc)),
            private_behavior=PrivateBehaviorView(trust=trust),
        )
        result = assemble_persona(
            snapshot, context, user_input="今天很平常。", max_units=30000,
            relationship_expression_enabled=True,
            evidence_summaries=(UntrustedFragment("linli.daily-life", f"合成近况 {minute}"),),
        )
        mode = json.loads(re.search(r"<mode_constraints>\s*(.*?)\s*</mode_constraints>", result.system_content, re.S)[1])
        assert "trusted_time" not in mode and "character_local_time" not in mode
        clock = json.loads(re.search(r"<runtime_time>\s*(.*?)\s*</runtime_time>", result.system_content, re.S)[1])
        assert clock["trusted_time"] == context.to_dict()["trusted_time"]
        assert clock["character_local_time"] == f"2026-09-10T16:0{minute}:00+08:00"
        assert f"合成近况 {minute}" in result.system_content
        assert "runtime_time" in result.budget_report.included_ids
        assert result.budget_report.used_units == len(result.system_content) + len(result.user_content)
        results.append(result)

    common = os.path.commonprefix([result.system_content for result in results])
    for declaration in snapshot.declarations:
        if declaration.tier == "PUBLIC_CANON":
            assert declaration.declaration_id in common
    assert "<private_behavior>" not in common
    assert "合成近况" not in common
    assert results[0].system_content != results[1].system_content


def test_clock_remains_required_even_when_optional_context_is_dropped():
    snapshot = load_persona(Path(__file__).resolve().parents[2] / "linli_character/persona_release_v2.json").snapshot
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime(2026, 9, 10, tzinfo=timezone.utc)))
    with pytest.raises(PromptBudgetExceeded) as caught:
        assemble_persona(snapshot, context, user_input="你好", max_units=100)
    assert "runtime_time" in caught.value.report.included_ids
