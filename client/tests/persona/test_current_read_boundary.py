import json
from datetime import datetime, timezone

from runtime.reply.conversation_context import conversation_context


def row(identifier, status, stamp, **values):
    return dict(letter_id=identifier, channel='qq', binding_id='same',
                delivery_status=status, created_at=stamp,
                life_received_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                content=identifier, reply_text='draft-' + identifier, **values)


def test_frozen_turn_keeps_unanswered_user_inputs_without_drafts_or_other_binding():
    from runtime.personal_chat.context import freeze_read_window, READ_WINDOW
    rows = [row('delivered', 'DELIVERED', 1), row('waiting', 'GENERATED', 2),
            row('failed', 'FAILED', 3), row('current', 'GENERATING', 4)]
    rows.append({**row('foreign', 'GENERATED', 3), 'binding_id': 'other'})
    token = READ_WINDOW.set(freeze_read_window(rows, channel='qq', binding_id='same', current_id='current'))
    try:
        rows[0]['reply_text'] = 'mutated-after-freeze'
        rows.append(row('future', 'DELIVERED', 5))
        recent, old = conversation_context([], query='current', now=datetime.now(timezone.utc))
    finally:
        READ_WINDOW.reset(token)
    items = json.loads(recent)['letters']
    assert [i['user_letter'] for i in items] == ['delivered', 'waiting', 'failed']
    assert [i['linli_reply'] for i in items] == ['draft-delivered', '', '']
    assert not old
    assert 'mutated-after-freeze' not in recent and 'foreign' not in recent and 'future' not in recent


def test_late_unanswered_message_precedes_newer_delivered_turn_and_projects_as_user_only():
    from runtime.personal_chat.context import freeze_read_window, READ_WINDOW
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    rows = [row('newer', 'DELIVERED', 20, user_sent_at='1970-01-01T00:00:20+00:00'),
            row('late', 'SKIPPED', 30, user_sent_at='1970-01-01T00:00:10+00:00'),
            row('current', 'GENERATING', 40)]
    token = READ_WINDOW.set(freeze_read_window(rows, channel='qq', binding_id='same', current_id='current'))
    try:
        recent, _ = conversation_context([], query='current', now=datetime.now(timezone.utc))
    finally:
        READ_WINDOW.reset(token)
    assert [i['user_letter'] for i in json.loads(recent)['letters']] == ['late', 'newer']
    messages = ({'role': 'system', 'content': '<untrusted_history>' + json.dumps({'text': recent}) + '</untrusted_history>'},
                {'role': 'user', 'content': 'current'})
    actual = prepare_dialogue_messages(messages, max_input_chars=20000)
    assert [(m['role'], m['content'].split('\n')[-1]) for m in actual if m['role'] != 'system'] == [
        ('user', 'late'), ('user', 'newer'), ('assistant', 'draft-newer'), ('user', 'current')]
    assert 'draft-late' not in str(actual)


def test_default_read_remains_delivered_only_without_chat_boundary():
    recent, _ = conversation_context([row('waiting', 'GENERATED', 1)], query='next', now=datetime.now(timezone.utc))
    assert json.loads(recent)['letters'] == []


def test_received_boundary_excludes_superseded_and_future_and_keeps_equal_timestamp_order():
    from runtime.personal_chat.context import freeze_read_window
    rows = [row('second', 'RECEIVED', 2, user_sent_at='1970-01-01T00:00:01+00:00'),
            row('first', 'RECEIVED', 1, user_sent_at='1970-01-01T00:00:01+00:00'),
            row('merged', 'RECEIVED', 2, superseded_by='current'),
            row('current', 'GENERATING', 3), row('later', 'RECEIVED', 4),
            row('future-delivery', 'DELIVERED', 5)]
    actual = freeze_read_window(rows, channel='qq', binding_id='same', current_id='current')
    assert [r['letter_id'] for r in actual] == ['first', 'second']


def test_same_receipt_timestamp_uses_sequence_and_legacy_position_not_id():
    from runtime.personal_chat.context import freeze_read_window
    for sequenced in (False, True):
        rows = [row('z-first', 'DELIVERED', 1), row('a-second', 'RECEIVED', 1),
                row('current', 'GENERATING', 1), row('late', 'RECEIVED', 1)]
        if sequenced:
            for i, value in enumerate(rows):
                value['received_sequence'] = i + 1
            rows = [rows[3], rows[1], rows[0], rows[2]]
        actual = freeze_read_window(rows, channel='qq', binding_id='same', current_id='current')
        assert [r['letter_id'] for r in actual] == ['z-first', 'a-second']


