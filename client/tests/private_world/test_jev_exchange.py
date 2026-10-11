from runtime.private_world.jev_exchange import EXCHANGE_MAX_INPUT_BYTES as JEV_MAX_INPUT_BYTES
import asyncio

import pytest


class Port:
    def __init__(self, choose):
        self.choose = choose
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        assert not self.calls or purpose == 'exchange-anchored-facts', 'only selected evidence permits a dependent request'
        self.calls.append((state, questions, purpose))
        answers = {}
        for key, question in questions.items():
            if key.endswith('_existing_match'):
                old = self.choose(key.removesuffix('_existing_match') + '_identity', question, state)
                answers[key] = old if old.startswith('p') else 'none'
            elif key.endswith('_applicability_kind'):
                prefix = key.removesuffix('_applicability_kind')
                status = self.choose(prefix + '_status', question, state)
                identity = self.choose(prefix + '_identity', questions[prefix + '_existing_match'], state)
                no_projects = not any(self.calls[0][0]['exchange']['previous_state'].values())
                answers[key] = status.split('_', 1)[0] if status.startswith(('linli_', 'shared_')) and (
                    identity != 'none' or no_projects) else 'none'
            else:
                answers[key] = first(key, question, state) if key.endswith('_evidence') else self.choose(key, question, state)
        # Preserve existing semantic fixtures as the wire now binds evidence,
        # carries new kind in identity, and asks plain status for that action.
        for key, value in list(answers.items()):
            if not key.startswith('update_'):
                continue
            if key.endswith('_status') and value.startswith(('linli_', 'shared_')):
                answers[key] = value.split('_', 1)[1]
            if key.endswith('_evidence'):
                quote_key = key.removesuffix('_evidence') + '_quote'
                selected = self.choose(quote_key, self.calls[0][1][quote_key], state)
                answers[key] = selected if selected in questions[key]['criteria'] else next(
                    ref for ref in questions[key]['criteria'] if ref != 'none')
        return answers


def first(key, question, state):
    return 'none' if 'none' in question['criteria'] else next(iter(question['criteria']))


def test_shared_promise_has_exact_quote_and_stable_new_id():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        if key.startswith('update_0_'):
            return {'update_0_quote': 'r0', 'update_0_status': 'shared_planned', 'update_0_identity': 'new'}[key]
        return first(key, q, state)
    data = {'user_letter': '练好发我听听吧。', 'linli_reply': '明天练好后发给你听。',
            'previous_state': {'projects': [], 'shared': []}, 'active_boundaries': [], 'origin': 'user'}
    result = asyncio.run(extract(Port(choose), data, '', 'life:a'))
    again = asyncio.run(extract(Port(choose), data, '', 'life:a'))
    assert result['updates'] == again['updates']
    item = result['updates'][0]
    assert item['quote'] == item['detail'] == data['linli_reply']
    assert item['kind'] == 'shared' and item['status'] == 'planned' and item['actor'] == 'linli'


def test_update_retains_existing_id_kind_and_user_cancellation():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        if key.startswith('update_0_'):
            return {'update_0_quote': 'u0', 'update_0_status': 'shared_cancelled', 'update_0_identity': 'p0'}[key]
        return first(key, q, state)
    data = {'user_letter': '不用寄书给我了，取消吧。', 'linli_reply': '那我明天寄给你。', 'origin': 'user',
            'previous_state': {'projects': [], 'shared': [{'id': 'send-book', 'kind': 'shared', 'title': '寄书'}]},
            'active_boundaries': []}
    result = asyncio.run(extract(Port(choose), data, '', 'life:b'))
    assert result['updates'][0]['id'] == 'send-book'
    assert result['updates'][0]['status'] == 'cancelled'
    assert result['updates'][0]['quote'] == data['user_letter']


def test_proactive_input_cannot_claim_user_agreement():
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': '', 'linli_reply': '明天一起看书吗？', 'origin': 'proactive',
            'active_boundaries': [], 'previous_state': {}}
    port = Port(first)
    asyncio.run(extract(port, data, '', 'life:p'))
    assert not any(key.endswith('_status') and key.startswith('update_') for key in port.calls[0][1])
    assert port.calls[0][0]['exchange']['origin'] == 'proactive'


