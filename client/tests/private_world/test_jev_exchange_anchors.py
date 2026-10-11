"""Synthetic BEFORE/AFTER cases; no provider credentials or paid calls."""
import asyncio

import pytest

from runtime.private_world import jev_exchange
from runtime.private_world.jev_exchange import extract


def empty(question):
    return 'none' if 'none' in question['criteria'] else next(iter(question['criteria']))


class DriftPort:
    """Independent ordinal questions drift; explicit action questions stay bound."""
    def __init__(self, *, same_quote=False):
        self.calls = []
        self.same_quote = same_quote

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        result = {}
        for key, question in questions.items():
            value = empty(question)
            if key.startswith('update_0_') or self.same_quote and key.startswith('update_1_'):
                field = key.split('_', 2)[2]
                if field == 'quote':
                    if 'action_anchors' in state:
                        wanted = '我吃完午饭了，' if key.startswith('update_0_') else '也洗好了碗。'
                        value = next((ref for ref, item in state['action_anchors'].items()
                                      if item['text'] == wanted), 'r0')
                    else:
                        value = 'r0'
                elif field == 'identity':
                    value = 'new_linli' if self.same_quote and 'new_linli' in question['criteria'] else (
                        'new' if self.same_quote else 'p0' if '我吃完午饭了。' in question['instructions'] else 'p1')
                elif field == 'status':
                    value = 'completed' if 'completed' in question['criteria'] else (
                        'linli_completed' if self.same_quote else 'linli_paused')
                elif field == 'evidence':
                    value = 'r0'
                elif field == 'existing_match':
                    value = 'none' if self.same_quote else 'p0' if '我吃完午饭了。' in question['instructions'] else 'p1'
                elif field == 'applicability_kind':
                    value = 'linli'
            catalog = {value: ref for ref, value in state.get('status_catalog', {}).items()}
            result[key] = catalog.get(value, value)
        return result


def test_valid_same_kind_fields_cannot_attach_dishes_status_to_lunch_quote():
    data = {'user_letter': '好。', 'linli_reply': '我吃完午饭了。碗还没洗，先暂停洗碗。',
            'previous_state': {'projects': [
                {'id': 'lunch', 'title': '吃午饭', 'kind': 'linli', 'status': 'ongoing'},
                {'id': 'dishes', 'title': '洗碗', 'kind': 'linli', 'status': 'ongoing'}]}}
    port = DriftPort()
    result = asyncio.run(extract(port, data, '', 'life:bound'))
    assert [(item['id'], item['status'], item['quote']) for item in result['updates']] == [
        ('lunch', 'completed', '我吃完午饭了。')]
    assert len(port.calls) == 2
    for key, question in port.calls[1][1].items():
        if key.startswith('update_0_'):
            assert '我吃完午饭了。' in question['instructions']


def test_two_new_actions_can_share_complete_evidence_without_identity_collision():
    data = {'user_letter': '好。', 'linli_reply': '我吃完午饭了，也洗好了碗。'}
    first = asyncio.run(extract(DriftPort(same_quote=True), data, '', 'life:two'))
    again = asyncio.run(extract(DriftPort(same_quote=True), data, '', 'life:two:correct'))
    assert len(first['updates']) == 2
    assert len({item['id'] for item in first['updates']}) == 2
    assert {item['quote'] for item in first['updates']} == {data['linli_reply']}
    assert all(item['status'] == 'completed' for item in first['updates'])
    assert [item['id'] for item in first['updates']] == [item['id'] for item in again['updates']]


class BoundPort:
    def __init__(self, selections):
        self.selections = selections
        self.calls = []

    async def ask(self, state, questions, *, purpose):
        self.calls.append((state, questions, purpose))
        result = {}
        for key, question in questions.items():
            value = empty(question)
            if key.startswith('update_'):
                _, index, field = key.split('_', 2)
                if int(index) < len(self.selections) and self.selections[int(index)] is not None:
                    text, identity, status = self.selections[int(index)]
                    if field == 'quote':
                        value = next(ref for ref, anchor in state['action_anchors'].items() if anchor['text'] == text)
                    elif field == 'evidence':
                        anchors = self.calls[0][0]['action_anchors']
                        anchor = next(anchor for anchor in anchors.values() if anchor['text'] == text)
                        # The supporting sentence is referenced by the anchor that carries it
                        # (absent: the anchor is its own support) instead of being repeated.
                        support = anchors[anchor['support']]['text'] if 'support' in anchor else anchor['text']
                        value = next(ref for ref in question['criteria'] if ref != 'none' and (
                            state['sources']['user_letter' if ref.startswith('u') else 'linli_reply'][
                                slice(*state['quotes'][ref])] == support))
                    else:
                        if field == 'existing_match':
                            value = identity if identity.startswith('p') else 'none'
                        elif field == 'applicability_kind':
                            if identity.startswith('new_'):
                                value = identity.removeprefix('new_')
                            elif identity.startswith('p'):
                                previous = self.calls[0][0]['exchange']['previous_state']
                                old = (previous['projects'] + previous['shared'])[int(identity[1:])]
                                value = old.get('kind', 'linli')
                            else:
                                value = 'none'
                        else:
                            value = identity if field == 'identity' else status
            result[key] = value
        return result


