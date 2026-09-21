import json

import pytest

from runtime.memory.recall_check import _quote_texts, _validate_finding


def world_source():
    return {'source':'s0', 'scope':'evidence_summary', 'text':json.dumps({
        'fragment_id':'linli.daily-life', 'untrusted':True,
        'text':json.dumps({'kind':'character_life_reference', 'stale':False,
            'current':{'source_id':'day:meal', 'actor':'linli',
                'occurred_at':'2026-09-21T18:03:00+08:00', 'activity':'吃晚饭', 'note':'青菜腐竹配饭'}}, ensure_ascii=False)},ensure_ascii=False)}


def test_nested_world_wire_fields_are_not_quote_evidence():
    source = world_source()
    assert _quote_texts(source['text']) == ['吃晚饭', '青菜腐竹配饭']
    finding = dict(topic='晚饭', status='confirmed', event_stage='reported', finding='在吃晚饭',
                   citations=[{'source':'s0', 'quote':'"activity": "吃晚饭"'}])
    with pytest.raises(ValueError, match='RECALL_CHECK_INVALID'):
        _validate_finding(finding, {'s0':source})
    finding['citations'] = [{'source':'s0','quote':'青菜腐竹配饭'}]
    _validate_finding(finding, {'s0':source})


def test_rhythm_wire_fields_are_not_quotable_original_text():
    text=json.dumps({'fragment_id':'linli.rhythm', 'text':json.dumps({
        'activity':'晚饭时间','note':'按自己的节奏生活。','availability':'busy'},ensure_ascii=False)},ensure_ascii=False)
    assert _quote_texts(text) == ['晚饭时间','按自己的节奏生活。']


def test_preflight_encoding_keeps_records_and_world_as_data_without_reencoding_originals():
    from runtime.memory.recall_check import _source_packet
    world = world_source()
    quote = '他说："还没吃"；{"我":"用户"}'
    historical = {'source':'s1', 'scope':'historical_exchange', 'text':json.dumps([
        {'citation':'reply:a:user', 'speaker':'user', 'occurred_at':'2026-09-21T18:00:00+08:00', 'text':quote}],ensure_ascii=False)}
    before = json.dumps([world,historical],ensure_ascii=False)
    packet = json.loads(_source_packet('你吃什么', [world,historical]))
    current = packet['sources'][0]['text']['text']['current']
    assert current['note'] == '青菜腐竹配饭'
    assert current['source_id'] == 'day:meal'
    assert current['occurred_at'] == '2026-09-21T18:03:00+08:00'
    record = packet['sources'][1]['text'][0]
    assert record['text'] == quote
    assert record['speaker'] == 'user'
    assert json.dumps([world,historical],ensure_ascii=False) == before


def test_bad_additional_reference_does_not_erase_verified_original():
    from runtime.memory.recall_check import _validate
    source = world_source()
    value = dict(reply_intent='recall_question',direct_questions=['你吃什么'],findings=[
        dict(topic='晚饭',status='confirmed',event_stage='completed',finding='记录包含青菜腐竹配饭',
             citations=[{'source':'s0','quote':'青菜腐竹配饭'}, {'source':'s0','quote':'note: 青菜腐竹配饭'}])])
    result=_validate(value,[source,{'source':'current','scope':'current_user_statement','text':'你吃什么'}])
    assert result['status'] == 'partial'
    item=result['findings'][0]
    assert item['status'] == 'uncertain'
    assert item['event_stage'] == 'unknown'
    assert item['citations'] == [{'source':'s0','quote':'青菜腐竹配饭'}]


def test_question_paraphrase_is_discarded_without_losing_valid_evidence():
    from runtime.memory.recall_check import _validate
    source=world_source()
    value=dict(reply_intent='action_request',direct_questions=['你吃什么','拍给我看看（晚餐）'],findings=[
        dict(topic='晚饭',status='confirmed',event_stage='reported',finding='记录包含青菜腐竹配饭',
             citations=[{'source':'s0','quote':'青菜腐竹配饭'}])])
    result=_validate(value,[source,{'source':'current','scope':'current_user_statement','text':'你吃什么？拍给我看看'}])
    assert result['status'] == 'partial'
    assert result['direct_questions'] == ['你吃什么']
    assert result['findings'][0]['citations']
@pytest.mark.parametrize('current', ['{"text":"请保留这段 JSON 原文"}', '"请保留两侧引号"'])
def test_current_user_json_is_literal_evidence_not_a_source_container(current):
    from runtime.memory.recall_check import _validate
    source = {'source': 'current', 'scope': 'current_user_statement', 'text': current}
    value = dict(reply_intent='sharing', direct_questions=[], findings=[
        dict(topic='当前原文', status='confirmed', event_stage='reported', finding='用户提供的原文',
             citations=[dict(source='current', quote=current)])])
    result = _validate(value, [source])
    assert result['findings'][0]['citations'][0]['quote'] == current