def test_routine_is_selected_and_not_silently_empty():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23',
                'sleep_minute': '30', 'utc_offset': '480'}.get(key, first(key, q, state))
    data = {'user_letter': '我住上海，通常每天23点30分睡觉。', 'linli_reply': '知道了。',
            'previous_state': {}, 'active_boundaries': [], 'origin': 'user'}
    result = asyncio.run(extract(Port(choose), data, '', 'life:r'))
    assert result['routine'] == {'sleep_minute': 1410, 'utc_offset_minutes': 480, 'quote': data['user_letter']}


@pytest.mark.parametrize('minute', range(60))
def test_single_sleep_minute_choice_preserves_every_exact_minute(minute):
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': f'我通常在东京晚上23点{minute:02d}分睡觉。', 'linli_reply': '知道了。'}
    def choose(key, q, state):
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23',
            'sleep_minute': str(minute),
            'utc_offset': '540'}.get(key, first(key, q, state))
    port = Port(choose)
    result = asyncio.run(extract(port, data, '', 'life:minute'))
    assert len(port.calls) == 2
    assert len(port.calls[1][1]['sleep_minute']['criteria']) == 61
    assert result['routine']['sleep_minute'] == 23 * 60 + minute


def test_unknown_sleep_minute_cannot_become_zero():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23',
            'sleep_minute': 'unknown', 'utc_offset': '540'}.get(key, first(key, q, state))
    with pytest.raises(ValueError, match='JEV_EXCHANGE_ROUTINE_EVIDENCE'):
        asyncio.run(extract(Port(choose), {'user_letter': '我通常在东京晚上十一点多睡。', 'linli_reply': '好。'}, '', 'life:unknown'))


def test_long_unquotable_sentence_fails_instead_of_dropping_condition():
    from runtime.private_world.jev_exchange import extract
    with pytest.raises(ValueError, match='JEV_EXCHANGE_QUOTE_CAPACITY'):
        asyncio.run(extract(Port(first), {'user_letter': '如果' + '条件' * 130, 'linli_reply': '好。'}, '', 'long'))


def test_conduct_never_gets_generated_reply():
    from runtime.private_world.jev_exchange import conduct
    port = Port(first)
    result = asyncio.run(conduct(port, {'user_letter': '我老板逼我熬夜。', 'active_boundaries': []}, '', 'life:c', conflict=True))
    assert result == {'conduct': 'none', 'target': 'unclear', 'quote': ''}
    assert all('linli_reply' not in state for state, _, _ in port.calls)


def test_quote_catalog_uses_shared_original_then_bound_identity():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        return {'update_0_quote': 'u0', 'update_0_status': 'shared_cancelled', 'update_0_identity': 'p0'}.get(key, first(key, q, state))
    data = {'user_letter': '如果还没寄就别寄书了，等我下周确认。', 'linli_reply': '好，我等你确认。', 'origin': 'user',
            'character_development': {'private_unused': 'NOT_FOR_EXCHANGE'},
            'previous_state': {'projects': [], 'shared': [{'id': 'book', 'kind': 'shared', 'title': '寄书'}]},
            'active_boundaries': [], 'previous_observation': {'note': 'OLD_OBSERVATION'}}
    port = Port(choose)
    result = asyncio.run(extract(port, data, 'APPROVED_CONTRACT', 'life:compact'))
    assert result['updates'][0]['quote'] == data['user_letter']
    assert result['updates'][0]['id'] == 'book'
    assert len(port.calls) == 2
    # Every slot names entries of one shared directory; the durable id stays on the host.
    assert port.calls[1][1]['update_0_existing_match']['criteria']['p0'] == 'p0'
    assert port.calls[1][0]['exchange']['previous_state'] == {'p0': {'kind': 'shared', 'title': '寄书', 'status': 'unknown'}}
    assert set(port.calls[1][1]['update_0_applicability_kind']['criteria']) == {'none', 'shared'}
    assert all(data['user_letter'] in question['instructions'] for key, question in port.calls[1][1].items()
               if key.startswith('update_0_'))
    for state, questions, purpose in port.calls:
        assert 'NOT_FOR_EXCHANGE' not in str(state)
        for key, span in state['quotes'].items():
            source = 'user_letter' if key.startswith('u') else 'linli_reply'
            assert state['sources'][source][span[0]:span[1]]
            assert len(span) == 2
        for question in questions.values():
            for value in question['criteria'].values():
                if isinstance(value, dict) and 'quote_id' in value:
                    assert value['quote_id'] in state['quotes']