def test_conjunctions_only_supply_anchors_and_keep_full_conditional_evidence():
    data = {'user_letter': '好。', 'linli_reply': '如果明天不下雨，我会寄书也会发录音。'}
    port = BoundPort([('我会寄书', 'new_shared', 'planned'), ('也会发录音。', 'new_shared', 'planned')])
    result = asyncio.run(extract(port, data, '', 'life:condition'))
    assert len(result['updates']) == 2
    assert {item['quote'] for item in result['updates']} == {data['linli_reply']}
    assert {item['status'] for item in result['updates']} == {'planned'}
    assert set(port.calls[1][1]) == {
        'update_0_existing_match', 'update_0_applicability_kind', 'update_0_status', 'update_0_evidence',
        'update_1_existing_match', 'update_1_applicability_kind', 'update_1_status', 'update_1_evidence'}
    assert all(data['linli_reply'] in question['instructions'] for question in port.calls[1][1].values())


def test_shared_evidence_keeps_two_existing_identities_and_their_own_kinds():
    data = {'user_letter': '好。', 'linli_reply': '我录完音了，也发给你了。', 'previous_state': {
        'projects': [{'id': 'record', 'title': '录音', 'kind': 'linli'}],
        'shared': [{'id': 'send', 'title': '发给用户', 'kind': 'shared'}]}}
    result = asyncio.run(extract(BoundPort([('我录完音了，', 'p0', 'completed'),
                                           ('也发给你了。', 'p1', 'completed')]), data, '', 'life:known'))
    assert [(item['id'], item['kind']) for item in result['updates']] == [('record', 'linli'), ('send', 'shared')]
    assert {item['quote'] for item in result['updates']} == {data['linli_reply']}


def test_new_identity_uses_action_and_evidence_instead_of_slot_number():
    data = {'user_letter': '好。', 'linli_reply': '我练完琴了。碗也洗好了。'}
    selections = [('我练完琴了。', 'new_linli', 'completed'), ('碗也洗好了。', 'new_linli', 'completed')]
    first = asyncio.run(extract(BoundPort(selections), data, '', 'life:order'))
    reversed_slots = asyncio.run(extract(BoundPort(list(reversed(selections))), data, '', 'life:order'))
    assert {item['title']: item['id'] for item in first['updates']} == {
        item['title']: item['id'] for item in reversed_slots['updates']}


def test_detailed_transport_is_consumed_without_an_extra_choice_request():
    class Detailed(BoundPort):
        async def ask(self, *args, **kwargs):
            raise AssertionError('detailed transport already carries the choices')

        async def ask_detailed(self, state, questions, *, purpose):
            choices = await super().ask(state, questions, purpose=purpose)
            return {key: {'choice': choice, 'confidence_source': 'unavailable'} for key, choice in choices.items()}

    port = Detailed([('我练完琴了。', 'new_linli', 'completed')])
    result = asyncio.run(extract(port, {'linli_reply': '我练完琴了。'}, '', 'life:details'))
    assert result['updates'][0]['status'] == 'completed'
    assert len(port.calls) == 2


