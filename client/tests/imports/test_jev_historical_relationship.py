import asyncio
from datetime import datetime, timezone
from runtime.imports.historical_memory import HistoricalExchange, assess_historical_relationship


def test_historical_relationship_uses_jev_only(monkeypatch):
    from runtime.reply import jev_questions
    class Decisions:
        async def ask(self, state, questions, *, purpose):
            assert purpose == 'historical-relationship'
            return {key: 'familiar' if key == 'stage' else 'yes' if key.startswith('e') else '20'
                    for key in questions}
    monkeypatch.setattr(jev_questions,'configured_questions',lambda:Decisions())
    # No complete method: a hidden text model call makes this fail.
    result = asyncio.run(assess_historical_relationship([
        HistoricalExchange('test',datetime.now(timezone.utc),'最近练习还好吗','慢慢有进步了')],
        gateway=object(),persona_policy='身份需要双方明确确认'))
    assert result.trust == 20 and result.evidence_indexes == (1,)


def test_ordered_batches_build_on_the_running_state_and_never_lower_the_stage(monkeypatch):
    """Each five-letter batch used to be scored from zero, so everyday batches stayed
    low while one explicit line lifted the stage: "close" with every score low."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from runtime.imports.historical_memory import HistoricalExchange, assess_historical_relationship
    from runtime.reply import jev_questions

    class Port:
        async def ask(self, state, questions, *, purpose):
            assert set(questions['trust']['criteria']) == {'same', 'up_small', 'up', 'up_big'}
            return {key: 'acquaintance' if key == 'stage' else 'yes' if key.startswith('e')
                    else 'up' if key == 'familiarity' else 'same' for key in questions}
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Port())
    exchanges = tuple(HistoricalExchange(source_record_id=f'r{i}', user_message='今天下雨了。',
                      assistant_message='记得带伞。', occurred_at=datetime(2026, 9, i + 1, tzinfo=timezone.utc))
                      for i in range(5))
    previous = dict(familiarity=40, trust=55, comfort=50, closeness=45, tension=5, relationship_stage='close')
    result = asyncio.run(assess_historical_relationship(exchanges, gateway=SimpleNamespace(),
        persona_policy='Synthetic policy.', previous_state=previous, preserve_order=True))
    assert (result.familiarity, result.trust, result.closeness) == (44, 55, 45)
    assert result.relationship_stage.value == 'close'


def test_history_alone_cannot_fill_the_relationship(monkeypatch):
    """Sixteen strongly positive batches stay below the "high" band (70)."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from runtime.imports.historical_memory import HistoricalExchange, HISTORY_CEILING, assess_historical_relationship
    from runtime.reply import jev_questions

    class Port:
        async def ask(self, state, questions, *, purpose):
            return {key: 'close' if key == 'stage' else 'yes' if key.startswith('e')
                    else 'up_big' for key in questions}
    monkeypatch.setattr(jev_questions, 'configured_questions', lambda: Port())
    exchanges = tuple(HistoricalExchange(source_record_id=f'r{i}', user_message='很想你。',
                      assistant_message='我也想你。', occurred_at=datetime(2026, 9, i + 1, tzinfo=timezone.utc))
                      for i in range(5))
    state = dict(familiarity=0, trust=0, comfort=0, closeness=0, tension=0, relationship_stage='unknown')
    for _ in range(16):
        result = asyncio.run(assess_historical_relationship(exchanges, gateway=SimpleNamespace(),
            persona_policy='Synthetic policy.', previous_state=state, preserve_order=True))
        state = dict(familiarity=result.familiarity, trust=result.trust, comfort=result.comfort,
                     closeness=result.closeness, tension=result.tension,
                     relationship_stage=result.relationship_stage.value)
    assert all(state[key] <= HISTORY_CEILING < 70 for key in ('familiarity', 'trust', 'comfort', 'closeness'))
    assert state['familiarity'] >= 50  # a long history still reads as "medium", not "low"