def test_exchange_directory_drops_old_bodies_but_keeps_identity_state_and_current_proof():
    import copy
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': '书先别寄。', 'linli_reply': '好，取消寄书。', 'origin': 'user',
        'previous_state': {'projects': [], 'shared': [dict(id='stable-book', title='寄书', kind='shared',
            actor='linli', status='planned', updated_at='2026-09-27T12:00:00+00:00',
            detail='OLD_BODY_MUST_STAY_HOST' * 2000, history=[{'quote': 'OLD_HISTORY_MUST_STAY_HOST'}])]},
        'rhythm': {'phase': 'day', 'local_time': '2026-09-28T14:00:00+08:00',
            'wellbeing': {'state': 'well', 'care': 'normal', 'evidence': 'OLD_BODY_MUST_STAY_HOST'},
            'history': 'OLD_HISTORY_MUST_STAY_HOST'},
        'previous_observation': {'activity': '看书', 'note': '刚看完这一章。', 'occurred_at': '2026-09-28T05:00:00+00:00',
            'history': 'OLD_HISTORY_MUST_STAY_HOST'},
        'active_boundaries': [{'boundary_id': 'b1', 'quote': '晚上别打电话', 'set_at': '2026-09-27T12:00:00+00:00',
            'evaluation_details': 'OLD_BODY_MUST_STAY_HOST'}]}
    before = copy.deepcopy(data)
    def choose(key, q, state):
        assert 'MUST_STAY_HOST' not in str(state)
        if 'previous_state' in state['exchange']:
            # The durable id and timestamps are host-side: the service matches by title/kind/status.
            names = ('title', 'kind', 'actor', 'status')
            expected = {key: data['previous_state']['shared'][0][key] for key in names}
            if 'action_anchors' in state:
                assert state['exchange']['previous_state']['shared'][0] == expected
            else:
                assert state['exchange']['previous_state'] == {'p0': expected}
        if key in ('update_0_identity', 'update_0_existing_match'):
            assert q['criteria']['p0'] == 'p0'
        assert state['exchange']['previous_observation']['note'] == '刚看完这一章。'
        assert state['sources']['user_letter'] == '书先别寄。'
        return {'update_0_quote': 'u0', 'update_0_status': 'shared_cancelled', 'update_0_identity': 'p0'}.get(key, first(key, q, state))
    port = Port(choose)
    result = asyncio.run(extract(port, data, 'APPROVED_CONTRACT', 'life:minimal'))
    assert len(port.calls) == 2 and data == before
    assert result['updates'][0]['id'] == 'stable-book'
    assert result['updates'][0]['title'] == '寄书'
    assert result['updates'][0]['quote'] == data['user_letter']


def test_conduct_shares_boundary_catalog_once_without_character_source():
    from runtime.private_world.jev_exchange import conduct
    port = Port(lambda key, q, state: {'conduct': 'boundary_1', 'quote': 'u0'}.get(key, first(key, q, state)))
    data = {'user_letter': '不管你说几点不打电话，现在就给我打。', 'linli_reply': 'MUST_NEVER_ENTER',
            'active_boundaries': [{'boundary_id': 'b0', 'quote': '其他边界'}, {'boundary_id': 'b1', 'quote': '现在不打电话'}]}
    result = asyncio.run(conduct(port, data, 'contract', 'life:c', conflict=True))
    assert result['boundary_id'] == 'b1'
    assert len(port.calls) == 1
    for state, _, purpose in port.calls:
        assert set(state['sources']) == {'user_letter'}
        assert 'MUST_NEVER_ENTER' not in str(state)
        assert state['active_boundaries'] == data['active_boundaries']


