import asyncio
from datetime import datetime, timezone

from runtime.reply.letter_presentation import split_signature, display_letter


def test_signature_metadata_and_paragraphs_are_separate():
    body, signature = split_signature('第一段。\r\n\r\n\r\n第二段。\n[[signature:阿离]]')
    assert body == '第一段。\r\n\r\n\r\n第二段。'
    assert signature == '阿离'
    assert display_letter(body, signature) == body + '\n\n阿离'
    original = '  第一段。\n\n\n  第二段。\n'
    assert split_signature(original)[0] == original


def test_missing_invalid_and_truncated_signature_default():
    for suffix in ('', '\n[[signature:]]', '\n[[signature:<script>]]', '\n[[signature:阿'):
        assert split_signature('正文。' + suffix) == ('正文。', '林离')
    assert split_signature('正文。\n\n林离\n[[signature:林离]]') == ('正文。', '林离')
    assert split_signature('正文。\n\n阿离\n[[signature:阿离]]') == ('正文。', '阿离')
    assert split_signature('我今天遇到林离。')[0] == '我今天遇到林离。'


def test_one_call_body_never_contains_signature_or_sticker():
    from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
    from runtime.reply.reply_context import ReplyContext, ReplyMode, TrustedTime
    from runtime.reply.reply_pipeline import ReplyPipeline, UnavailableRewriter
    from runtime.reply.reply_reviewer import NullReviewer
    class Writer:
        calls = 0
        async def run(self, request):
            self.calls += 1
            self.messages = request.normalized_messages()
            return ReplyResult('test', ReplyState.COMPLETED,
                               text='第一段。\n\n第二段。\n[[signature:阿离]]\n[[sticker:linli-07]]')
    writer = Writer()
    pipeline = ReplyPipeline(writer, reviewer=NullReviewer(), rewriter=UnavailableRewriter(), discover_runtime_ports=False)
    context = ReplyContext.create(ReplyMode.TEXT_LETTER, trusted_time=TrustedTime(datetime.now(timezone.utc)))
    result = asyncio.run(pipeline.run(ReplyRequest(messages=({'role':'user','content':'阿离，今天怎么样？'},)), context))
    assert writer.calls == 1
    assert result.text == '第一段。\n\n第二段。'
    assert result.signature == '阿离' and result.sticker_id == 'linli-07'
    assert '用户自己的名字' in writer.messages[0]['content']


def test_signature_is_only_displayed_on_published_new_letters():
    from original_client_letter_contract import serialize_letter_detail
    letter = dict(letter_id='format', content='合成信', reply_text='正文。', reply_signature='阿离',
                  letter_status='COMPLETED', reply_mode='text_letter', reply_not_before=200)
    assert serialize_letter_detail(letter, now=100)['replyText'] == ''
    assert 'replySignature' not in serialize_letter_detail(letter, now=100)
    assert serialize_letter_detail(letter, now=300)['replyText'] == '正文。\n\n阿离'
    assert serialize_letter_detail(letter, now=300)['replyBody'] == '正文。'
    assert serialize_letter_detail(letter, now=300)['replySignature'] == '阿离'
    assert letter['reply_text'] == '正文。'
    del letter['reply_signature']
    assert serialize_letter_detail(letter, now=300)['replyText'] == '正文。'
    assert 'replySignature' not in serialize_letter_detail(letter, now=300)


def test_display_never_invents_paragraphs_or_changes_authored_whitespace():
    from original_client_letter_contract import serialize_letter_detail
    body = '今天练琴后沿着河边走了一会儿，风很轻，街角的小店已经开门了。' * 5
    letter = dict(letter_id='synthetic-layout', content='合成信', reply_text=body,
                  reply_signature='林离', letter_status='COMPLETED', reply_mode='text_letter')
    payload = serialize_letter_detail(letter)
    assert payload['replyBody'] == body
    assert letter['reply_text'] == body
    authored = body[:30] + '\n\n' + body[30:]
    letter['reply_text'] = authored
    assert serialize_letter_detail(letter)['replyBody'] == authored
    letter['reply_text'] = '很短的一句话。'
    assert serialize_letter_detail(letter)['replyBody'] == '很短的一句话。'
