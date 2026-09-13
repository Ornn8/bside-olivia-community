from datetime import datetime, timezone
import json

import pytest

from runtime.personal_chat import decision, initiative


class UTCHost(datetime):
    """Simulate a UTC machine even on Windows, which lacks time.tzset()."""
    @classmethod
    def fromtimestamp(cls, value, tz=None):
        return super().fromtimestamp(value, tz or timezone.utc)

    def astimezone(self, tz=None):
        return super().astimezone(tz or timezone.utc)


@pytest.mark.parametrize('stamp,awake', [
    ('2026-09-13T00:29:00+00:00', False),
    ('2026-09-13T00:30:00+00:00', True),
    ('2026-09-13T15:59:00+00:00', True),
    ('2026-09-13T16:00:00+00:00', False),
])
def test_sleep_uses_character_timezone_on_utc_machine(monkeypatch, stamp, awake):
    monkeypatch.setattr(initiative, 'datetime', UTCHost)
    policy = initiative.Initiative([], clock=lambda: datetime.fromisoformat(stamp).timestamp(), interval=lambda: 0)
    policy.received(None, None)
    assert policy.ready() is awake


@pytest.mark.parametrize('stamp,valid', [
    ('2026-09-13T16:30:00+00:00', False),  # Character is asleep, UTC host is awake.
    ('2026-09-14T00:30:00+00:00', True),   # Character is awake, UTC host is asleep.
])
def test_appointment_validation_uses_same_timezone(monkeypatch, stamp, valid):
    monkeypatch.setattr(decision, 'datetime', UTCHost)
    raw = json.dumps(dict(text='好', delivery='text', listening='keep', initiative='keep',
        pause_until=None, letter='keep', letter_until=None, followup_at=stamp,
        evidence='明天找我', sticker=None, skip=False))
    now = datetime.fromisoformat('2026-09-13T10:00:00+00:00').timestamp()
    if valid:
        assert decision.decode(raw, user='明天找我', now=now)['followup_at'] == datetime.fromisoformat(stamp).timestamp()
    else:
        with pytest.raises(ValueError, match='PERSONAL_CHAT_DECISION_INVALID'):
            decision.decode(raw, user='明天找我', now=now)