def test_relationship_boundary_and_contact_keep_separate_originals():
    from runtime.private_world.jev_exchange import extract
    from runtime.memory.private_world_relationship import validate_boundary_changes, validate_exchange_relationship
    def choose(key, q, state):
        return {'relationship': 'meaningful_exchange', 'relationship_user_quote': 'u0',
                'relationship_reply_quote': 'r0', 'boundary_0_quote': 'r0', 'boundary_0_action': 'withdraw_0',
                'contact_choice': 'qq', 'contact_quote': 'u0'}.get(key, first(key, q, state))
    data = {'user_letter': '我选QQ，我们用QQ交流练琴感受吧。', 'linli_reply': '之前那条不准聊练琴的约定撤销，我们可以聊。',
            'contact_invited': True, 'origin': 'user', 'previous_state': {},
            'active_boundaries': [{'boundary_id': 'b1', 'quote': '以后别和我聊练琴'}]}
    result = asyncio.run(extract(Port(choose), data, '', 'life:e'))
    assert result['boundaries'] == [{'action': 'withdraw', 'boundary_id': 'b1', 'quote': data['linli_reply']}]
    assert result['contact_choice'] == {'choice': 'qq', 'quote': data['user_letter']}
    validate_boundary_changes(result['boundaries'], data['linli_reply'])
    validate_exchange_relationship(result['relationship'], data['user_letter'], data['linli_reply'])


def test_update_is_accepted_by_original_validator():
    from runtime.private_world.jev_exchange import extract
    from runtime.private_world.daily_life import validate_exchange_updates
    def choose(key, q, state):
        return {'update_0_quote': 'r0', 'update_0_status': 'linli_ongoing', 'update_0_identity': 'new'}.get(key, first(key, q, state))
    data = {'user_letter': '练得怎样？', 'linli_reply': '我正在练第二段。', 'origin': 'user', 'previous_state': {}, 'active_boundaries': []}
    result = asyncio.run(extract(Port(choose), data, '', 'life:v'))
    rows = validate_exchange_updates('life:v', data['user_letter'], data['linli_reply'], result['updates'],
                                     stamp='2026-09-28T00:00:00+00:00', origin='user')
    assert rows[0]['actor'] == 'linli'


def test_overlapping_fragments_use_jev_canonical_evidence_instead_of_duplicate_commit():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        if key.startswith('update_0_'):
            return {'update_0_quote': 'r0', 'update_0_status': 'shared_planned', 'update_0_identity': 'new'}[key]
        return first(key, q, state)
    data = {'user_letter': '好。', 'linli_reply': '明天练好后，发给你听。', 'previous_state': {}, 'active_boundaries': []}
    result = asyncio.run(extract(Port(choose), data, '', 'life:o'))
    assert len(result['updates']) == 1
    assert result['updates'][0]['quote'] == data['linli_reply']


def test_jev_canonical_evidence_keeps_multiple_independent_promises():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        if key.startswith(('update_0_', 'update_1_')):
            index, field = key.split('_')[1:]
            return {'quote': 'r' + str(int(index) + 1), 'status': 'shared_planned', 'identity': 'new'}[field]
        return first(key, q, state)
    data = {'user_letter': '好。', 'linli_reply': '明天寄书，后天发录音。', 'previous_state': {}, 'active_boundaries': []}
    result = asyncio.run(extract(Port(choose), data, '', 'life:separate'))
    assert {u['quote'] for u in result['updates']} == {'明天寄书，', '后天发录音。'}
    assert {u['detail'] for u in result['updates']} == {data['linli_reply']}
    assert {u['title'] for u in result['updates']} == {'明天寄书，', '后天发录音。'}
    assert len({u['id'] for u in result['updates']}) == 2


