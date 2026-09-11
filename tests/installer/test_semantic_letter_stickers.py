from datetime import datetime, timezone

from runtime.reply.reply_context import BehaviorLevel as L, PrivateBehaviorView, ReplyContext, ReplyMode, TrustedTime
from runtime.letter_stickers.selection import allowed_stickers, split_selection, selection_instruction


def test_progressive_unlock_and_unknown_is_not_high():
    base=allowed_stickers(PrivateBehaviorView())
    familiar=allowed_stickers(PrivateBehaviorView(familiarity=L.HIGH))
    close=allowed_stickers(PrivateBehaviorView(familiarity=L.HIGH,trust=L.MEDIUM,comfort=L.MEDIUM))
    assert set(base)<set(familiar)<set(close)
    assert len(close)==72
    assert 'linli-25' not in base and 'linli-25' in familiar
    assert 'linli-27' not in familiar and 'linli-27' in close


def test_high_closeness_unlocks_new_whimsical_sets_only_with_trust_and_comfort():
    high = PrivateBehaviorView(familiarity=L.HIGH, trust=L.HIGH, comfort=L.HIGH, closeness=L.HIGH)
    assert len(allowed_stickers(PrivateBehaviorView())) == 29
    assert len(allowed_stickers(PrivateBehaviorView(familiarity=L.MEDIUM))) == 54
    assert len(allowed_stickers(high)) == 108
    assert 'linli-108' in allowed_stickers(high)
    from dataclasses import replace
    for field in ('trust', 'comfort', 'closeness'):
        for level in (L.UNKNOWN, L.LOW, L.MEDIUM):
            assert 'linli-73' not in allowed_stickers(replace(high, **{field: level}))
    assert split_selection('正文。\n[[sticker:linli-108]]', allowed_stickers(high)) == ('正文。','linli-108')
    assert split_selection('正文。\n[[sticker:linli-108]]', allowed_stickers(PrivateBehaviorView())) == ('正文。','linli-01')


def test_selection_removed_before_text_consumers_and_locked_choice_falls_back():
    allowed=allowed_stickers(PrivateBehaviorView())
    assert split_selection('信的正文。\n[[sticker:linli-07]]',allowed)==('信的正文。','linli-07')
    assert split_selection('信的正文。\n[[sticker:linli-27]]',allowed)==('信的正文。','linli-01')
    assert split_selection('信的正文。\n[[sticker:garbage]]',allowed)==('信的正文。','linli-01')
    assert split_selection('信的正文。',allowed)==('信的正文。','linli-01')
    assert split_selection('信的正文。\n[[sticker:linli-',allowed)==('信的正文。','linli-01')
    assert split_selection('信的正文。[[sticker:linli-07]]',allowed)==('信的正文。','linli-07')
    assert split_selection('信的正文。[STICKER:linli-07]',allowed)==('信的正文。','linli-01')


def test_candidates_only_contain_unlocked_choices():
    note=selection_instruction(allowed_stickers(PrivateBehaviorView()))
    assert 'linli-07' in note and 'linli-27' not in note


def test_one_generation_call_metadata_does_not_reach_review():
    import asyncio
    from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    class Writer:
        calls=0
        async def run(self, request):
            self.calls+=1
            self.messages=request.normalized_messages()
            return ReplyResult('test',ReplyState.COMPLETED,text='今天练琴很顺。\n[[sticker:linli-07]]')
    writer=Writer()
    pipeline=ReplyPipeline(writer,reviewer=NullReviewer(),rewriter=UnavailableRewriter(),discover_runtime_ports=False)
    context=ReplyContext.create(ReplyMode.TEXT_LETTER,trusted_time=TrustedTime(datetime.now(timezone.utc)))
    result=asyncio.run(pipeline.run(ReplyRequest(messages=({'role':'system','content':'按人设写信。'},{'role':'user','content':'今天怎么样？'})),context))
    assert writer.calls==1 and result.rewrite_calls==0
    assert result.text=='今天练琴很顺。' and result.sticker_id=='linli-07'
    assert '[[sticker:' in writer.messages[0]['content']
    assert 'linli-27' not in writer.messages[0]['content']


def test_persisted_metadata_is_only_exposed_after_publication():
    from original_client_letter_contract import serialize_letter_detail
    letter=dict(letter_id='sticker-fixture',content='合成来信',reply_text='合成回信',letter_status='COMPLETED',reply_sticker_id='linli-108',reply_not_before=200)
    assert 'replyStickerId' not in serialize_letter_detail(letter,now=100)
    assert serialize_letter_detail(letter,now=300)['replyStickerId']=='linli-108'
