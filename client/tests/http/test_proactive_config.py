"""Wording and rules for what she starts herself come from the cloud, field by field, and default to today."""
import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from runtime import cloud_events, proactive_config
from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.initiative import Initiative

BEIJING = timezone(timedelta(hours=8))


def clock(hour, minute=0, day=13):
    return datetime(2026, 10, day, hour, minute, tzinfo=BEIJING).timestamp()


NOW = clock(12)

FULL = {
    'version': 'cfg-7',
    'templates': {name: f'TEXT-{name}' for name in proactive_config.TEMPLATES},
    'rules': {'initiative_media': {'letter': ['text'], 'qq': ['audio_speech', 'text']}, 'initiative_photo': True,
              'channel_policy': 'letter_first', 'max_per_day': 3, 'min_gap_minutes': 90,
              'quiet_hours': ['22:30', '09:00'], 'contact_enabled': False},
}


@pytest.fixture(autouse=True)
def fresh_config():
    proactive_config.configure(None)
    yield
    proactive_config.configure(None)


# --- validation


def test_a_complete_answer_is_taken_as_it_is():
    version, templates, rules = proactive_config.validate(FULL)
    assert version == 'cfg-7' and templates == FULL['templates'] and rules == FULL['rules']


def test_unknown_fields_are_ignored():
    version, templates, rules = proactive_config.validate({**FULL, 'future': 1, 'templates': {**FULL['templates'], 'new': 'x'},
                                                         'rules': {**FULL['rules'], 'new_rule': 5}})
    assert set(templates) == set(proactive_config.TEMPLATES) and 'new_rule' not in rules


@pytest.mark.parametrize('payload', [None, [], 'x', {}, {'version': ''}, {'version': 7}, {'version': '../x'},
                                     {'version': 'x' * 65}, {'rules': {}}])
def test_an_answer_without_a_usable_version_is_refused(payload):
    with pytest.raises(ValueError):
        proactive_config.validate(payload)


@pytest.mark.parametrize('name,bad', [
    ('initiative_planning', ''), ('initiative_planning', '   '), ('initiative_write', 'x' * 3001), ('diary_due', 5),
    ('contact_invitation', None), ('diary_due', 'a\x00b'),
])
def test_a_bad_template_falls_back_alone(name, bad):
    _, templates, _ = proactive_config.validate({'version': 'v', 'templates': {**FULL['templates'], name: bad}})
    assert name not in templates and len(templates) == 3
    assert proactive_config.validate({'version': 'v', 'templates': {name: 'x' * 3000}})[1] == {name: 'x' * 3000}


@pytest.mark.parametrize('name,bad', [
    ('initiative_media', 'text'), ('initiative_media', {'letter': ['photo']}), ('initiative_media', {'qq': 'audio_speech'}),
    ('initiative_media', {'qq': ['text', 'text']}), ('initiative_photo', 'yes'), ('initiative_photo', 1),
    ('channel_policy', 'random'), ('channel_policy', None), ('max_per_day', 11), ('max_per_day', -1),
    ('max_per_day', True), ('max_per_day', 2.5), ('min_gap_minutes', 1441), ('min_gap_minutes', '30'),
    ('quiet_hours', ['22:00']), ('quiet_hours', ['22:00', '22:00']), ('quiet_hours', ['25:00', '07:00']),
    ('quiet_hours', '22:00-07:00'), ('contact_enabled', 'false'), ('contact_enabled', 0),
])
def test_a_bad_rule_falls_back_alone(name, bad):
    _, _, rules = proactive_config.validate({'version': 'v', 'rules': {**FULL['rules'], name: bad}})
    assert name not in rules and len(rules) == len(FULL['rules']) - 1


def test_one_bad_channel_does_not_spoil_the_other_in_the_media_rule():
    _, _, rules = proactive_config.validate({'version': 'v', 'rules': {'initiative_media': {'letter': ['photo'], 'qq': ['text']}}})
    assert rules['initiative_media'] == {'qq': ['text']}
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_media': {'letter': [], 'qq': ['audio_speech', 'text']}}})
    assert proactive_config.media('letter') == [] and proactive_config.media('qq') == ['audio_speech', 'text']