def test_provider_failure_is_not_replaced_by_empty_updates():
    from runtime.private_world.jev_exchange import extract
    class Broken:
        async def ask(self, *args, **kwargs):
            raise RuntimeError('JEV_PROVIDER_UNAVAILABLE')
    with pytest.raises(RuntimeError, match='JEV_PROVIDER_UNAVAILABLE'):
        asyncio.run(extract(Broken(), {'user_letter': '你好', 'linli_reply': '明天给你寄书'}, '', 'life:failed'))


def test_conduct_violation_requires_existing_boundary_and_exact_user_quote():
    from runtime.private_world.jev_exchange import conduct
    def choose(key, q, state):
        return {'conduct': 'boundary_0', 'quote': 'u0'}.get(key, first(key, q, state))
    data = {'user_letter': '不管你说什么，今晚必须和我通话。',
            'active_boundaries': [{'boundary_id': 'b7', 'quote': '晚上不通话'}],
            'linli_reply': '这段回复不得用于证明用户行为'}
    port = Port(choose)
    result = asyncio.run(conduct(port, data, '', 'life:c', conflict=True))
    assert result == {'conduct': 'boundary_violation', 'target': 'linli', 'boundary_id': 'b7', 'quote': data['user_letter']}
    assert all('linli_reply' not in state for state, _, _ in port.calls)


def test_existing_project_kind_is_derived_from_identity():
    from runtime.private_world.jev_exchange import extract
    port = Port(lambda key, q, state: {'update_0_quote': 'r0', 'update_0_status': 'linli_completed', 'update_0_identity': 'p0'}.get(key, first(key, q, state)))
    data = {'user_letter': '好。', 'linli_reply': '我练完了。', 'previous_state': {
        'shared': [{'id': 'ours', 'kind': 'shared', 'title': '一起练'}]}}
    result = asyncio.run(extract(port, data, '', 'life:kind'))
    assert result['updates'][0]['kind'] == 'shared'
    assert len(port.calls) == 2


def test_missing_conduct_quote_cannot_commit_an_allegation():
    from runtime.private_world.jev_exchange import conduct
    port = Port(lambda key, q, state: 'pressure' if key == 'conduct' else 'none')
    with pytest.raises(ValueError, match='JEV_EXCHANGE_CONDUCT_EVIDENCE'):
        asyncio.run(conduct(port, {'user_letter': '现在必须打给我。'}, '', 'life:missing', conflict=True))
    assert len(port.calls) == 1


def test_oversized_exchange_fails_before_provider_instead_of_batching():
    from runtime.private_world.jev_exchange import extract
    port = Port(first)
    with pytest.raises(ValueError, match='JEV_INPUT_TOO_LARGE'):
        asyncio.run(extract(port, {'user_letter': '你好。', 'linli_reply': '你好。'}, 'x' * JEV_MAX_INPUT_BYTES, 'life:large'))
    assert port.calls == []


def test_empty_quote_gates_unused_status_without_minting_fact():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        if key == 'update_0_status':
            return 'shared_planned'
        return first(key, q, state)
    port = Port(choose)
    result = asyncio.run(extract(port, {'user_letter': '好。', 'linli_reply': '明天练好后，发给你听。'}, '', 'life:missing'))
    assert result['updates'] == []
    assert len(port.calls) == 1


def test_question_quote_with_no_status_or_identity_is_not_a_world_fact():
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': '你在做什么？', 'linli_reply': '先聊一会儿。',
            'previous_state': {'projects': [{'id': 'practice', 'title': '练琴', 'kind': 'linli'}]}}
    port = Port(lambda key, q, state: 'u0' if key == 'update_0_quote' else first(key, q, state))
    result = asyncio.run(extract(port, data, '', 'life:question'))
    assert result['updates'] == [] and len(port.calls) == 2


def test_none_status_after_matching_identity_means_no_change():
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': '现在呢？', 'linli_reply': '我正在练琴。',
            'previous_state': {'projects': [{'id': 'practice', 'title': '练琴', 'kind': 'linli'}]}}
    port = Port(lambda key, q, state: {'update_0_quote': 'r0', 'update_0_status': 'none',
        'update_0_identity': 'p0'}.get(key, first(key, q, state)))
    assert asyncio.run(extract(port, data, '', 'life:conflict'))['updates'] == []


