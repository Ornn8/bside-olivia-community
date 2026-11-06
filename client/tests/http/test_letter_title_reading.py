"""A letter's title is read from the small model's answer in the shapes it really comes back in."""
import pytest

from runtime.reply.letter_presentation import title_from_reply
from tests.http.test_cloud_contact_letter import SETUP
from tests.http.test_expression_context_routes import run_isolated


@pytest.mark.parametrize('reply, title', [
    ('{"title":"在家读书的晚上"}', '在家读书的晚上'),
    ('{\n  "title": "在家读书的晚上"\n}', '在家读书的晚上'),
    ('```json\n{"title": "在家读书的晚上"}\n```', '在家读书的晚上'),
    ('好的，标题如下：{"title":"「在家读书的晚上」"}', '在家读书的晚上'),
    ('{"title":"今晚的雨\n很想你"}', '今晚的雨 很想你'),                 # a raw line break inside the title
    ('{"title":"今晚的雨\n很想你"}', '今晚的雨 很想你'),                # an escaped one
    ('{"title":"\n今晚的雨\n"}', '今晚的雨'),
    ('{"note":"x"} {"title":"月亮很亮"}', '月亮很亮'),
    ('{"title":"Rain on the window"}', 'Rain on the window'),
])
def test_the_shapes_a_title_comes_back_in(reply, title):
    assert title_from_reply(reply) == title


@pytest.mark.parametrize('reply', [
    None, '', '好的', '{"title":""}', '{"title":"  \n "}', '{"title":3}', '{"name":"x"}', '["a"]',
    '{"title":"' + '很长' * 30 + '"}', '{"title":"' + '很长的一行' * 10 + '\n短"}', '{"title":"没写完',
])
def test_an_answer_without_a_usable_title_gives_none(reply):
    assert title_from_reply(reply) is None


def test_two_lines_too_long_together_keep_the_first_line_when_it_fits():
    line = '很长的一行' * 5
    assert title_from_reply('{"title":"%s\n%s"}' % (line, line)) == line


def test_a_letter_whose_first_title_answer_is_unusable_is_asked_once_more(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
answers = ['这是一封很温柔的信', '{"title":"月亮很亮"}']
async def two_tries(messages, *, request_id, scope):
    calls.append(dict(messages=tuple(dict(m) for m in messages), request_id=request_id, contact=contact_context.current()))
    if request_id.endswith(':title') or request_id.endswith(':title2'):
        return SimpleNamespace(text=answers.pop(0))
    return SimpleNamespace(text='今晚想和你说两句。\n[[signature:林离]]')
server.letters_adapter.gateway = SimpleNamespace(complete_scoped=two_tries, timeout_seconds_for_scope=lambda *a, **kw: 10)
async def main():
    contact_id = await carry(1)
    assert [call['request_id'].rsplit(':', 1)[-1] for call in calls] == ['body', 'title', 'title2']
    [letter] = letters_of(contact_id)
    assert letter['title'] == '月亮很亮' and letter['letter_status'] == 'COMPLETED'
asyncio.run(main())
''')


def test_a_letter_whose_title_cannot_be_read_twice_or_asked_for_keeps_the_usual_title(tmp_path):
    run_isolated(tmp_path, SETUP + r'''
async def never(messages, *, request_id, scope):
    calls.append(dict(messages=(), request_id=request_id, contact=None))
    if request_id.endswith(':title'):
        raise TimeoutError()
    if request_id.endswith(':title2'):
        raise AssertionError('a failed call is not asked again')
    return SimpleNamespace(text='今晚想和你说两句。\n[[signature:林离]]')
server.letters_adapter.gateway = SimpleNamespace(complete_scoped=never, timeout_seconds_for_scope=lambda *a, **kw: 10)
async def main():
    contact_id = await carry(1)
    [letter] = letters_of(contact_id)
    assert letter['title'] == cloud_contact.DEFAULT_TITLE and letter['letter_status'] == 'COMPLETED'
    assert [call['request_id'].rsplit(':', 1)[-1] for call in calls] == ['body', 'title']
asyncio.run(main())
''')
