"""A contact the cloud asks for: what is accepted, when it is carried out and how it is reported."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from runtime import cloud_contact, cloud_events, contact_context, proactive_config
from runtime.cloud_contact import Outcome
from runtime.diary.diary import SHANGHAI

ID = 'contact:00000000-0000-4000-8000-000000000001'
OTHER = 'contact:00000000-0000-4000-8000-000000000002'
THIRD = 'contact:00000000-0000-4000-8000-000000000003'


def at(hour, minute=0, day=13):
    """An epoch second for a Shanghai wall-clock time on 2026-10-<day>."""
    return int(datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI).timestamp())


NOW = at(20)


def event(contact_id=ID, **fields):
    value = {'id': contact_id, 'kind': 'contact', 'at': NOW - 600, 'deliver_after': NOW - 60, 'expires': NOW + 3600,
             'purpose': 'recall', 'channel': 'letter', 'brief': 'say a few warm words', 'qq_format': 'text'}
    value.update(fields)
    return value


@pytest.fixture(autouse=True)
def built_in_config():
    proactive_config.configure(None)
    yield
    proactive_config.configure(None)


def kept(tmp_path, *items, now=NOW):
    cloud_contact.record(tmp_path, [cloud_contact.parse_event(item) for item in items or (event(),)], now)


def no_slot():
    from contextlib import contextmanager
    @contextmanager
    def claim():
        yield True
    return claim


# --- schema


def test_a_good_event_is_kept_with_the_documented_defaults():
    parsed = cloud_contact.parse_event(event())
    assert parsed == {'id': ID, 'at': NOW - 600, 'deliver_after': NOW - 60, 'expires': NOW + 3600, 'purpose': 'recall',
                      'channel': 'letter', 'qq_wait_seconds': 180, 'title': None, 'brief': 'say a few warm words',
                      'body': None, 'letter_length': None, 'qq_format': 'text', 'media': [], 'quiet_hours': True,
                      'window': None, 'billing': 'normal'}


def test_every_field_of_the_schema_is_read():
    parsed = cloud_contact.parse_event(event(
        channel='qq_then_letter', qq_wait_seconds=0, title='晚安', letter_length={'min': 200, 'max': 600},
        qq_format='voice', quiet_hours=False, window=['19:00', '22:00'], billing='free',
        media=[{'type': 'photo', 'brief': 'a quiet street'}, {'type': 'video', 'brief': None}, {'type': 'voice'}]))
    assert parsed['channel'] == 'qq_then_letter' and parsed['qq_wait_seconds'] == 0 and parsed['title'] == '晚安'
    assert parsed['letter_length'] == {'min': 200, 'max': 600} and parsed['qq_format'] == 'voice'
    assert parsed['quiet_hours'] is False and parsed['window'] == ['19:00', '22:00'] and parsed['billing'] == 'free'
    assert [item['type'] for item in parsed['media']] == ['photo', 'video', 'voice']
    assert parsed['media'][0]['brief'] == 'a quiet street' and parsed['media'][1]['brief'] is None


def test_unknown_fields_are_ignored_and_unknown_kinds_are_left_alone():
    parsed = cloud_contact.parse_event(event(newer_field={'a': 1}, price_cents=500))
    assert parsed is not None and 'newer_field' not in parsed and 'price_cents' not in parsed
    payload = {'events': [event(), event(OTHER, kind='newer'), {'id': 'gift:00000000-0000-4000-8000-000000000009',
                                                                  'kind': 'gift', 'at': NOW}]}
    assert [item['id'] for item in cloud_contact.parse(payload)] == [ID]
    # The visit times of 'gift' events are read as before and never mix with contacts.
    assert cloud_events.validate(payload) == [{'id': 'gift:00000000-0000-4000-8000-000000000009', 'at': NOW}]
    assert cloud_contact.parse({'events': 'x'}) == [] and cloud_contact.parse(None) == []


ASSET = {'type': 'photo', 'asset_url': 'https://bside-moon.cn/assets/a.png', 'sha256': 'a' * 64, 'bytes': 1000}


@pytest.mark.parametrize('change', [
    {'id': 'contact:../x'}, {'id': 'gift:00000000-0000-4000-8000-000000000001'}, {'id': None}, {'kind': 'gift'},
    {'purpose': 'Recall'}, {'purpose': ''}, {'purpose': 'x' * 33}, {'channel': 'sms'}, {'channel': None},
    {'deliver_after': '5'}, {'deliver_after': True}, {'deliver_after': 0}, {'at': 1.5},
    {'expires': NOW - 100}, {'expires': NOW - 60},
    {'qq_wait_seconds': 3601}, {'qq_wait_seconds': -1}, {'qq_wait_seconds': True},
    {'title': ''}, {'title': 'x' * 41}, {'title': 5},
    {'brief': ''}, {'brief': 'x' * 2001}, {'brief': 5}, {'brief': None},
    {'letter_length': {'min': 500, 'max': 100}}, {'letter_length': {'min': 1}}, {'letter_length': 'long'},
    {'qq_format': 'sms'}, {'quiet_hours': 'yes'}, {'billing': 'discount'},
    {'body': ''}, {'body': 'x' * 6001}, {'body': 5},
    {'media': [{'type': 'photo'}] * 4}, {'media': [{'type': 'gif'}]}, {'media': 'photo'},
    {'media': [{'type': 'photo', 'brief': 'x' * 301}]},
    {'media': [{**ASSET, 'sha256': 'A' * 64}]}, {'media': [{**ASSET, 'sha256': 'a' * 63}]},
    {'media': [{**ASSET, 'bytes': 10 * 1024 * 1024 + 1}]}, {'media': [{**ASSET, 'bytes': 0}]},
    {'media': [{**ASSET, 'asset_url': 'http://bside-moon.cn/assets/a.png'}]},
    {'media': [{**ASSET, 'asset_url': 'https://download.bside-moon.cn/installers/a.png'}]},
    {'media': [{**ASSET, 'asset_url': 'https://evil.example/a.png'}]},
    {'media': [{**ASSET, 'asset_url': 'https://bside-moon.cn.evil.example/a.png'}]},
    {'media': [{**ASSET, 'asset_url': 'https://user@bside-moon.cn/a.png'}]},
    {'media': [{**ASSET, 'asset_url': 'https://bside-moon.cn:8443/a.png'}]},
    {'media': [{**ASSET, 'type': 'video'}]},
    {'window': ['19:00']}, {'window': ['19:00', '22:00', '23:00']}, {'window': ['22:00', '19:00']},
    {'window': ['19:00', '19:00']}, {'window': ['19:00', '24:00']}, {'window': ['7:00', '22:00']}, {'window': '19:00-22:00'},
])
def test_an_event_that_breaks_the_schema_is_dropped(change):
    assert cloud_contact.parse_event(event(**change)) is None


def test_a_fixed_picture_from_a_host_the_client_trusts_is_kept():
    parsed = cloud_contact.parse_event(event(media=[ASSET]))
    assert parsed['media'] == [{**ASSET, 'brief': None}]
    from runtime.official_endpoints import COMPONENT_COS_HOSTS
    for host in ('api.bside-moon.cn', 'bside-moon.cn', *COMPONENT_COS_HOSTS):
        assert cloud_contact.parse_event(event(media=[{**ASSET, 'asset_url': f'https://{host}/assets/a.png'}])) is not None
    assert cloud_contact.trusted_hosts() == {'bside-moon.cn', 'api.bside-moon.cn', *COMPONENT_COS_HOSTS}


def test_the_introduction_picture_is_accepted_with_its_exact_address_checksum_and_size():
    picture = {'type': 'photo', 'asset_url': 'https://bside-moon.cn/assets/olivia-intro.jpg', 'bytes': 255817,
               'sha256': 'c3da242abd46793dfcca1f96eb3a09a377f964371cd4d6fd4e4d69d2feb76038'}
    parsed = cloud_contact.parse_event(event(brief=None, body='欢迎。', purpose='intro', media=[picture]))
    assert parsed['media'] == [{**picture, 'brief': None}]
    for lie in ({'bytes': 255818}, {'sha256': picture['sha256'][:-1] + '7'}, {'asset_url': picture['asset_url'].replace('bside-moon', 'bside-moon2')}):
        accepted = cloud_contact.parse_event(event(brief=None, body='欢迎。', media=[{**picture, **lie}]))
        assert (accepted is None) == ('asset_url' in lie)  # Only the address is checked here; size and checksum on download.


def test_a_fixed_text_replaces_the_brief():
    parsed = cloud_contact.parse_event(event(brief=None, body='欢迎来到这里。', title='欢迎'))
    assert parsed['body'] == '欢迎来到这里。' and parsed['brief'] is None
    assert cloud_contact.parse_event(event(brief=None)) is None  # Neither words to write nor a text to send.


def test_a_window_must_run_inside_one_day():
    assert cloud_contact.parse_event(event(window=['19:00', '22:00']))['window'] == ['19:00', '22:00']
    assert cloud_contact.parse_event(event(window=None))['window'] is None
    assert cloud_contact.parse_event(event(window=['00:00', '23:59']))['window'] == ['00:00', '23:59']
    assert cloud_contact.parse_event(event(window=['22:00', '06:00'])) is None


# --- what is kept


def test_a_contact_is_counted_once_and_the_visits_stay(tmp_path):
    cloud_events.record(tmp_path, [{'id': 'gift:00000000-0000-4000-8000-000000000009', 'at': NOW}], NOW)
    assert cloud_contact.record(tmp_path, [cloud_contact.parse_event(event())], NOW) == [ID]
    assert cloud_contact.record(tmp_path, [cloud_contact.parse_event(event())], NOW + 120) == []
    cloud_events.record(tmp_path, [], NOW + 240)  # A later visit check does not drop the contact.
    assert [item['id'] for item in cloud_contact.pending(tmp_path)] == [ID]
    state = json.loads((tmp_path / 'proactive' / 'cloud-events.json').read_text(encoding='utf-8'))
    assert set(state) == {'visits', 'contacts'} and state['visits']
    assert cloud_events.visits(tmp_path, '2026-10-13') == ['20:00']
    cloud_contact.settle(tmp_path, ID, 'delivered', NOW, channel='letter')
    assert cloud_contact.record(tmp_path, [cloud_contact.parse_event(event())], NOW + 300) == []  # Handled: never again.


def test_the_file_keeps_the_pending_ones_and_the_last_200_handled(tmp_path):
    for number in range(230):
        contact_id = 'contact:00000000-0000-4000-8000-%012d' % number
        cloud_contact.record(tmp_path, [cloud_contact.parse_event(event(contact_id))], NOW)
        cloud_contact.settle(tmp_path, contact_id, 'delivered', NOW + number, channel='letter')
    cloud_contact.record(tmp_path, [cloud_contact.parse_event(event(OTHER))], NOW)
    handled = cloud_contact.handled(tmp_path)
    assert len(handled) == 200 and 'contact:00000000-0000-4000-8000-%012d' % 229 in handled
    assert 'contact:00000000-0000-4000-8000-%012d' % 0 not in handled
    assert [item['id'] for item in cloud_contact.pending(tmp_path)] == [OTHER]
    assert all(set(entry) == {'status', 'channel', 'error_code', 'at', 'acked', 'ack_tries', 'ack_after'}
               for entry in handled.values())


def test_a_damaged_entry_cannot_stop_the_others(tmp_path):
    kept(tmp_path, event())
    path = tmp_path / 'proactive' / 'cloud-events.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    state['contacts']['pending']['contact:broken'] = {'id': 'contact:broken'}
    state['contacts']['handled']['contact:broken'] = 'x'
    path.write_text(json.dumps(state), encoding='utf-8')
    assert cloud_contact.due(tmp_path, NOW)['id'] == ID


# --- when


def test_a_contact_waits_for_deliver_after_and_is_dropped_at_its_expiry(tmp_path):
    kept(tmp_path, event(deliver_after=NOW + 600, expires=NOW + 7200))
    assert cloud_contact.due(tmp_path, NOW) is None
    assert cloud_contact.due(tmp_path, NOW + 599) is None
    assert cloud_contact.due(tmp_path, NOW + 600)['id'] == ID
    assert cloud_contact.due(tmp_path, NOW + 7199)['id'] == ID
    assert cloud_contact.due(tmp_path, NOW + 7200) is None
    cloud_contact.expire(tmp_path, NOW + 7199)
    assert [item['id'] for item in cloud_contact.pending(tmp_path)] == [ID]
    cloud_contact.expire(tmp_path, NOW + 7200)
    assert cloud_contact.pending(tmp_path) == [] and cloud_contact.handled(tmp_path)[ID]['status'] == 'expired'


def test_the_oldest_due_contact_goes_first(tmp_path):
    kept(tmp_path, event(THIRD, deliver_after=NOW - 10), event(OTHER, deliver_after=NOW - 500),
         event(ID, deliver_after=NOW - 100))
    assert cloud_contact.due(tmp_path, NOW)['id'] == OTHER
    cloud_contact.settle(tmp_path, OTHER, 'delivered', NOW, channel='letter')
    assert cloud_contact.due(tmp_path, NOW)['id'] == ID


def test_quiet_hours_are_kept_unless_the_contact_says_otherwise(tmp_path):
    kept(tmp_path, event(ID, deliver_after=at(6), expires=at(23)),
         event(OTHER, quiet_hours=False, deliver_after=at(6), expires=at(23)))
    early = at(7, 40)
    assert cloud_contact.due(tmp_path, early)['id'] == OTHER  # The built-in quiet hours end at 08:30.
    cloud_contact.settle(tmp_path, OTHER, 'delivered', early, channel='letter')
    assert cloud_contact.due(tmp_path, early) is None
    assert cloud_contact.due(tmp_path, at(8, 29)) is None
    assert cloud_contact.due(tmp_path, at(8, 30))['id'] == ID


def test_the_clouds_quiet_hours_replace_the_built_in_ones_for_contacts(tmp_path):
    proactive_config.adopt({'version': 'v1', 'rules': {'quiet_hours': ['21:00', '07:00']}})
    kept(tmp_path, event(expires=at(23) + 86400))
    assert cloud_contact.due(tmp_path, at(20, 59))['id'] == ID
    assert cloud_contact.due(tmp_path, at(21)) is None
    assert cloud_contact.due(tmp_path, at(2, 0, day=14)) is None
    assert cloud_contact.due(tmp_path, at(7, 0, day=14))['id'] == ID


def test_a_contact_inside_its_window_is_due_and_outside_it_waits(tmp_path):
    kept(tmp_path, event(window=['19:00', '22:00'], deliver_after=at(12), expires=at(12, day=16)))
    assert cloud_contact.due(tmp_path, at(14)) is None
    assert cloud_contact.due(tmp_path, at(18, 59)) is None
    assert cloud_contact.due(tmp_path, at(19))['id'] == ID
    assert cloud_contact.due(tmp_path, at(21, 59))['id'] == ID
    assert cloud_contact.due(tmp_path, at(22)) is None
    assert cloud_contact.due(tmp_path, at(23, 30)) is None


def test_a_contact_missed_today_goes_out_in_the_next_days_window(tmp_path):
    kept(tmp_path, event(window=['19:00', '22:00'], deliver_after=at(12), expires=at(23, day=14)))
    assert cloud_contact.due(tmp_path, at(23, 30)) is None
    cloud_contact.expire(tmp_path, at(23, 30))
    assert cloud_contact.pending(tmp_path)  # Still waiting: it has not expired.
    assert cloud_contact.due(tmp_path, at(10, day=14)) is None
    assert cloud_contact.due(tmp_path, at(20, day=14))['id'] == ID


def test_a_contact_that_never_met_its_window_expires_unsent(tmp_path):
    kept(tmp_path, event(window=['19:00', '22:00'], deliver_after=at(12), expires=at(18, 30)))
    assert cloud_contact.due(tmp_path, at(18, 29)) is None
    cloud_contact.expire(tmp_path, at(18, 30))
    assert cloud_contact.handled(tmp_path)[ID]['status'] == 'expired'


# --- which channel


def routed(event_fields, now=NOW, *, qq=True, preferred='qq', **state):
    parsed = {**cloud_contact.parse_event(event(**event_fields)), 'started_at': None, 'qq_failed': False,
              'attempts': 0, **state}
    return cloud_contact.route(parsed, now, qq, lambda: preferred)


def test_letter_and_qq_each_use_their_own_channel():
    assert routed({'channel': 'letter'}) == 'letter'
    assert routed({'channel': 'letter'}, qq=False) == 'letter'
    assert routed({'channel': 'qq'}) == 'qq'
    assert routed({'channel': 'qq'}, qq=False) is None  # It waits for QQ; it never turns into a letter by itself.


def test_preferred_follows_the_channel_the_owner_uses_most():
    assert routed({'channel': 'preferred'}, preferred='qq') == 'qq'
    assert routed({'channel': 'preferred'}, preferred='letter') == 'letter'


def test_qq_then_letter_uses_qq_when_it_is_connected():
    assert routed({'channel': 'qq_then_letter'}) == 'qq'


def test_qq_then_letter_writes_the_letter_once_the_wait_has_passed_without_qq():
    began = NOW - 100
    assert routed({'channel': 'qq_then_letter', 'qq_wait_seconds': 180}, qq=False, started_at=began) is None
    assert routed({'channel': 'qq_then_letter', 'qq_wait_seconds': 180}, now=began + 179, qq=False, started_at=began) is None
    assert routed({'channel': 'qq_then_letter', 'qq_wait_seconds': 180}, now=began + 180, qq=False, started_at=began) == 'letter'
    assert routed({'channel': 'qq_then_letter', 'qq_wait_seconds': 0}, qq=False) == 'letter'


def test_qq_then_letter_stops_trying_qq_when_the_wait_is_over_or_qq_failed():
    fields = {'channel': 'qq_then_letter', 'qq_wait_seconds': 180}
    assert routed(fields, now=NOW, started_at=NOW - 60, attempts=1) == 'qq'  # Still inside the wait.
    assert routed(fields, now=NOW, started_at=NOW - 181, attempts=1) == 'letter'
    assert routed(fields, now=NOW, started_at=NOW - 10, attempts=1, qq_failed=True) == 'letter'
    assert routed({**fields, 'qq_wait_seconds': 0}, now=NOW, started_at=NOW) == 'qq'  # The first try is always made.


# --- carrying it out


class Cloud:
    """What the tick needs from the rest of the client, recorded."""
    def __init__(self, qq=True, preferred='qq', outcomes=None):
        self.qq, self.preferred, self.outcomes = qq, preferred, list(outcomes or [])
        self.delivered, self.acks, self.context = [], [], []
        self.fail_acks = False

    async def deliver(self, channel, contact):
        self.delivered.append((channel, contact['id'], contact['attempts']))
        self.context.append(contact_context.current())
        return self.outcomes.pop(0) if self.outcomes else Outcome('delivered')

    async def post(self, payload):
        if self.fail_acks:
            raise RuntimeError('offline')
        self.acks.append(payload)

    def run(self, root, now):
        return asyncio.run(cloud_contact.tick(root, now, qq_ready=lambda: self.qq, preferred=lambda: self.preferred,
                                              deliver=self.deliver, slot=no_slot(), post=self.post))


def test_a_due_contact_is_delivered_reported_and_not_repeated(tmp_path):
    kept(tmp_path, event(purpose='intro'))
    cloud = Cloud()
    assert cloud.run(tmp_path, NOW) == ID
    assert cloud.delivered == [('letter', ID, 1)] and cloud.context == [ID]
    cloud.run(tmp_path, NOW + 30)
    assert cloud.delivered == [('letter', ID, 1)]
    assert cloud.acks == [{'id': ID, 'status': 'delivered', 'channel': 'letter', 'error_code': None}]
    cloud.run(tmp_path, NOW + 60)
    assert len(cloud.acks) == 1  # Reported once.
    assert contact_context.current() is None  # The context ends with the task.


def test_the_report_carries_what_could_not_be_attached(tmp_path):
    kept(tmp_path, event(media=[{'type': 'video'}]))
    cloud = Cloud(outcomes=[Outcome('delivered', 'MEDIA_VIDEO_UNSUPPORTED')])
    cloud.run(tmp_path, NOW)
    cloud.run(tmp_path, NOW + 30)
    assert cloud.acks[0]['status'] == 'delivered' and cloud.acks[0]['error_code'] == 'MEDIA_VIDEO_UNSUPPORTED'


def test_only_one_contact_is_carried_out_at_a_time(tmp_path):
    kept(tmp_path, event(OTHER, deliver_after=NOW - 500), event(ID))
    cloud = Cloud()
    assert cloud.run(tmp_path, NOW) == OTHER
    assert [item['id'] for item in cloud_contact.pending(tmp_path)] == [ID]
    assert cloud.run(tmp_path, NOW + 30) == ID


def test_a_contact_waiting_for_qq_does_not_hold_back_the_ones_behind_it(tmp_path):
    kept(tmp_path, event(OTHER, channel='qq', deliver_after=NOW - 500), event(ID, channel='letter'))
    cloud = Cloud(qq=False)
    assert cloud.run(tmp_path, NOW) == ID
    assert [(channel, contact_id) for channel, contact_id, _ in cloud.delivered] == [('letter', ID)]
    assert [item['id'] for item in cloud_contact.pending(tmp_path)] == [OTHER]
    cloud.qq = True
    assert cloud.run(tmp_path, NOW + 30) == OTHER


def test_her_own_initiative_holds_the_contact_back_and_the_contact_holds_hers_back(tmp_path):
    from runtime.reply.proactive_runtime import contact_slot
    kept(tmp_path)
    server = SimpleNamespace()
    cloud = Cloud()
    async def scenario():
        with contact_slot(server, 'letter') as hers:  # Her own initiative is in progress.
            assert hers
            await cloud_contact.tick(tmp_path, NOW, qq_ready=lambda: True, preferred=lambda: 'qq', deliver=cloud.deliver,
                                     slot=lambda: contact_slot(server, 'contact'), post=cloud.post)
        assert cloud.delivered == []
        async def during(channel, contact):
            with contact_slot(server, 'letter') as hers:
                assert not hers  # Hers waits while a contact is being written.
            return Outcome('delivered')
        await cloud_contact.tick(tmp_path, NOW + 30, qq_ready=lambda: True, preferred=lambda: 'qq', deliver=during,
                                 slot=lambda: contact_slot(server, 'contact'), post=cloud.post)
    asyncio.run(scenario())
    assert cloud_contact.handled(tmp_path)[ID]['status'] == 'delivered' and not server._jev_contact_channel


def test_a_failed_contact_is_tried_again_a_limited_number_of_times(tmp_path):
    kept(tmp_path)
    cloud = Cloud(outcomes=[Outcome('retry', 'LLM_UNAVAILABLE')] * 5)
    clock = NOW
    for _ in range(6):
        cloud.run(tmp_path, clock)
        clock += 300
    assert len(cloud.delivered) == cloud_contact.MAX_ATTEMPTS
    cloud.run(tmp_path, clock)
    assert cloud.acks == [{'id': ID, 'status': 'failed', 'channel': 'letter', 'error_code': 'LLM_UNAVAILABLE'}]


def test_a_retry_waits_before_the_next_try(tmp_path):
    kept(tmp_path)
    cloud = Cloud(outcomes=[Outcome('retry', 'LLM_UNAVAILABLE')])
    cloud.run(tmp_path, NOW)
    cloud.run(tmp_path, NOW + 30)
    assert len(cloud.delivered) == 1
    cloud.run(tmp_path, NOW + cloud_contact.RETRY_SECONDS)
    assert len(cloud.delivered) == 2


def test_nothing_that_never_began_counts_as_an_attempt(tmp_path):
    kept(tmp_path)
    cloud = Cloud(outcomes=[Outcome('busy')] * 5)
    clock = NOW
    for _ in range(5):
        cloud.run(tmp_path, clock)
        clock += 120
    assert len(cloud.delivered) == 5 and cloud_contact.pending(tmp_path)[0]['attempts'] == 0
    cloud.run(tmp_path, clock)
    assert cloud_contact.handled(tmp_path)[ID]['status'] == 'delivered'


def test_a_delivery_that_may_have_reached_the_owner_is_never_repeated(tmp_path):
    kept(tmp_path, event(channel='qq_then_letter'))
    cloud = Cloud(outcomes=[Outcome('failed', 'DELIVERY_UNCONFIRMED', False)])
    cloud.run(tmp_path, NOW)
    assert [channel for channel, *_ in cloud.delivered] == ['qq']  # No letter on top of a message that may be there.
    cloud.run(tmp_path, NOW + 30)
    assert cloud.acks[0]['status'] == 'failed' and cloud.acks[0]['error_code'] == 'DELIVERY_UNCONFIRMED'


def test_qq_then_letter_writes_the_letter_at_once_when_qq_fails(tmp_path):
    kept(tmp_path, event(channel='qq_then_letter'))
    cloud = Cloud(outcomes=[Outcome('failed', 'PERSONAL_CHAT_GENERATION_FAILED'), Outcome('delivered')])
    cloud.run(tmp_path, NOW)
    assert [channel for channel, *_ in cloud.delivered] == ['qq', 'letter']
    cloud.run(tmp_path, NOW + 30)
    assert cloud.acks[0]['channel'] == 'letter' and cloud.acks[0]['status'] == 'delivered'


def test_qq_then_letter_waits_for_qq_then_falls_back_to_the_letter(tmp_path):
    kept(tmp_path, event(channel='qq_then_letter', qq_wait_seconds=180))
    cloud = Cloud(qq=False)
    cloud.run(tmp_path, NOW)
    cloud.run(tmp_path, NOW + 120)
    assert cloud.delivered == []
    cloud.run(tmp_path, NOW + 181)
    assert [channel for channel, *_ in cloud.delivered] == ['letter']


def test_qq_then_letter_writes_the_letter_when_qq_stays_busy_for_the_whole_wait(tmp_path):
    kept(tmp_path, event(channel='qq_then_letter', qq_wait_seconds=180))
    cloud = Cloud(outcomes=[Outcome('busy')] * 3)
    cloud.run(tmp_path, NOW)
    cloud.run(tmp_path, NOW + 120)
    assert [channel for channel, *_ in cloud.delivered] == ['qq', 'qq']
    cloud.run(tmp_path, NOW + 240)
    assert [channel for channel, *_ in cloud.delivered] == ['qq', 'qq', 'qq', 'letter']
    assert cloud.delivered[-1][2] == 1  # The busy tries were not attempts.
    assert cloud_contact.handled(tmp_path)[ID]['channel'] == 'letter'


def test_an_answer_to_his_purchase_tries_qq_again_at_the_next_check(tmp_path):
    kept(tmp_path, event(purpose='gift_goods', channel='qq_then_letter', qq_wait_seconds=180),
         event(OTHER, channel='qq_then_letter', qq_wait_seconds=180))
    cloud = Cloud(outcomes=[Outcome('busy')] * 2)
    cloud.run(tmp_path, NOW)
    assert {item['id']: item['retry_after'] for item in cloud_contact.pending(tmp_path)}[ID] == NOW
    cloud.run(tmp_path, NOW + 30)
    assert {item['id']: item['retry_after'] for item in cloud_contact.pending(tmp_path)}[ID] == NOW + 30
    cloud.run(tmp_path, NOW + 60)
    assert cloud_contact.handled(tmp_path)[ID]['status'] == 'delivered'


def test_qq_then_letter_still_uses_qq_when_it_comes_back_inside_the_wait(tmp_path):
    kept(tmp_path, event(channel='qq_then_letter', qq_wait_seconds=180))
    cloud = Cloud(qq=False)
    cloud.run(tmp_path, NOW)
    cloud.qq = True
    cloud.run(tmp_path, NOW + 100)
    assert [channel for channel, *_ in cloud.delivered] == ['qq']


def test_preferred_resolves_to_the_owners_channel(tmp_path):
    kept(tmp_path, event(channel='preferred'))
    cloud = Cloud(preferred='letter')
    cloud.run(tmp_path, NOW)
    assert [channel for channel, *_ in cloud.delivered] == ['letter']


def test_an_expired_contact_is_reported_not_sent(tmp_path):
    kept(tmp_path, event(expires=NOW + 100))
    cloud = Cloud(qq=False)
    cloud.qq = False
    kept(tmp_path, event(OTHER, channel='qq', expires=NOW + 100))
    cloud.run(tmp_path, NOW)  # QQ is down: the second one waits.
    cloud.run(tmp_path, NOW + 100)
    cloud.run(tmp_path, NOW + 130)
    statuses = {ack['id']: ack['status'] for ack in cloud.acks}
    assert statuses[OTHER] == 'expired'
    assert [d for d in cloud.delivered if d[1] == OTHER] == []


def test_the_clouds_master_switch_turns_every_contact_down(tmp_path):
    proactive_config.adopt({'version': 'v1', 'rules': {'contact_enabled': False}})
    kept(tmp_path)
    cloud = Cloud()
    cloud.run(tmp_path, NOW)
    cloud.run(tmp_path, NOW + 30)
    assert cloud.delivered == []
    assert cloud.acks == [{'id': ID, 'status': 'skipped', 'channel': None, 'error_code': 'CONTACT_DISABLED'}]


def test_a_contact_task_runs_under_the_contacts_own_billing_turn(tmp_path, monkeypatch):
    from runtime.reply import jev_billing
    monkeypatch.setenv('OLIVIA_JEV_BILLING_ENABLED', '1')
    jev_billing.configure_account(lambda: 'olivia-synthetic-key')
    seen = []
    async def deliver(channel, contact):
        with jev_billing.billing_scope('personal-chat:other-id:0'):  # Nested scopes keep the contact's turn.
            seen.append(jev_billing.billing_headers()['X-Olivia-Turn-Id'])
        return Outcome('delivered')
    kept(tmp_path)
    asyncio.run(cloud_contact.tick(tmp_path, NOW, qq_ready=lambda: True, preferred=lambda: 'qq', deliver=deliver,
                                   slot=no_slot(), post=Cloud().post))
    assert seen == [ID]


# --- reporting


def test_a_report_that_fails_is_tried_again_later_and_five_times_at_most(tmp_path):
    kept(tmp_path)
    cloud = Cloud()
    cloud.run(tmp_path, NOW)
    cloud.fail_acks = True
    clock = NOW + 30
    tries = []
    for _ in range(40):
        before = cloud_contact.handled(tmp_path)[ID]['ack_tries']
        cloud.run(tmp_path, clock)
        after = cloud_contact.handled(tmp_path)[ID]['ack_tries']
        if after != before:
            tries.append(clock)
        clock += 30 * 60
    assert len(tries) == cloud_contact.MAX_ACK_TRIES == 5
    assert not cloud_contact.handled(tmp_path)[ID]['acked']
    assert all(later - earlier > 0 for earlier, later in zip(tries, tries[1:]))


def test_a_report_that_gets_through_after_failures_is_sent_once(tmp_path):
    kept(tmp_path)
    cloud = Cloud()
    cloud.fail_acks = True
    cloud.run(tmp_path, NOW)
    cloud.run(tmp_path, NOW + 30)
    entry = cloud_contact.handled(tmp_path)[ID]
    assert entry['ack_tries'] == 1 and not entry['acked']
    cloud.run(tmp_path, NOW + 31)  # Backing off: not yet.
    assert cloud_contact.handled(tmp_path)[ID]['ack_tries'] == 1
    cloud.fail_acks = False
    cloud.run(tmp_path, NOW + 3600)
    cloud.run(tmp_path, NOW + 7200)
    assert len(cloud.acks) == 1 and cloud_contact.handled(tmp_path)[ID]['acked']


def test_a_settled_contact_cannot_be_settled_twice(tmp_path):
    kept(tmp_path)
    assert cloud_contact.settle(tmp_path, ID, 'delivered', NOW, channel='qq')
    assert not cloud_contact.settle(tmp_path, ID, 'failed', NOW + 1, channel='qq')
    assert cloud_contact.handled(tmp_path)[ID]['status'] == 'delivered'
    assert not cloud_contact.settle(tmp_path, OTHER, 'delivered', NOW)
    kept(tmp_path, event(OTHER))
    assert not cloud_contact.settle(tmp_path, OTHER, 'sent', NOW)  # Only the four known ends.


# --- the cloud's side of the conversation


def serve(handlers):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    app = web.Application()
    for method, path, handler in handlers:
        app.router.add_route(method, path, handler)
    return TestServer(app)


def test_the_events_check_says_what_this_client_can_do_and_keeps_contacts(tmp_path, monkeypatch):
    from aiohttp import web
    import original_client_relay_api as relay
    seen = []
    async def events(request):
        seen.append(('events', request.headers.get('X-Olivia-Events'), request.headers.get('Authorization')))
        return web.json_response({'events': [event(), {'id': 'gift:00000000-0000-4000-8000-000000000009',
                                                      'kind': 'gift', 'at': NOW}, event(OTHER, kind='later')]})
    async def scenario():
        server = serve([('GET', '/v1/events', events)])
        await server.start_server()
        try:
            monkeypatch.setattr(relay, 'RELAY_BASE', str(server.make_url('/v1')))
            monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'olivia-synthetic')
            monkeypatch.setenv('OLIVIA_GPU_API_URL', str(server.make_url('')).rstrip('/'))
            await cloud_events.poll(tmp_path)
        finally:
            await server.close()
    asyncio.run(scenario())
    assert seen == [('events', 'contact.v1', 'Bearer olivia-synthetic')]
    assert [item['id'] for item in cloud_contact.pending(tmp_path)] == [ID]
    assert cloud_events.visits(tmp_path, '2026-10-13') == ['20:00']


def test_a_report_is_posted_to_the_cloud_with_the_fields_it_expects(tmp_path, monkeypatch):
    from aiohttp import web
    import original_client_relay_api as relay
    posted = []
    async def ack(request):
        posted.append((await request.json(), request.headers.get('X-Olivia-Events')))
        return web.json_response({'ok': True})
    kept(tmp_path)
    cloud_contact.settle(tmp_path, ID, 'delivered', NOW, channel='qq', error_code='MEDIA_VIDEO_UNAVAILABLE')
    async def scenario():
        server = serve([('POST', '/v1/events/ack', ack)])
        await server.start_server()
        try:
            monkeypatch.setattr(relay, 'RELAY_BASE', str(server.make_url('/v1')))
            monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'olivia-synthetic')
            monkeypatch.setenv('OLIVIA_GPU_API_URL', str(server.make_url('')).rstrip('/'))
            await cloud_contact.flush_acks(tmp_path, NOW)
        finally:
            await server.close()
    asyncio.run(scenario())
    assert posted == [({'id': ID, 'status': 'delivered', 'channel': 'qq', 'error_code': 'MEDIA_VIDEO_UNAVAILABLE'}, None)]
    assert cloud_contact.handled(tmp_path)[ID]['acked']


def test_a_report_without_an_account_waits_instead_of_being_lost(tmp_path, monkeypatch):
    monkeypatch.delenv('OLIVIA_GPU_API_KEY', raising=False)
    kept(tmp_path)
    cloud_contact.settle(tmp_path, ID, 'delivered', NOW, channel='letter')
    asyncio.run(cloud_contact.flush_acks(tmp_path, NOW))
    assert not cloud_contact.handled(tmp_path)[ID]['acked'] and cloud_contact.handled(tmp_path)[ID]['ack_tries'] == 1


# --- every request made for a contact names it


def test_the_context_is_set_only_for_a_real_contact_id():
    assert contact_context.current() is None and contact_context.headers() == {} and contact_context.request_prefix() == ''
    with contact_context.using('not-a-contact'):
        assert contact_context.current() is None
    with contact_context.using(None):
        assert contact_context.current() is None
    with contact_context.using(ID):
        assert contact_context.headers() == {'X-Olivia-Contact-Id': ID}
        assert contact_context.request_prefix() == 'contact-00000000000040008000000000000001-'
        with contact_context.using(None):  # An id-less inner scope leaves the outer contact alone.
            assert contact_context.current() == ID
    assert contact_context.current() is None


def test_the_relay_llm_client_names_the_contact_and_labels_its_requests(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from llm_gateway import GatewayRequestScope, OpenAICompatibleAdapter
    from tests.llm.test_gateway import make_config
    monkeypatch.setenv('B03_TEST_KEY', 'TEST')
    seen = []
    async def handler(request):
        seen.append((request.headers.get('X-Olivia-Contact-Id'), request.headers.get('X-Request-ID')))
        return web.json_response({'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}]})
    async def scenario():
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handler)
        async with TestClient(TestServer(app)) as client:
            adapter = OpenAICompatibleAdapter(make_config(str(client.make_url('/v1'))))
            messages = ({'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'})
            await adapter.complete_scoped(messages, scope=GatewayRequestScope.BACKGROUND_REASONING, request_id='proactive:x:body')
            with contact_context.using(ID):
                await adapter.complete_scoped(messages, scope=GatewayRequestScope.BACKGROUND_REASONING, request_id='proactive:x:body')
                await adapter.complete_scoped(messages, scope=GatewayRequestScope.PERSONAL_CHAT_JSON, request_id='deadbeef')
    asyncio.run(scenario())
    assert seen[0][0] is None and seen[0][1] == 'proactive:x:body'
    assert seen[1] == (ID, 'contact-proactive:x:body') and seen[2] == (ID, 'contact-deadbeef')


def test_the_jev_client_names_the_contact_in_both_of_its_transports(monkeypatch):
    from runtime.reply.companion_decision import JevDecisionPort
    local = JevDecisionPort('http://127.0.0.1:8097/v1/companion/decide', token='synthetic-token')
    cloud = JevDecisionPort('https://api.bside-moon.cn/v1/companion/decide')
    from runtime.reply import jev_billing
    jev_billing.configure_account(lambda: 'olivia-synthetic-key')
    for port in (local, cloud):
        assert 'X-Olivia-Contact-Id' not in port.request_headers('a' * 64)
        with contact_context.using(ID):
            assert port.request_headers('a' * 64)['X-Olivia-Contact-Id'] == ID


def test_the_media_client_names_the_contact_and_its_orders_carry_the_prefix(monkeypatch):
    from aiohttp import web
    from runtime.remote_generation import RemoteGeneration
    seen, submitted = [], []
    async def capabilities(request):
        seen.append(request.headers.get('X-Olivia-Contact-Id'))
        return web.json_response({'kinds': ['tts'], 'shared_assets': []})
    async def submit(request):
        seen.append(request.headers.get('X-Olivia-Contact-Id'))
        submitted.append((request.headers['Idempotency-Key'], (await request.json())['request_id']))
        return web.json_response({'task_id': 'task-1', 'status': 'failed', 'stage': 'x', 'outputs': []})
    async def scenario():
        server = serve([('GET', '/v1/capabilities', capabilities), ('POST', '/v1/tasks', submit),
                        ('GET', '/v1/tasks/task-1', submit)])
        await server.start_server()
        try:
            api = RemoteGeneration(str(server.make_url('')).rstrip('/'), 'synthetic-token')
            await api.request('capabilities', {})
            with contact_context.using(ID):
                await api.request('capabilities', {})
                try:
                    await api.generate('tts', {'text': 'hello'}, 'unused.wav')
                except Exception:
                    pass  # Only the order that was placed matters here.
            try:
                await api.generate('tts', {'text': 'hello'}, 'unused.wav')
            except Exception:
                pass
        finally:
            await server.close()
    asyncio.run(scenario())
    assert seen[0] is None and seen[1] == ID and ID in seen[2:4]
    contact_order, plain_order = submitted[0], submitted[-1]
    assert contact_order[0] == contact_order[1] and contact_order[0].startswith('contact-00000000000040008000000000000001-')
    assert not plain_order[0].startswith('contact-') and len(contact_order[0]) <= 80


# --- the fixed picture


class Download:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
        self.content = self

    async def iter_chunked(self, size):
        for start in range(0, len(self.data), size):
            yield self.data[start:start + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def png(color=(10, 20, 30)):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    return buffer.getvalue()


def fetch(monkeypatch, tmp_path, data, **asset):
    import aiohttp
    import hashlib
    class Session:
        def __init__(self, *a, **k):
            self.urls = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        def get(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            return Download(data)
    monkeypatch.setattr(aiohttp, 'ClientSession', Session)
    announced = {'asset_url': 'https://bside-moon.cn/assets/a.png', 'sha256': hashlib.sha256(data).hexdigest(),
                 'bytes': len(data), **asset}
    return asyncio.run(cloud_contact.fetch_photo(announced, tmp_path / 'media'))


def test_a_fixed_picture_with_the_announced_size_and_checksum_is_saved(monkeypatch, tmp_path):
    data = png()
    path = fetch(monkeypatch, tmp_path, data)
    assert path.parent == tmp_path / 'media' and path.name.startswith('photo-') and path.suffix == '.png'
    assert len(path.name) == len('photo-') + 32 + len('.png')
    from PIL import Image
    with Image.open(path) as image:
        assert image.format == 'PNG' and image.size == (8, 8)


def test_a_jpeg_is_saved_as_the_png_the_letter_and_the_photo_acknowledgement_expect(monkeypatch, tmp_path):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (12, 9), (200, 100, 50)).save(buffer, format='JPEG')
    path = fetch(monkeypatch, tmp_path, buffer.getvalue(), asset_url='https://bside-moon.cn/assets/olivia-intro.jpg')
    import re
    assert re.fullmatch(r'photo-[a-f0-9]{32}\.png', path.name)
    with Image.open(path) as image:
        assert image.format == 'PNG' and image.size == (12, 9)


@pytest.mark.parametrize('lie', [{'sha256': 'b' * 64}, {'bytes': 999}, {'bytes': 5}])
def test_a_picture_that_is_not_what_was_announced_is_refused(monkeypatch, tmp_path, lie):
    with pytest.raises(ValueError, match='CONTACT_ASSET_MISMATCH'):
        fetch(monkeypatch, tmp_path, png(), **lie)
    assert not (tmp_path / 'media').exists() or not list((tmp_path / 'media').iterdir())


def test_something_that_is_not_a_picture_is_refused_even_with_a_matching_checksum(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match='CONTACT_ASSET_INVALID'):
        fetch(monkeypatch, tmp_path, b'<html>not an image</html>')


def test_a_picture_is_only_fetched_from_a_trusted_host(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match='CONTACT_ASSET_UNTRUSTED'):
        fetch(monkeypatch, tmp_path, png(), asset_url='https://evil.example/a.png')
    with pytest.raises(ValueError, match='CONTACT_ASSET_UNTRUSTED'):
        fetch(monkeypatch, tmp_path, png(), asset_url='http://bside-moon.cn/a.png')


# --- telemetry


def test_the_outcome_is_written_as_a_contact_event_without_any_text(tmp_path):
    from runtime.diagnostics import reply_telemetry
    collector = reply_telemetry.Collector(tmp_path / 'telemetry', version='2.3.3')
    collector.account('olivia-synthetic-key')
    collector.schema = 3
    reply_telemetry.configure(collector)
    try:
        kept(tmp_path, event(purpose='gift_goods', brief='SYNTHETIC_PRIVATE_WORDS'))
        cloud_contact.settle(tmp_path, ID, 'delivered', NOW, channel='letter', error_code='MEDIA_PHOTO_SKIPPED')
        kept(tmp_path, event(OTHER, purpose='intro'))
        cloud_contact.settle(tmp_path, OTHER, 'expired', NOW)
        events = [item for item in collector.batch(3) if item['stage'] == 'contact']
        assert [(item['status'], item['channel'], item['purpose']) for item in events] == [
            ('ok', 'proactive', 'gift_goods'), ('skipped', 'proactive', 'intro')]
        assert 'SYNTHETIC_PRIVATE_WORDS' not in json.dumps(events) and ID not in json.dumps(events)
        assert not [item for item in collector.batch(1) if item['stage'] == 'contact']  # Old servers never see the stage.
    finally:
        reply_telemetry.configure(None)