def test_incomplete_relationship_does_not_discard_evidenced_life_update():
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        return {'update_0_quote': 'r0', 'update_0_status': 'linli_completed',
                'relationship': 'meaningful_exchange', 'relationship_stage': 'committed',
                'relationship_user_quote': 'u0', 'relationship_reply_quote': 'none'}.get(key, first(key, q, state))
    result = asyncio.run(extract(Port(choose), {'user_letter': '吃完了？', 'linli_reply': '我吃完午饭了。'}, '', 'life:partial'))
    assert result['relationship'] is None
    assert result['updates'][0]['status'] == 'completed'
    assert result['updates'][0]['quote'] == '我吃完午饭了。'


def test_role_activity_cannot_be_sourced_from_user_guess():
    from runtime.private_world.jev_exchange import extract
    port = Port(lambda key, q, state: {'update_0_quote': 'u0', 'update_0_status': 'linli_ongoing'}.get(key, first(key, q, state)))
    with pytest.raises(ValueError, match='JEV_RESPONSE_INVALID'):
        asyncio.run(extract(port, {'user_letter': '你正在练琴吧？', 'linli_reply': '你猜。'}, '', 'life:actor'))


def test_awaiting_user_is_preserved_without_inventing_agreement():
    from runtime.private_world.jev_exchange import extract
    port = Port(lambda key, q, state: {'update_0_quote': 'r0', 'update_0_status': 'shared_awaiting_user'}.get(key, first(key, q, state)))
    result = asyncio.run(extract(port, {'user_letter': '你好。', 'linli_reply': '明天一起看书吗？'}, '', 'life:pending'))
    assert result['updates'][0]['status'] == 'awaiting_user'
    assert result['updates'][0]['actor'] == 'linli'


def test_realistic_letter_with_existing_projects_fits_one_packet():
    import json
    from runtime.private_world.jev_exchange import extract
    from runtime.private_world.daily_life_runtime import _EXCHANGE_LIFE_PROMPT
    port = Port(first)
    data = {'user_letter': '吃完了吗，想听你评价这顿饭',
        'linli_reply': '吃完了，碗也已经洗了。评价的话，番茄炒蛋看着简单，其实挺挑火候。番茄得先炒出汁，蛋得嫩，火大一点就发干。今天这盘算过得去，蛋没老，番茄的酸味也压住了，糖放得不多，正好。配白米饭是很稳的组合，热饭沾上一点汤汁，比什么复杂菜都让人吃得安心。',
        'previous_state': {'projects': [{'id': f'project-{i}', 'kind': 'linli', 'title': f'练习{i}', 'status': 'ongoing'} for i in range(8)]}}
    asyncio.run(extract(port, data, _EXCHANGE_LIFE_PROMPT, 'life:budget'))
    state, questions, purpose = port.calls[0]
    size = len(json.dumps(dict(state=state, questions=questions, purpose=purpose), ensure_ascii=False, separators=(',', ':')).encode())
    assert size < JEV_MAX_INPUT_BYTES
    assert len(questions) == 17  # anchors and independent facts; routine values depend on its proof
    assert state['sources'] == {key: data[key] for key in ('user_letter', 'linli_reply')}
    assert _EXCHANGE_LIFE_PROMPT not in str(state)
    assert all(value == key for key, value in questions['update_0_quote']['criteria'].items() if key != 'none')