def test_quiet_hours_may_be_null_and_may_pass_midnight():
    assert proactive_config.validate({'version': 'v', 'rules': {'quiet_hours': None}})[2] == {'quiet_hours': None}
    proactive_config.adopt({'version': 'v', 'rules': {'quiet_hours': ['23:00', '06:30']}})
    assert proactive_config.quiet_window() == (23 * 60, 6 * 60 + 30)
    assert proactive_config.initiative_blocked([], clock(23, 30)) == 'quiet_hours'
    assert proactive_config.initiative_blocked([], clock(5, 59)) == 'quiet_hours'
    assert proactive_config.initiative_blocked([], clock(6, 30)) is None


# --- defaults


def test_without_any_answer_every_value_is_the_built_in_one():
    assert proactive_config.status() == {'version': 'builtin', 'source': 'defaults'}
    for name, value in proactive_config.DEFAULT_RULES.items():
        assert proactive_config.rule(name) == value
    assert all(proactive_config.template(name) is None for name in proactive_config.TEMPLATES)
    assert proactive_config.media('letter') == ['text'] and proactive_config.media('qq') == ['audio_speech']
    assert proactive_config.rule('contact_enabled') is True and proactive_config.initiative_blocked([], NOW) is None
    assert proactive_config.quiet_window() == (0, 8 * 60 + 30)  # What her own initiative already keeps.


def test_a_value_read_from_the_config_cannot_be_changed_in_place():
    proactive_config.rule('initiative_media')['qq'].append('text')
    assert proactive_config.media('qq') == ['audio_speech']
    proactive_config.adopt(FULL)
    proactive_config.rule('quiet_hours').append('x')
    assert proactive_config.rule('quiet_hours') == ['22:30', '09:00']


# --- fetching, keeping and falling back


def serving(monkeypatch, handler):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    import original_client_relay_api as relay
    app = web.Application()
    app.router.add_get('/v1/proactive/config', handler)
    server = TestServer(app)
    async def start():
        await server.start_server()
        monkeypatch.setattr(relay, 'RELAY_BASE', str(server.make_url('/v1')))
        monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'olivia-synthetic')
        monkeypatch.setenv('OLIVIA_GPU_API_URL', str(server.make_url('')).rstrip('/'))
    return server, start


def refresh(monkeypatch, handler, now):
    server, start = serving(monkeypatch, handler)
    async def scenario():
        await start()
        try:
            return await proactive_config.refresh_if_due(now)
        finally:
            await server.close()
    return asyncio.run(scenario())


def test_the_answer_is_fetched_with_the_events_header_and_kept(monkeypatch, tmp_path):
    from aiohttp import web
    seen = []
    async def handler(request):
        seen.append(request.headers.get('X-Olivia-Events'))
        return web.json_response(FULL)
    proactive_config.configure(tmp_path)
    assert refresh(monkeypatch, handler, NOW) is True
    assert seen == ['contact.v1']
    assert proactive_config.status() == {'version': 'cfg-7', 'source': 'cloud'}
    assert proactive_config.template('diary_due') == 'TEXT-diary_due' and proactive_config.rule('max_per_day') == 3
    kept = json.loads((tmp_path / 'proactive' / 'config.json').read_text(encoding='utf-8'))
    assert kept == {'version': 'cfg-7', 'templates': FULL['templates'], 'rules': FULL['rules']}


def test_a_restart_without_the_cloud_uses_the_kept_answer(monkeypatch, tmp_path):
    from aiohttp import web
    async def handler(request):
        return web.json_response(FULL)
    proactive_config.configure(tmp_path)
    refresh(monkeypatch, handler, NOW)
    proactive_config.configure(tmp_path)  # A new start; the cloud cannot be reached.
    assert proactive_config.status() == {'version': 'cfg-7', 'source': 'cache'}
    assert proactive_config.rule('channel_policy') == 'letter_first' and proactive_config.template('initiative_write')
    monkeypatch.delenv('OLIVIA_GPU_API_KEY')
    assert asyncio.run(proactive_config.refresh_if_due(NOW)) is False
    assert proactive_config.status()['source'] == 'cache' and proactive_config.rule('max_per_day') == 3


