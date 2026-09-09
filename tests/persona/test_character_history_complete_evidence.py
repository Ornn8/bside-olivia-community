"""Budget trimming must not turn conditional dialogue into a permanent claim."""
from types import SimpleNamespace

from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord
from runtime.memory.memory_prompt import MemoryPromptBuilder
from runtime.reply.reply_pipeline import (
    _CHARACTER_REPLY_PREFIX,
    _character_reply_fragment,
    _selected_history,
)


def record(text, identity="fixture"):
    return MemoryRecord(
        memory_id=identity, domain=CONVERSATION_MEMORY, text=text,
        source="fixture", created_at=0,
        provenance={"source_record_id": "history:" + identity},
        metadata={"canonical": True, "history_actor": "linli"},
    )


def test_budget_cannot_strip_a_trailing_time_limit():
    opening = "我说我不想聊天"
    text = opening + "，仅限今晚；睡醒以后照常聊。"
    assert _character_reply_fragment(
        record(text), remaining=len(_CHARACTER_REPLY_PREFIX) + len(opening),
    ) is None
    complete = _character_reply_fragment(
        record(text), remaining=len(_CHARACTER_REPLY_PREFIX) + len(text),
    )
    assert complete.text == _CHARACTER_REPLY_PREFIX + text


def test_oversized_evidence_does_not_displace_a_later_complete_fact():
    long_record = record("我说：" + "这只是当天的情况。" * 150 + "以后不一定如此。", "long")
    short_record = record("我对用户说：以后叫我林离。", "short")
    selected = _selected_history(SimpleNamespace(references=(long_record, short_record), text=""))
    assert len(selected.fragments) == 1
    assert selected.fragments[0].text == _CHARACTER_REPLY_PREFIX + short_record.text


def test_memory_summary_route_also_skips_incomplete_current_facts():
    long_record = record("我说：" + "这只是当天的情况。" * 100 + "以后不一定如此。", "long")
    short_record = record("我对用户说：以后叫我林离。", "short")

    class Memory:
        def search(self, *args, **kwargs):
            return (long_record, short_record)

        def status(self):
            return {"status": "available"}

    prompt = MemoryPromptBuilder(
        Memory(), conversation_memory=None, conversation_budget=3000, legacy_budget=0,
    ).build("上次说过什么？", max_chars=3000)
    assert prompt.references == (short_record,)
    assert short_record.text in prompt.text
    assert "这只是当天的情况" not in prompt.text
    assert prompt.truncated