def test_exchange_keeps_how_each_side_addresses_the_other_as_original_quotes():
    """Addressing comes from what was actually written, never a model-made name."""
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': '吃完了，小离，你也早点午睡。落款，你的老姜。', 'linli_reply': '好呀老姜，我这就去睡。',
            'previous_state': {'projects': [], 'shared': []}, 'active_boundaries': [], 'origin': 'user'}
    def choose(key, q, state):
        picks = {'address_linli_quote': '小离，', 'address_self_quote': '你的老姜。', 'address_user_quote': '好呀老姜，'}
        if key in picks:
            quotes = {k: v for k, v in state['quotes'].items()}
            text = {**{k: data['user_letter'][s:e] for k, (s, e) in quotes.items() if k.startswith('u')},
                    **{k: data['linli_reply'][s:e] for k, (s, e) in quotes.items() if k.startswith('r')}}
            return next(k for k, v in text.items() if v == picks[key])
        return first(key, q, state)
    result = asyncio.run(extract(Port(choose), data, '', 'life:addr'))
    assert result['addressing'] == {'user_calls_linli': '小离，', 'user_self': '你的老姜。', 'linli_calls_user': '好呀老姜，'}

    proactive = {**data, 'origin': 'proactive'}
    port = Port(first)
    assert asyncio.run(extract(port, proactive, '', 'life:p'))['addressing'] == {}
    assert not any(key.startswith('address_') for key in port.calls[0][1])


def test_long_letter_uses_coarser_quotes_instead_of_dropping_the_exchange():
    from runtime.private_world.jev_exchange import _quotes, extract
    letter = ''.join(f'第{i}天我去了图书馆，借了一本书，晚上读到很晚。' for i in range(60))
    with pytest.raises(ValueError, match='JEV_EXCHANGE_QUOTE_CAPACITY'):
        _quotes(letter, 'u')  # the finest windows no longer fit
    assert len(_quotes(letter, 'u', 2)) == 60
    port = Port(first)
    asyncio.run(extract(port, {'user_letter': letter, 'linli_reply': '好。'}, '', 'long-letter'))
    assert len(port.calls) == 1  # degrading happens before the single paid request
    state = port.calls[0][0]
    assert all(start >= 0 for start, _ in state['quotes'].values())


def test_run_on_sentence_splits_only_at_clause_marks():
    from runtime.private_world.jev_exchange import _quotes
    sentence = '，'.join(['今天去了河边散步看到很多人在钓鱼'] * 20) + '。'
    quotes = _quotes(sentence, 'u', 2)
    assert all(len(quote) <= 240 and sentence.find(quote) >= 0 for quote in quotes.values())


def test_more_changes_than_common_slots_expand_anchors_then_bind_fields():
    from runtime.private_world.jev_exchange import extract
    reply = '我练完琴了。信也寄了。菜买好了。碗洗了。'
    class Overflow(Port):
        async def ask(self, state, questions, *, purpose):
            self.calls.append(questions)
            if len(self.calls) == 1:
                return {key: 'unsupported' if key == 'capacity' else first(key, q, state) for key, q in questions.items()}
            picks = {**{f'update_{i}_quote': f'r{i}' for i in range(4)},
                     **{f'update_{i}_evidence': f'r{i}' for i in range(4)},
                     **{f'update_{i}_status': 'completed' for i in range(4)},
                     **{f'update_{i}_existing_match': 'none' for i in range(4)},
                     **{f'update_{i}_applicability_kind': 'linli' for i in range(4)}}
            return {key: picks.get(key, 'ok' if key == 'capacity' else first(key, q, state)) for key, q in questions.items()}
    port = Overflow(first)
    result = asyncio.run(extract(port, {'user_letter': '好。', 'linli_reply': reply}, '', 'life:many'))
    assert [sum(key.endswith('_quote') and key.startswith('update_') for key in q) for q in port.calls] == [3, 12, 0]
    assert [item['quote'] for item in result['updates']] == ['我练完琴了。', '信也寄了。', '菜买好了。', '碗洗了。']


def test_capacity_still_unsupported_with_every_slot_fails_closed():
    from runtime.private_world.jev_exchange import extract
    class Always(Port):
        async def ask(self, state, questions, *, purpose):
            self.calls.append(questions)
            return {key: 'unsupported' if key == 'capacity' else first(key, q, state) for key, q in questions.items()}
    port = Always(first)
    with pytest.raises(ValueError, match='JEV_EXCHANGE_UNREPRESENTABLE_UPDATE'):
        asyncio.run(extract(port, {'user_letter': '好。', 'linli_reply': '我练完琴了。'}, '', 'life:full'))
    assert len(port.calls) == 2