def test_an_error_from_the_cloud_keeps_what_is_in_use(monkeypatch, tmp_path):
    from aiohttp import web
    proactive_config.configure(tmp_path)
    proactive_config.adopt(FULL)
    async def broken(request):
        return web.json_response({'error': {'code': 'nope'}}, status=500)
    assert refresh(monkeypatch, broken, NOW) is False
    async def invalid(request):
        return web.json_response({'version': '', 'rules': {'max_per_day': 0}})
    proactive_config.configure(tmp_path)
    assert refresh(monkeypatch, invalid, NOW) is False
    assert proactive_config.status() == {'version': 'cfg-7', 'source': 'cache'}
    assert proactive_config.rule('max_per_day') == 3


def test_a_missing_or_damaged_kept_answer_means_the_built_in_values(tmp_path):
    proactive_config.configure(tmp_path)
    assert proactive_config.status() == {'version': 'builtin', 'source': 'defaults'}
    (tmp_path / 'proactive').mkdir()
    for text in ('{not json', '[]', json.dumps({'version': '', 'rules': {'max_per_day': 1}})):
        (tmp_path / 'proactive' / 'config.json').write_text(text, encoding='utf-8')
        proactive_config.configure(tmp_path)
        assert proactive_config.status() == {'version': 'builtin', 'source': 'defaults'}
        assert proactive_config.rule('max_per_day') is None


def test_fields_that_fail_fall_back_but_the_rest_of_the_answer_applies(monkeypatch, tmp_path):
    from aiohttp import web
    async def handler(request):
        return web.json_response({**FULL, 'rules': {**FULL['rules'], 'max_per_day': 99, 'channel_policy': 'qq_first'},
                                  'templates': {'diary_due': 'x' * 4000, 'initiative_write': 'mine'}})
    proactive_config.configure(tmp_path)
    refresh(monkeypatch, handler, NOW)
    assert proactive_config.rule('max_per_day') is None and proactive_config.rule('channel_policy') == 'qq_first'
    assert proactive_config.template('diary_due') is None and proactive_config.template('initiative_write') == 'mine'


def test_the_answer_is_fetched_at_start_and_then_every_six_hours(monkeypatch, tmp_path):
    from aiohttp import web
    calls = []
    async def handler(request):
        calls.append(1)
        return web.json_response(FULL)
    server, start = serving(monkeypatch, handler)
    async def scenario():
        await start()
        try:
            proactive_config.configure(tmp_path)
            results = [await proactive_config.refresh_if_due(NOW),
                       await proactive_config.refresh_if_due(NOW + 3600),
                       await proactive_config.refresh_if_due(NOW + proactive_config.REFRESH_SECONDS - 1),
                       await proactive_config.refresh_if_due(NOW + proactive_config.REFRESH_SECONDS)]
            return results
        finally:
            await server.close()
    assert asyncio.run(scenario()) == [True, False, False, True] and len(calls) == 2


def test_a_failed_fetch_is_tried_again_soon_not_in_six_hours(monkeypatch, tmp_path):
    monkeypatch.delenv('OLIVIA_GPU_API_KEY', raising=False)
    proactive_config.configure(tmp_path)
    assert asyncio.run(proactive_config.refresh_if_due(NOW)) is False
    assert proactive_config._next_attempt == NOW + proactive_config.RETRY_SECONDS < NOW + proactive_config.REFRESH_SECONDS


# --- each rule takes effect


ORIGINAL = {
    'planning': '只输出 JSON：{"decision":"send或defer","format":"text","title":"简短标题"}。没有自然的具体话题就defer。',
    'write_text': '现在写这封主动信，只输出最终正文。无需逐条复述旧信。按平常文字信写。',
    'write_voice': '现在写这封主动信，只输出最终正文。无需逐条复述旧信。内容适合一段简短语音。',
    'diary': '今天是她日记里记下的日子：opportunity.remembered 写明了是什么事。'
             '纪念日就自然地提起并表达心意；对方今天要考试、体检或出行，就关心一句；约好的事就主动提起。'
             '只说这件记下的事本身，不编造当时的细节。',
    'invitation': '本次关系资格已由应用确认。自然地提出交换联系方式，并明确询问用户想要QQ还是微信；'
                  '不要提分数、解锁、系统门槛，不声称已经添加或用户已经同意，不编造账号或二维码。'
                  '可以围绕偶尔想随口聊两句来写，不要求用户转移所有通信，保留写信的习惯。',
}