def test_unconfirmed_delivery_final_messages_keep_user_sources_not_assistant_draft():
    from runtime.personal_chat.context import freeze_read_window, READ_WINDOW
    from runtime.reply.fact_attribution import prepare_dialogue_messages
    rows = [row('uncertain', 'DELIVERY_UNCONFIRMED', 1, source_messages={'platform-7': 'hello'}),
            row('current', 'GENERATING', 2)]
    token = READ_WINDOW.set(freeze_read_window(rows, channel='qq', binding_id='same', current_id='current'))
    try:
        recent, _ = conversation_context([], query='current', now=datetime.now(timezone.utc))
    finally:
        READ_WINDOW.reset(token)
    actual = prepare_dialogue_messages(({'role': 'system', 'content': '<untrusted_history>' + json.dumps({'text': recent}) + '</untrusted_history>'},
                                       {'role': 'user', 'content': 'current'}), max_input_chars=20000)
    history = next(m for m in actual if m['role'] == 'user' and m['content'] != 'current')
    metadata = json.loads(history['content'].split('\n', 1)[0][len('[历史消息 '):-1])
    assert metadata['source_message_ids'] == ['platform-7']
    assert metadata['delivery_state'] == 'user_received_reply_unconfirmed'
    assert 'draft-uncertain' not in str(actual)
    assert actual[-1]['content'] == 'current'


def test_merged_current_uses_last_receipt_cutoff_without_moving_original_identity():
    from runtime.personal_chat.context import freeze_read_window
    rows = [row('current', 'GENERATING', 1, received_sequence=1,
                read_boundary_created_at=3, read_boundary_sequence=3,
                user_sent_at='1970-01-01T00:00:01+00:00'),
            {**row('wechat-between', 'DELIVERED', 2, received_sequence=2), 'channel': 'wechat'},
            row('merged-tail', 'SKIPPED', 3, received_sequence=3, superseded_by='current'),
            row('after-boundary', 'RECEIVED', 3, received_sequence=4)]
    actual = freeze_read_window(rows, channel='qq', binding_id='same', current_id='current')
    assert [r['letter_id'] for r in actual] == ['wechat-between']
    assert rows[0]['created_at'] == 1 and rows[0]['received_sequence'] == 1


def test_read_snapshot_ignores_generation_payloads_and_freezes_nested_evidence():
    from runtime.personal_chat.context import freeze_read_window
    class UnreadGenerationTrace:
        def __deepcopy__(self, memo):
            raise AssertionError('generation payload is not dialogue evidence')
    delivered = row('old', 'DELIVERED', 1, generation_trace=UnreadGenerationTrace(),
                    incoming_media_observations=[{'summary': '冻结的观察'}],
                    companion_decision={'source_id_map': {'a': 'reply:old:1:user'}})
    actual = freeze_read_window([delivered, row('current', 'GENERATING', 2)],
                               channel='qq', binding_id='same', current_id='current')
    delivered['incoming_media_observations'][0]['summary'] = '后来修改'
    delivered['companion_decision']['source_id_map']['a'] = 'later'
    assert actual[0]['incoming_media_observations'][0]['summary'] == '冻结的观察'
    assert actual[0]['companion_decision']['source_id_map']['a'] == 'reply:old:1:user'
    assert 'generation_trace' not in actual[0]


def test_late_pending_merge_preserves_delivery_order_even_with_platform_clock_reversal():
    from runtime.personal_chat.context import freeze_read_window
    rows = [row('first', 'DELIVERED', 1, user_sent_at='1970-01-01T00:00:30Z'),
            row('second', 'DELIVERED', 2, user_sent_at='1970-01-01T00:00:10Z'),
            {**row('other-channel', 'DELIVERED', 3), 'channel': 'wechat'},
            row('late-25', 'FAILED', 4, user_sent_at='1970-01-01T00:00:25Z'),
            row('late-20', 'FAILED', 5, user_sent_at='1970-01-01T00:00:20Z'),
            row('last', 'DELIVERED', 6, user_sent_at='1970-01-01T00:00:40Z'),
            row('current', 'GENERATING', 7)]
    actual = freeze_read_window(rows, channel='qq', binding_id='same', current_id='current')
    assert [r['letter_id'] for r in actual] == ['late-20', 'late-25', 'first', 'second', 'other-channel', 'last']