@pytest.mark.parametrize('sign,hours,minutes,offset', [('east', '5', '30', 330), ('west', '3', '30', -210), ('east', '0', '0', 0)])
def test_single_utc_offset_choice_preserves_exact_minutes(sign, hours, minutes, offset):
    from runtime.private_world.jev_exchange import extract
    def choose(key, q, state):
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23', 'sleep_minute': '0',
                'utc_offset': str(offset)}.get(key, first(key, q, state))
    result = asyncio.run(extract(Port(choose), {'user_letter': '我通常23点睡。', 'linli_reply': '好。'}, '', 'life:tz'))
    assert result['routine']['utc_offset_minutes'] == offset


@pytest.mark.parametrize('literal,selected,expected', [('UTC+03:30', '-210', 210), ('UTC-08:00', '480', -480),
                                                      ('UTC+05:45', '-345', 345), ('UTC+00:00', '0', 0)])
def test_explicit_utc_sign_normalizes_a_known_matching_offset(literal, selected, expected):
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': f'我通常按{literal}当地时间23点17分睡。', 'linli_reply': '好。'}
    def choose(key, question, state):
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23', 'sleep_minute': '17',
                'utc_offset': selected, 'utc_offset_literal': 't0'}.get(key, first(key, question, state))
    result = asyncio.run(extract(Port(choose), data, '', 'life:utc-sign'))
    assert result['routine']['utc_offset_minutes'] == expected


def test_explicit_utc_literal_does_not_turn_unknown_semantic_offset_into_a_fact():
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': '我通常按UTC+03:30当地时间23点17分睡。', 'linli_reply': '好。'}
    def choose(key, question, state):
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23', 'sleep_minute': '17',
                'utc_offset': 'unknown', 'utc_offset_literal': 't0'}.get(key, first(key, question, state))
    with pytest.raises(ValueError, match='JEV_EXCHANGE_ROUTINE_EVIDENCE'):
        asyncio.run(extract(Port(choose), data, '', 'life:utc-unknown'))


@pytest.mark.parametrize('literal,selected', [
    ('UTC+03:30', '300'),
    ('UTC+03:30或UTC-03:30', '-210'),
    ('UTC+25:00', '300'),
    ('UTC+03:77', '-180'),
    ('UTC+03.5', '-180'),
])
def test_utc_math_does_not_override_distinct_ambiguous_or_invalid_evidence(literal, selected):
    from runtime.private_world.jev_exchange import extract
    data = {'user_letter': f'我通常按{literal}当地时间23点17分睡。', 'linli_reply': '好。'}
    def choose(key, question, state):
        if key == 'utc_offset_literal':
            return 't0' if literal == 'UTC+03:30' else 'none'
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23', 'sleep_minute': '17',
                'utc_offset': selected}.get(key, first(key, question, state))
    result = asyncio.run(extract(Port(choose), data, '', 'life:utc-nonliteral'))
    assert result['routine']['utc_offset_minutes'] == int(selected)


@pytest.mark.parametrize('letter', [
    '我通常23点17分睡，但我不采用UTC+03:30。',
    '家人采用UTC+03:30，我本人固定按自己的当地时间23点17分睡。',
])
def test_inapplicable_utc_literal_cannot_change_a_same_magnitude_offset(letter):
    from runtime.private_world.jev_exchange import extract
    def choose(key, question, state):
        if key == 'utc_offset_literal':
            assert question['criteria']['t0']['text'] == 'UTC+03:30'
            return 'none'
        return {'routine': 'set', 'routine_quote': 'u0', 'sleep_hour': '23', 'sleep_minute': '17',
                'utc_offset': '-210'}.get(key, first(key, question, state))
    port = Port(choose)
    result = asyncio.run(extract(port, {'user_letter': letter}, '', 'life:utc-inapplicable'))
    assert 'utc_offset_literal' in port.calls[1][1]
    assert result['routine']['utc_offset_minutes'] == -210
    assert len(port.calls) == 2
