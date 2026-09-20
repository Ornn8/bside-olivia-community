import json

from runtime.memory.recall_check import _validate, _project


def checked(speaker, status='uncertain'):
    quote = '我刚吃完饭'
    sources = [{'source':'s0', 'scope':'historical_exchange', 'text':json.dumps([
        dict(citation='reply:dinner:'+speaker, speaker=speaker, occurred_at='2026-09-21T18:00:00+08:00',text=quote)],ensure_ascii=False)},
        {'source':'current','scope':'current_user_statement','text':'你吃什么'}]
    value = dict(reply_intent='recall_question',direct_questions=['你吃什么'],findings=[
        dict(topic='晚饭',status=status,event_stage='reported',finding='这句话属于对应说话人的说法，具体餐食未确定',
             citations=[dict(source='s0',quote=quote)])])
    return _validate(value,sources),sources


def test_uncertain_character_statement_is_not_relabelled_as_user_report():
    value,_ = checked('linli')
    assert value['findings'][0]['continuity_scope'] == 'character_statement'
    value,_ = checked('user')
    assert value['findings'][0]['continuity_scope'] == 'user_report'


def test_projection_preserves_matched_speaker_and_time_without_copying_context():
    value,sources = checked('linli')
    result = _project(({'role':'system','content':'rules'}, {'role':'user','content':'你吃什么'}),
                      value,sources,max_input_chars=10000)
    import re
    payload=json.loads(re.search(r'<recall_check>\s*(.*?)\s*</recall_check>',result[0]['content'],re.S).group(1))
    ref=payload['originals'][payload['events'][0]['originals'][0]]
    assert ref['records'] == [dict(citation='reply:dinner:linli',speaker='linli',occurred_at='2026-09-21T18:00:00+08:00')]
    assert 'context' not in str(ref)


def test_unidentified_reference_does_not_become_user_testimony():
    value,sources=checked('unknown')
    assert value['findings'][0]['continuity_scope'] == 'unresolved'


def test_speaker_does_not_prove_the_experience_belongs_to_that_speaker():
    quote = '你吃了面，我还没吃'
    sources = [{'source': 's0', 'scope': 'historical_exchange', 'text': json.dumps([
        dict(citation='reply:meal:linli', speaker='linli', text=quote)], ensure_ascii=False)}]
    value = dict(reply_intent='recall_question', direct_questions=[], findings=[
        dict(topic='晚饭', status='confirmed', event_stage='completed',
             finding='原文提到吃面', citations=[dict(source='s0', quote=quote)])])
    result = _validate(value, sources)
    assert result['findings'][0]['continuity_scope'] == 'character_statement'


def test_mixed_speaker_quotes_do_not_become_one_character_experience():
    sources = [{'source': 's0', 'scope': 'historical_exchange', 'text': json.dumps([
        dict(citation='reply:meal:user', speaker='user', text='我吃了面'),
        dict(citation='reply:meal:linli', speaker='linli', text='我还没吃')], ensure_ascii=False)}]
    value = dict(reply_intent='recall_question', direct_questions=[], findings=[
        dict(topic='晚饭', status='confirmed', event_stage='completed', finding='双方的晚饭说法',
             citations=[dict(source='s0', quote='我吃了面'), dict(source='s0', quote='我还没吃')])])
    result = _validate(value, sources)
    assert result['findings'][0]['continuity_scope'] == 'unresolved'