def instruction(kind='shared_followup', **kwargs):
    import local_server
    return local_server._proactive_instruction({'kind': kind, 'relationship_tier': 'trusted'}, **kwargs)


def test_with_no_template_the_instruction_is_what_it_always_was():
    from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION
    assert ORIGINAL['planning'] in instruction(planning=True)
    assert ORIGINAL['write_text'] in instruction(planning=False)
    assert LETTER_PRESENTATION_INSTRUCTION in instruction(planning=False)
    assert ORIGINAL['write_voice'] in instruction(planning=False, mode='voice')
    assert LETTER_PRESENTATION_INSTRUCTION not in instruction(planning=False, mode='voice')
    assert ORIGINAL['diary'] in instruction('diary_due', planning=False)
    assert ORIGINAL['invitation'] in instruction('contact_invitation', planning=False)
    assert ORIGINAL['diary'] not in instruction(planning=False) and ORIGINAL['invitation'] not in instruction(planning=False)


def test_each_template_replaces_its_own_text_and_nothing_else():
    from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION
    proactive_config.adopt(FULL)
    planning = instruction(planning=True)
    assert 'TEXT-initiative_planning' in planning and ORIGINAL['planning'] not in planning
    assert planning.startswith('现在没有新的用户来信。')  # The shared opening and the closing guidance stay.
    writing = instruction(planning=False)
    assert 'TEXT-initiative_write' in writing and ORIGINAL['write_text'] not in writing
    assert LETTER_PRESENTATION_INSTRUCTION in writing  # The letter format is the client's own contract.
    diary = instruction('diary_due', planning=False)
    assert 'TEXT-diary_due' in diary and ORIGINAL['diary'] not in diary
    invitation = instruction('contact_invitation', planning=False)
    assert 'TEXT-contact_invitation' in invitation and ORIGINAL['invitation'] not in invitation
    assert 'TEXT-diary_due' not in invitation and 'TEXT-contact_invitation' not in diary


def test_a_template_that_is_missing_keeps_the_built_in_text_for_that_stage():
    proactive_config.adopt({'version': 'v', 'templates': {'diary_due': 'only this one'}})
    assert ORIGINAL['planning'] in instruction(planning=True) and ORIGINAL['write_text'] in instruction(planning=False)
    assert 'only this one' in instruction('diary_due', planning=False)


def media_packet(channel, available=('text', 'audio_speech')):
    return {'channel': channel, 'available_media': list(available), 'hard_gates': {'paused': False, 'blocked_reasons': []}}


def test_initiative_media_decides_what_the_cloud_is_offered():
    from runtime.reply import companion_proactive as proactive
    assert proactive._media(media_packet('letter')) == ['text'] and proactive._media(media_packet('qq')) == ['audio_speech']
    assert proactive._media(media_packet('wechat')) == ['audio_speech']
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_media': {'letter': ['text'], 'qq': ['audio_speech', 'text']}}})
    assert proactive._media(media_packet('qq')) == ['text', 'audio_speech']
    assert proactive._media(media_packet('qq', ['audio_speech'])) == ['audio_speech']
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_media': {'letter': ['text'], 'qq': ['text']}}})
    assert proactive._media(media_packet('qq')) == ['text']
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_media': {'letter': [], 'qq': ['audio_speech']}}})
    assert proactive._media(media_packet('letter')) == []
    assert proactive._local_defer(media_packet('letter'))['reason'] == 'no_medium'


def test_initiative_photo_lets_a_picture_follow_what_she_starts_herself():
    from runtime.image_reply import secondary_photo_allowed
    own = {'origin': 'proactive', 'letter_id': 'a'}
    reply = {'origin': 'user', 'letter_id': 'b'}
    assert not secondary_photo_allowed(own) and secondary_photo_allowed(reply)
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_photo': True}})
    assert secondary_photo_allowed(own) and secondary_photo_allowed(reply)
    assert not secondary_photo_allowed({**own, 'degraded_stages': ['x']})
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_photo': False}})
    assert not secondary_photo_allowed(own)


