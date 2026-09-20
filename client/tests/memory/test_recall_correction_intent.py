import json

from runtime.memory.recall_check import _validate, _project


def test_correction_is_a_supported_turn_intent_and_keeps_the_disputed_original():
    quote='我吃了面，你也就吃这个吗'
    sources=[{'source':'s0','scope':'historical_exchange','text':json.dumps([
        {'citation':'reply:a:linli','speaker':'linli','text':quote}],ensure_ascii=False)},
        {'source':'current','scope':'current_user_statement','text':'我吃啥了？'}]
    value=dict(reply_intent='correction',direct_questions=['我吃啥了？'],findings=[
        dict(topic='上轮对用户饮食的无根据推断',status='uncertain',event_stage='unknown',
             finding='角色没有依据断言用户吃了什么',citations=[{'source':'s0','quote':quote}])])
    checked=_validate(value,sources)
    messages=({'role':'system','content':'证据规则'}, {'role':'user','content':'我吃啥了？'})
    result=_project(messages,checked,sources,max_input_chars=10000)
    assert checked['reply_intent']=='correction'
    assert result[-1]==messages[-1]
    assert '<reply_focus>' in result[0]['content']
    assert '纠正上一轮发言' in result[0]['content']
    assert quote in result[0]['content']