def test_unproven_anchor_does_not_discard_an_independently_proven_action():
    data = {'linli_reply': '如果明天练完，我再告诉你。碗已经洗好了。'}
    port = BoundPort([('如果明天练完，我再告诉你。', 'none', 'none'),
                      ('碗已经洗好了。', 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:partial-proof'))
    assert [(item['quote'], item['status']) for item in result['updates']] == [('碗已经洗好了。', 'completed')]


def test_none_status_gates_an_existing_identity_without_claiming_an_update():
    data = {'linli_reply': '如果明天练完，我再告诉你。', 'previous_state': {
        'projects': [{'id': 'practice', 'kind': 'linli', 'title': '练琴'}]}}
    result = asyncio.run(extract(BoundPort([(data['linli_reply'], 'p0', 'none')]), data, '', 'life:uncertain'))
    assert result['updates'] == []


def test_sparse_slots_preserve_selected_action_without_rejecting_other_facts():
    data = {'linli_reply': '书已经寄好了。'}
    port = BoundPort([None, ('书已经寄好了。', 'new_shared', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:gap'))
    assert len(result['updates']) == 1
    assert result['updates'][0]['status'] == 'completed'


def test_identity_none_gates_unused_status_and_preserves_separate_valid_action():
    data = {'user_letter': '我已经忙完自己的工作。', 'linli_reply': '我练完琴了。'}
    port = BoundPort([(data['user_letter'], 'none', 'completed'),
                      (data['linli_reply'], 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:identity-gate'))
    assert [item['quote'] for item in result['updates']] == [data['linli_reply']]


def test_status_none_keeps_an_unchanged_identity_from_discarding_another_valid_action():
    data = {'linli_reply': '那份稿子还是之前的状态。刚才花已经浇好了。', 'previous_state': {
        'projects': [{'id': 'draft', 'title': '修改稿件', 'kind': 'linli', 'status': 'planned'}]}}
    port = BoundPort([('那份稿子还是之前的状态。', 'p0', 'none'),
                      ('刚才花已经浇好了。', 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:no-new-state'))
    assert [(item['quote'], item['status']) for item in result['updates']] == [('刚才花已经浇好了。', 'completed')]


def test_whole_anchor_containing_selected_action_does_not_mint_a_duplicate_new_fact():
    data = {'linli_reply': '我吃完午饭了，也洗好了碗。'}
    port = BoundPort([(data['linli_reply'], 'new_linli', 'completed'),
                      ('我吃完午饭了，', 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:containment'))
    assert len(result['updates']) == 1
    assert result['updates'][0]['title'] == '我吃完午饭了，'


@pytest.mark.parametrize('reply_status', ['cancelled', 'planned'])
def test_same_identity_deduplicates_equal_status_and_retains_user_cancellation(reply_status):
    data = {'user_letter': '录音今天不用发了。', 'linli_reply': '我今天会处理那份录音。',
            'previous_state': {'shared': [{'id': 'send', 'title': '发录音', 'kind': 'shared'}]}}
    port = BoundPort([(data['user_letter'], 'p0', 'cancelled'),
                      (data['linli_reply'], 'p0', reply_status)])
    result = asyncio.run(extract(port, data, '', 'life:duplicates'))
    assert [(item['id'], item['status'], item['actor'], item['quote']) for item in result['updates']] == [
        ('send', 'cancelled', 'user', data['user_letter'])]


def test_different_statuses_for_same_identity_still_fail_without_user_cancellation():
    data = {'linli_reply': '我练完这首曲子了。我现在还在练这首曲子。', 'previous_state': {
        'projects': [{'id': 'practice', 'title': '练曲', 'kind': 'linli'}]}}
    port = BoundPort([('我练完这首曲子了。', 'p0', 'completed'),
                      ('我现在还在练这首曲子。', 'p0', 'ongoing')])
    with pytest.raises(ValueError, match='JEV_EXCHANGE_CONFLICTING_UPDATES'):
        asyncio.run(extract(port, data, '', 'life:status-conflict'))


def test_long_comma_sentence_keeps_legal_support_windows():
    from runtime.private_world.jev_exchange import _action_anchors, _quotes
    reply = '，'.join(f'第{i}段' + '今天去了河边散步看到很多人在钓鱼' * 2 for i in range(9)) + '。'
    assert len(reply) > 240
    quotes = _quotes(reply, 'r')
    anchors = _action_anchors(quotes, {'user_letter': '', 'linli_reply': reply})
    assert anchors
    assert all(0 < len(anchor['support']) <= 240 and anchor['support'] in reply for anchor in anchors.values())


def test_same_surface_anchor_keeps_distinct_user_and_character_sources():
    from runtime.private_world.jev_exchange import _action_anchors, _quotes
    sources = {'user_letter': '我吃完午饭', 'linli_reply': '我吃完午饭并洗好了碗。'}
    quotes = {**_quotes(sources['user_letter'], 'u'), **_quotes(sources['linli_reply'], 'r')}
    anchors = _action_anchors(quotes, sources)
    assert {anchor['source'] for anchor in anchors.values() if anchor['text'] == '我吃完午饭'} == {
        'user_letter', 'linli_reply'}


def test_large_identity_directory_is_bounded_and_keyed_without_model_index_counting():
    projects = [{'id': f'archive-{i}', 'title': f'档案处理任务{i}', 'kind': 'linli', 'status': 'planned'}
                for i in range(50)]
    projects[49]['title'] = '数字化第九册日记'
    data = {'linli_reply': '我把第九册日记数字化好了。', 'previous_state': {'projects': projects},
            'previous_observation': {'activity': '整理档案', 'note': '在图书馆', 'status': 'ongoing'}}

    class SemanticOptions(BoundPort):
        async def ask(self, state, questions, *, purpose):
            result = await super().ask(state, questions, purpose=purpose)
            for key, question in questions.items():
                if key.endswith('_existing_match'):
                    # Each option is a key of the one shared directory, which names the project.
                    directory = state['exchange']['previous_state']
                    result[key] = next((option for option in question['criteria']
                                        if directory.get(option, {}).get('title') == projects[49]['title']), 'none')
            return result

    port = SemanticOptions([(data['linli_reply'], 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:large-directory'))
    assert result['updates'][0]['id'] == 'archive-49'
    second_state, second_questions, _ = port.calls[1]
    # 50 known projects exceed what a request may carry, whatever the history: the one the
    # reply mentions is among those shown, and every slot refers to the same directory.
    directory = second_state['exchange']['previous_state']
    assert len(directory) == jev_exchange.MAX_KNOWN_ITEMS < len(projects)
    assert len(port.calls[0][0]['exchange']['previous_state']['projects']) == jev_exchange.MAX_KNOWN_ITEMS
    key = next(key for key, entry in directory.items() if entry['title'] == projects[49]['title'])
    assert directory[key] == {'title': projects[49]['title'], 'kind': 'linli', 'status': 'planned'}
    assert set(second_questions['update_0_existing_match']['criteria']) == {'none', *directory}
    assert all(option == name for name, option in second_questions['update_0_existing_match']['criteria'].items()
               if name != 'none')
    assert 'action_anchors' not in second_state
    assert second_state['sources'] == port.calls[0][0]['sources']
    assert second_state['exchange']['previous_observation'] == data['previous_observation']


def test_bound_evidence_options_show_complete_text_and_prioritize_conditions():
    data = {'linli_reply': '待资料核验通过，明天再提交那份报告。'}
    port = BoundPort([('明天再提交那份报告。', 'new_linli', 'planned')])
    result = asyncio.run(extract(port, data, '', 'life:bound-complete-proof'))
    state, questions, _ = port.calls[1]
    question = questions['update_0_evidence']
    # The rule text is written once in the request; the question names the rule it follows.
    assert 'dependent_contract.evidence' in question['instructions']
    assert '最短' not in question['instructions'] and '最短' not in state['dependent_contract']['evidence']
    assert '完整证明优先' in state['dependent_contract']['evidence']
    for ref, meaning in question['criteria'].items():
        if ref == 'none':
            continue
        assert meaning['source'] == 'linli_reply'
        assert meaning['text'] == data['linli_reply'][slice(*meaning['span'])]
        assert meaning['span'] == state['quotes'][ref]
    assert result['updates'][0]['quote'] == data['linli_reply']


def test_future_continuation_anchor_keeps_the_current_item_status_contract():
    data = {'linli_reply': '这篇译稿目前搁置，下个月再继续翻译。', 'previous_state': {
        'projects': [{'id': 'draft', 'title': '翻译这篇稿子', 'kind': 'linli', 'status': 'ongoing'}]}}
    port = BoundPort([('下个月再继续翻译。', 'p0', 'paused')])
    result = asyncio.run(extract(port, data, '', 'life:current-pause'))
    assert 'dependent_contract.status' in port.calls[1][1]['update_0_status']['instructions']
    instructions = port.calls[1][0]['dependent_contract']['status']
    assert '锚点只定位同一事项' in instructions
    assert '当前实际生效' in instructions
    assert '未来恢复' in instructions
    assert result['updates'][0]['id'] == 'draft'
    assert result['updates'][0]['status'] == 'paused'


def test_user_source_role_kind_contradiction_rejects_only_that_item():
    data = {'user_letter': '同事刚完成了陈列布置。', 'linli_reply': '我已修好这盏灯。', 'previous_state': {
        'projects': [{'id': 'display', 'title': '陈列布置', 'kind': 'linli', 'status': 'ongoing'}]}}
    class UserKindPort(BoundPort):
        async def ask(self, state, questions, *, purpose):
            result = await super().ask(state, questions, purpose=purpose)
            if 'update_0_applicability_kind' in result:
                result['update_0_applicability_kind'] = 'shared'
            return result
    port = UserKindPort([(data['user_letter'], 'p0', 'completed'),
                      (data['linli_reply'], 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:bad-actor-proof'))
    assert len(result['updates']) == 1
    assert result['updates'][0]['quote'] == data['linli_reply']
    assert set(port.calls[1][1]['update_0_applicability_kind']['criteria']) == {'none', 'shared'}


@pytest.mark.parametrize('letter,action,status,previous', [
    ('只有评审确认通过才继续。我会下周提交修订。', '我会下周提交修订。', 'planned', None),
    ('那项刺绣现已暂停。以后有空我会继续这幅刺绣。', '以后有空我会继续这幅刺绣。', 'paused',
     {'id': 'embroidery', 'title': '做这幅刺绣', 'kind': 'linli', 'status': 'ongoing'}),
])
def test_complete_bound_evidence_can_retain_conditions_or_current_state_across_sentences(letter, action, status, previous):
    data = {'linli_reply': letter, 'previous_state': {'projects': [previous] if previous else []}}
    class CompleteProofPort(BoundPort):
        async def ask(self, state, questions, *, purpose):
            result = await super().ask(state, questions, purpose=purpose)
            for key, question in questions.items():
                if key.endswith('_evidence'):
                    result[key] = next(ref for ref, candidate in question['criteria'].items()
                                       if isinstance(candidate, dict) and candidate['text'] == letter)
            return result
    port = CompleteProofPort([(action, 'p0' if previous else 'new_linli', status)])
    result = asyncio.run(extract(port, data, '', 'life:cross-sentence-proof'))
    assert result['updates'][0]['quote'] == letter
    assert result['updates'][0]['status'] == status
    assert result['updates'][0]['detail'] == action


def test_independent_completed_action_can_use_its_own_complete_clause():
    data = {'linli_reply': '我会下周整理画册，刚才茶具已经擦好了。'}
    class IndependentProofPort(BoundPort):
        async def ask(self, state, questions, *, purpose):
            result = await super().ask(state, questions, purpose=purpose)
            for key, question in questions.items():
                if key.endswith('_evidence'):
                    desired = '我会下周整理画册，' if key.startswith('update_0_') else '刚才茶具已经擦好了。'
                    result[key] = next(ref for ref, candidate in question['criteria'].items()
                                       if isinstance(candidate, dict) and candidate['text'] == desired)
            return result
    port = IndependentProofPort([('我会下周整理画册，', 'new_linli', 'planned'),
                                 ('刚才茶具已经擦好了。', 'new_linli', 'completed')])
    result = asyncio.run(extract(port, data, '', 'life:independent-phases'))
    assert [(item['status'], item['quote']) for item in result['updates']] == [
        ('planned', '我会下周整理画册，'), ('completed', '刚才茶具已经擦好了。')]


@pytest.mark.parametrize('prior_status', ['planned', 'paused'])
def test_current_plan_contract_does_not_require_execution_or_an_old_status_change(prior_status):
    data = {'linli_reply': '我准备周六给长椅刷漆。', 'previous_state': {
        'projects': [{'id': 'bench', 'title': '长椅刷漆', 'kind': 'linli', 'status': prior_status}]}}
    port = BoundPort([(data['linli_reply'], 'p0', 'planned')])
    result = asyncio.run(extract(port, data, '', 'life:current-future-plan'))
    status_question = port.calls[1][1]['update_0_status']
    assert '本轮明确安排或调整' in status_question['criteria']['planned']
    rule = port.calls[1][0]['dependent_contract']['status']
    assert 'dependent_contract.status' in status_question['instructions']
    assert '旧status为planned或paused' in rule
    assert '不能仅因尚未执行或旧状态相同选none' in rule
    assert result['updates'][0]['id'] == 'bench'
    assert result['updates'][0]['status'] == 'planned'