def owner_world(qq, letters, letters_on=True):
    def row(kind, name, age):
        base = {'letter_id': name, 'content': '在吗', 'created_at': NOW - age * 86400 + 60, 'letter_status': 'COMPLETED',
                'reply_text': '在'}
        return {**base, 'channel': 'qq', 'binding_id': 'b', 'source_messages': {name: 'x'}, 'delivery_status': 'DELIVERED'} \
            if kind == 'qq' else base
    return SimpleNamespace(
        store=SimpleNamespace(personal_chats=[row('qq', f'q{i}', 1 + i / 10) for i in range(qq)],
                              letters=[row('letter', f'l{i}', 1 + i / 10) for i in range(letters)]),
        _proactive_settings=lambda: {'enabled': letters_on})


@pytest.mark.parametrize('policy,qq_ready,letters_on,qq_rows,letter_rows,expected', [
    ('preferred', True, True, 5, 1, 'qq'), ('preferred', True, True, 1, 5, 'letter'),
    ('qq_first', True, True, 1, 5, 'qq'), ('qq_first', False, True, 5, 1, 'letter'), ('qq_first', False, False, 5, 1, None),
    ('letter_first', True, True, 5, 1, 'letter'), ('letter_first', True, False, 5, 1, 'qq'),
    ('letter_first', False, False, 1, 5, None),
])
def test_channel_policy_decides_where_her_own_contact_starts(policy, qq_ready, letters_on, qq_rows, letter_rows, expected):
    from runtime.personal_chat.engagement import initiating_channel
    proactive_config.adopt({'version': 'v', 'rules': {'channel_policy': policy}})
    assert initiating_channel(owner_world(qq_rows, letter_rows, letters_on), qq_ready, NOW) == expected


def started(name, at, status='DELIVERED', **extra):
    return {'letter_id': name, 'origin': 'proactive', 'channel': 'qq', 'created_at': at, 'delivery_status': status, **extra}


def test_the_limits_count_only_what_she_started_herself_and_that_was_sent():
    rows = [started('a', NOW - 3600), started('b', NOW - 7200), started('c', NOW - 90000),
            started('skipped', NOW - 60, 'SKIPPED'), started('failed', NOW - 60, 'FAILED'),
            started('contact', NOW - 60, contact_id='contact:x'), {'letter_id': 'u', 'created_at': NOW - 60, 'origin': 'user'}]
    assert proactive_config.initiative_blocked(rows, NOW) is None
    proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 2}})
    assert proactive_config.initiative_blocked(rows, NOW) == 'attempt_limit'
    assert proactive_config.initiative_blocked(rows[1:], NOW) is None  # One in the last day: below the limit.
    proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 0}})
    assert proactive_config.initiative_blocked([], NOW) == 'attempt_limit'
    proactive_config.adopt({'version': 'v', 'rules': {'min_gap_minutes': 90}})
    assert proactive_config.initiative_blocked(rows, NOW) == 'not_due'
    assert proactive_config.initiative_blocked(rows, NOW + 1800) is None  # 90 minutes after the latest of hers.
    assert proactive_config.initiative_blocked([], NOW) is None


def chat_initiative(rows, now):
    value = Initiative(rows, clock=lambda: now[0], interval=lambda: 60)
    value.received(PersonalMessage('qq', 'bot', 'owner', 'hello', 'hi'), SimpleNamespace(is_available=lambda: True))
    now[0] += 120
    return value


def chat_rows():
    """One message of hers two hours ago; the owner answered half an hour ago."""
    return [started('p', NOW - 7200),
            {'letter_id': 'u', 'origin': 'user', 'channel': 'qq', 'created_at': NOW - 1800, 'delivery_status': 'DELIVERED',
             'content': 'hi', 'reply_text': 'hello', 'source_messages': {'u': 'hi'}}]


def test_with_no_rule_her_qq_initiative_follows_the_built_in_cadence():
    now = [NOW]
    rows = chat_rows()
    assert chat_initiative(rows, now)._reason() == 'ready'
    now = [clock(8, 10)]
    assert chat_initiative(rows, now)._reason() == 'quiet_hours'


def test_the_cloud_can_add_quiet_hours_to_her_qq_initiative():
    proactive_config.adopt({'version': 'v', 'rules': {'quiet_hours': ['11:00', '14:00']}})
    now = [NOW - 120]
    assert chat_initiative(chat_rows(), now)._reason() == 'quiet_hours'
    now = [clock(15)]
    assert chat_initiative(chat_rows(), now)._reason() == 'ready'


def test_the_cloud_can_limit_how_many_her_qq_initiative_sends_and_how_often():
    rows = chat_rows()
    proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 1}})
    assert chat_initiative(rows, [NOW])._reason() == 'attempt_limit'
    proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 2}})
    assert chat_initiative(rows, [NOW])._reason() == 'ready'
    proactive_config.adopt({'version': 'v', 'rules': {'min_gap_minutes': 180}})
    assert chat_initiative(rows, [NOW])._reason() == 'not_due'
    proactive_config.adopt({'version': 'v', 'rules': {'min_gap_minutes': 60}})
    assert chat_initiative(rows, [NOW])._reason() == 'ready'


def test_a_contact_the_cloud_asked_for_does_not_use_up_her_own_allowance():
    rows = chat_rows()
    rows[0] = started('p', NOW - 7200, contact_id='contact:x')
    proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 1, 'min_gap_minutes': 600}})
    assert chat_initiative(rows, [NOW])._reason() == 'ready'


def test_the_cloud_rules_hold_back_her_letters_too():
    import local_server
    letters = [{'letter_id': 'l1', 'origin': 'proactive', 'letter_status': 'COMPLETED', 'created_at': int(NOW - 3600)}]
    saved = list(local_server.store.letters)
    local_server.store.letters[:] = letters
    try:
        proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 1}})
        assert proactive_config.initiative_blocked(local_server.store.letters, NOW) == 'attempt_limit'
        proactive_config.adopt({'version': 'v', 'rules': {'quiet_hours': ['11:00', '14:00']}})
        assert proactive_config.initiative_blocked(local_server.store.letters, NOW) == 'quiet_hours'
    finally:
        local_server.store.letters[:] = saved


def test_a_letter_scheduler_gate_reports_the_cloud_rule_as_its_reason(monkeypatch):
    import local_server
    monkeypatch.setattr(local_server, '_proactive_settings', lambda: {'enabled': True})
    monkeypatch.setattr(local_server, '_conversation_memory_ready_for_reply', lambda: True)
    monkeypatch.setattr(local_server, '_active_undelivered_letter', lambda *a, **k: None)
    monkeypatch.setattr('runtime.personal_chat.contact_activity.cross_channel_reason', lambda *a, **k: None)
    monkeypatch.setattr('runtime.reply.proactive_runtime.live_profile', lambda *a, **k: None)
    proactive_config.adopt({'version': 'v', 'rules': {'max_per_day': 0}})
    assert local_server._proactive_gate_reason() == 'attempt_limit'
    proactive_config.adopt({'version': 'v', 'rules': {'quiet_hours': ['00:00', '23:59']}})
    assert local_server._proactive_gate_reason() == 'quiet_hours'


# --- reporting the version in use


def test_the_diagnostic_bundle_names_the_config_version_and_where_it_came_from(tmp_path):
    from runtime.diagnostics.support_bundle import build_diagnostic_bundle
    from tests.http.test_diagnostic_support_bundle import _source
    def summary(config):
        source = _source()
        source['summary'] = {**source['summary'], 'proactive_config_version': config['version'],
                             'proactive_config_source': config['source']}
        with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
            return json.loads(archive.read('summary.json'))
    assert summary(proactive_config.status())['proactive_config_source'] == 'defaults'
    proactive_config.adopt(FULL)
    result = summary(proactive_config.status())
    assert result['proactive_config_version'] == 'cfg-7' and result['proactive_config_source'] == 'cloud'
    from runtime.diagnostics.support_bundle import DiagnosticBundleError
    bad = _source()
    bad['summary'] = {**bad['summary'], 'proactive_config_source': 'https://elsewhere.example'}
    with pytest.raises(DiagnosticBundleError):
        build_diagnostic_bundle(bad)
