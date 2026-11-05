"""A letter he quotes back (typed or photographed) is recognized as the stored original.

All letters here are synthetic. The shape mirrors an imported backup in which her undated
farewell reply also appears inside a later letter of his that re-sent it.
"""
import asyncio
import base64
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from runtime.imports.letter_backup import import_letters
from runtime.memory.archive_dates import estimate_dates
from runtime.memory.local_memory import LocalMemoryAdapter
from runtime.memory.memory_port import NullMemoryPort
from runtime.memory.memory_prompt import MemoryPromptBuilder
from runtime.memory.quoted_letters import FRAGMENT_ID, lead_quoted_sources, lookup, passages
from runtime.memory.recall import RecallResult

STOCK = '今天也要好好吃饭，早点睡觉。'
FAREWELL = ('亲爱的你：\n'
            '写这封信的时候，窗外的雨刚停。我要出一趟很远的门，可能有一阵子不能按时回信了。\n'
            '钢琴我盖好了琴布，那首我们说好一起弹完的曲子，第二段我还没练熟，等我回来再弹给你听。\n'
            '你寄来的每一封信我都收在蓝色的铁盒里，路上我会带着它们，想你的时候就拿出来读一遍。\n'
            '旅行的计划我写在最后一页：先坐夜车去海边，再沿着海岸往北走，走到看得见灯塔的地方。\n'
            '不要难过，也不要每天守着信箱。' + STOCK + '等我把路走完，我会第一个写信告诉你。\n林离')
RESENT = '我把你那封告别信又抄了一遍寄给你：\n' + FAREWELL + '\n我会一直等你。'


def letter(content, reply, created_at):
    return {'content': content, 'reply_text': reply, 'created_at': created_at, 'replied_at': None,
            'title': '', 'origin': 'user', 'reply_mode': 'text', 'letter_status': 'COMPLETED', 'source_id': ''}


def backup():
    # Export order: newest first. The farewell exchange lost its date.
    rows = [
        letter('九月的桂花开了，我摘了一小枝夹在信里。', '桂花的味道隔着信纸也闻得到。' + STOCK, '2026-09-12T20:00:00+08:00'),
        letter(RESENT, '收到你抄的那封信了，原来你一直留着。', '2026-09-10T21:00:00+08:00'),
        letter('你走了以后，院子里的猫每天都来门口等。' + STOCK, '替我摸摸它的头。', '2026-08-20T20:00:00+08:00'),
        letter('再见', FAREWELL, None),
        letter('下个月你真的要出门吗？我有点舍不得。', '还没定下来，定了第一个告诉你。', '2026-08-01T20:00:00+08:00'),
    ]
    for day in range(1, 7):
        rows.append(letter(f'七月{day}日，天气很热，我在楼下买了西瓜。' + STOCK,
                           f'西瓜要冰过才好吃。{day}号的晚霞我也看到了。' + STOCK, f'2026-07-{day:02d}T20:00:00+08:00'))
    return {'schema_version': 'olivia.letters.v1', 'letters': rows}


@pytest.fixture
def archive(tmp_path):
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as memory:
        import_letters(backup(), adapter=memory)
        yield memory


def adapter_for(memory, letters=()):
    return SimpleNamespace(memory_prompt_builder=MemoryPromptBuilder(memory, conversation_memory=None),
                           recent_letters=lambda: list(letters))


def note(fragment):
    assert fragment is not None and fragment.fragment_id == FRAGMENT_ID
    return json.loads(fragment.text)


def test_undated_letter_gets_a_range_from_its_import_neighbours(archive):
    rows = archive.list_legacy()
    estimates = estimate_dates(rows)
    farewell = next(row for row in rows if row['metadata']['reply_text'] == FAREWELL)
    assert farewell['occurred_at'] is None
    estimate = estimates[farewell['source_record_id']]
    assert estimate.earliest.date().isoformat() == '2026-08-01'
    assert estimate.latest.date().isoformat() == '2026-08-20'
    assert estimate.earliest < estimate.stamp < estimate.latest
    assert estimate.label() == '大约在2026年8月1日到2026年8月20日之间'
    # Dated rows are never re-dated; the Archive still says unknown.
    assert set(estimates) == {farewell['source_record_id']}


def test_estimates_follow_an_oldest_first_file_and_stay_unknown_without_dated_neighbours(tmp_path):
    rows = [letter('第一封', '回一', '2026-05-01T20:00:00+08:00'), letter('第二封', '回二', None),
            letter('第三封', '回三', '2026-05-09T20:00:00+08:00')]
    with LocalMemoryAdapter(tmp_path / 'a.sqlite3') as memory:
        import_letters({'schema_version': 'olivia.letters.v1', 'letters': rows}, adapter=memory)
        estimate, = estimate_dates(memory.list_legacy()).values()
        assert (estimate.earliest.day, estimate.latest.day) == (1, 9)
    with LocalMemoryAdapter(tmp_path / 'b.sqlite3') as memory:
        import_letters([{'content': '没有日期的信', 'reply': '没有日期的回信'}], adapter=memory)
        assert estimate_dates(memory.list_legacy()) == {}


def test_pasted_farewell_letter_is_recognized_as_her_own_with_its_context(archive):
    content = '这是你给我的告别信，当时把我弄得特别emo呢~\n' + FAREWELL
    fragment, sources = lookup(adapter_for(archive), content)
    value = note(fragment)
    match, = value['matches']
    assert match['found_in'] == '他这轮消息里贴的文字'
    assert match['from'].startswith('你（林离）写给他')
    assert match['extent'] == '整封信'
    assert match['when'].startswith('大约在2026年8月1日到2026年8月20日之间')
    # What he wrote just before and after her letter.
    assert match['before'] == {'speaker': '他', 'when': '大约在2026年8月1日到2026年8月20日之间（原信没写日期，按前后的信推算）',
                               'opening': '再见'}
    assert match['after']['speaker'] == '他' and match['after']['opening'].startswith('你走了以后')
    # The later letter that re-sent it is named, not mistaken for the original.
    assert match['also_in'] == [{'speaker': '他', 'when': '2026年9月10日'}]
    assert len(sources) == 1
    assert '不是新来信' in value['meaning'] or '不是一封新来信' in value['meaning']


def test_photographed_letter_with_reading_errors_is_still_matched(archive):
    from runtime.image_understanding import incoming_context
    read = (FAREWELL.replace('铁盒', '铁合').replace('灯塔', '灯搭').replace('，', ' ')
            .replace('第二段', '第二投'))
    observation = {'source': 'user', 'sha256': 'b' * 64, 'summary': '一张写满字的信纸，末尾有署名。',
                   'text': read, 'observed_at': '2026-10-29T07:47:00+00:00', 'evidence_kind': 'visual_observation'}
    row = {'letter_id': 'qq-1', 'channel': 'qq', 'incoming_images': [['img', 'https://qq.com/x']],
           'incoming_image_observations': [observation]}
    content = '这是你给我的告别信，当时把我弄得特别emo呢~' + incoming_context(row)
    assert ('photo', read) in passages(content)
    fragment, sources = lookup(adapter_for(archive), content)
    match, = note(fragment)['matches']
    assert match['found_in'] == '他发来的照片里的字' and match['from'].startswith('你（林离）')
    assert sources


def test_short_messages_and_stock_phrases_never_match_or_read_the_archive(archive):
    reads = []
    adapter = adapter_for(archive)
    original = archive.list_legacy
    archive.list_legacy = lambda: reads.append(1) or original()
    assert lookup(adapter, '晚安，' + STOCK) == (None, ())
    assert not reads  # too short to quote a letter: no archive read at all
    stock_only = '\n'.join([STOCK] * 4 + ['我在楼下买了西瓜，西瓜要冰过才好吃，晚霞我也看到了。'])
    assert lookup(adapter, stock_only) == (None, ())


def test_forgotten_letter_is_not_recognized(archive):
    from runtime.memory.companion_memory_context import _historical_source_id
    farewell = next(row for row in archive.list_legacy() if row['metadata']['reply_text'] == FAREWELL)
    excluded = (_historical_source_id(farewell['source_record_id']),)
    fragment, sources = lookup(adapter_for(archive), FAREWELL, excluded=excluded)
    # Only the later re-sent copy (his letter) can still match.
    match, = note(fragment)['matches']
    assert match['from'] == '他写给你的信' and match['when'] == '2026年9月10日'


def test_his_own_mailbox_letter_is_matched_but_never_the_letter_being_answered():
    text = ('那天我们在河边走了很久，你说桥下的水声像有人在弹琴。后来我们去了那家只开到九点的面馆，'
            '老板多给了我们一碟花生米。回家的路上路灯一盏一盏亮起来，我第一次觉得这条路太短了。'
            '到家以后我把那天的车票夹进了日记本，现在翻开还能看见上面印着的日期。')
    mailbox = [{'letter_id': 'old', 'content': text, 'reply_text': '我也记得那碟花生米。',
                'letter_status': 'COMPLETED', 'created_at': 1788000000, 'replied_at': 1788003600},
               {'letter_id': 'now', 'content': text, 'letter_status': 'RECEIVED', 'created_at': 1790000000}]
    adapter = SimpleNamespace(memory_prompt_builder=None, recent_letters=lambda: mailbox)
    fragment, sources = lookup(adapter, text, excluded=('reply:now:1',))
    match, = note(fragment)['matches']
    assert match['from'] == '他写给你的信' and match['when'] == '2026年8月29日'
    assert match['after']['opening'] == '我也记得那碟花生米。'
    assert sources == ()  # mailbox letters are already in the current memory index
    assert lookup(SimpleNamespace(memory_prompt_builder=None, recent_letters=lambda: mailbox[1:]), text,
                  excluded=('reply:now:1',)) == (None, ())


def test_quoted_exchange_leads_recall_ahead_of_other_hits(archive):
    builder = MemoryPromptBuilder(archive, conversation_memory=None, max_results=20, max_tokens=300000)
    adapter = SimpleNamespace(memory_prompt_builder=builder, recent_letters=lambda: [])
    _, sources = lookup(adapter, FAREWELL)
    recall = builder.collect(FAREWELL)
    led = lead_quoted_sources(adapter, recall, sources)
    first = led.records[:2]
    assert [r.metadata['speaker'] for r in first] == ['user', 'linli']
    assert first[1].text == FAREWELL and first[1].metadata['retrieval_route'] == 'quoted_letter'
    assert first[1].metadata['estimate'] == '大约在2026年8月1日到2026年8月20日之间'
    assert dict(led.source_status)['quoted_letter'] == 'available'
    rendered = builder.render(led, max_chars=6000).text
    from runtime.memory.memory_prompt import _unescape_reserved
    assert '"estimated_time":"大约在2026年8月1日到2026年8月20日之间"' in _unescape_reserved(rendered)
    assert lead_quoted_sources(SimpleNamespace(memory_prompt_builder=None), recall, sources) is recall


def test_reply_assembly_carries_the_note_and_keeps_the_letter_as_anchor(archive):
    from persona_loader import load_persona
    from reply_context import ReplyContext, ReplyMode, TrustedTime
    from runtime.reply.reply_pipeline import assemble_reply_messages
    snapshot = load_persona('linli_character/persona_release_v2.json').snapshot
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True,
                                  trusted_time=TrustedTime(datetime(2026, 10, 29, 7, 51, tzinfo=timezone.utc)))
    adapter = SimpleNamespace(memory_prompt_builder=MemoryPromptBuilder(
        archive, conversation_memory=None, max_results=20, max_tokens=300000), recent_letters=lambda: [])
    # A reunion greeting alone would swap recall for the two latest dated letters;
    # the matched letter is the anchor instead.
    content = '好久不见~这是你给我的告别信，当时把我弄得特别emo呢~\n' + FAREWELL
    messages, _ = assemble_reply_messages(adapter, snapshot, context, content, max_input_chars=60000)
    system = messages[0]['content']
    assert 'quoted_earlier_letter' in system
    from runtime.memory.memory_prompt import _unescape_reserved
    decoded = _unescape_reserved(json.loads(json.dumps(system)))
    assert '[ORIGINAL_CORRESPONDENCE_UNTRUSTED]' in decoded
    assert '大约在2026年8月1日到2026年8月20日之间' in decoded


def test_memory_disabled_reads_nothing(archive):
    from persona_loader import load_persona
    from reply_context import ReplyContext, ReplyMode, TrustedTime
    from runtime.reply.reply_pipeline import assemble_reply_messages
    snapshot = load_persona('linli_character/persona_release_v2.json').snapshot
    context = ReplyContext.create(ReplyMode.FUTURE_IM, future_im_enabled=True, trusted_time=TrustedTime(datetime(2026, 10, 29, tzinfo=timezone.utc)))
    renderer = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None)
    adapter = SimpleNamespace(memory_prompt_builder=MemoryPromptBuilder(archive, conversation_memory=None),
                              recent_letters=lambda: [], _memory_context_limit=lambda: 0,
                              _build_memory_prompt=lambda content, max_chars: renderer.render(RecallResult(), max_chars=0))
    messages, _ = assemble_reply_messages(adapter, snapshot, context, FAREWELL, max_input_chars=60000)
    assert 'quoted_earlier_letter' not in messages[0]['content']


def test_vision_reads_the_text_of_a_photographed_page(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from PIL import Image
    from runtime import image_understanding as vision
    path = tmp_path / 'page.png'
    Image.new('RGB', (800, 1000), (250, 250, 240)).save(path)
    replies = [{'summary': '一张写满字的信纸。', 'text': '  第一行\n第二行  '}, {'summary': '窗边的一只猫。'},
               {'summary': '信纸', 'text': 7}]

    async def scenario():
        requests = []
        async def complete(request):
            requests.append(await request.json())
            return web.json_response({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(replies[len(requests) - 1], ensure_ascii=False)}}]})
        app = web.Application(); app.router.add_post('/v1/chat/completions', complete)
        async with TestServer(app) as endpoint:
            server = SimpleNamespace(letters_adapter=SimpleNamespace(
                config=SimpleNamespace(base_url=str(endpoint.make_url('/v1')), api_key_env=''),
                gateway=SimpleNamespace(_key=lambda: 'synthetic')))
            page = await vision.describe_image(server, path, source='user')
            cat = await vision.describe_image(server, path, source='user')
            with pytest.raises(ValueError, match='IMAGE_VISION_INVALID'):
                await vision.describe_image(server, path, source='user')
        prompt = requests[0]['messages'][0]['content']
        assert '"text"' in prompt and requests[0]['max_tokens'] >= 2048
        return page, cat

    page, cat = asyncio.run(scenario())
    assert page['text'] == '第一行\n第二行' and 'text' not in cat
    stored = vision.validate_observation(page)
    assert stored['text'] == page['text'] and 'text' not in vision.validate_observation(cat)
    with pytest.raises(ValueError):
        vision.validate_observation({**page, 'text': ''})
    row = {'letter_id': 'qq-2', 'channel': 'qq', 'incoming_image_observations': [page]}
    memory_row, = vision.image_memory_rows(row)
    assert '第一行\n第二行' in memory_row['content']
    assert json.loads(vision.incoming_context(row).split('\n', 2)[2])['images'][0]['text'] == '第一行\n第二行'


def test_undated_letter_is_indexed_among_its_neighbours_with_unknown_time(tmp_path, archive):
    from runtime.memory.conversation_memory_delivery import ConversationMemoryDeliveryCommitter
    from runtime.memory.conversation_memory_outbox import CanonicalMemoryOutbox
    writes = []

    class Memory:
        def index_original_exchange(self, **kw):
            writes.append(kw)
            return True

    outbox = CanonicalMemoryOutbox(tmp_path / 'state.json', tmp_path / 'outbox.sqlite3',
                                   ConversationMemoryDeliveryCommitter(Memory()), archive_memory=archive)
    asyncio.run(outbox.scan_once())
    order = [w['assistant_message'] for w in writes]
    farewell = order.index(FAREWELL)
    # Newest first: after the 08-20 letter, before the 08-01 one, not after all of July.
    assert order[farewell - 1] == '替我摸摸它的头。'
    assert order[farewell + 1] == '还没定下来，定了第一个告诉你。'
    assert writes[farewell]['occurred_at'] is None  # the index never stores the estimate as a date


def test_reply_time_dates_an_exchange_and_one_sided_neighbours_stay_unknown(tmp_path):
    rows = [letter('最新的一封', '回最新', '2026-06-09T20:00:00+08:00'),
            letter('较早的一封', '回较早', '2026-06-01T20:00:00+08:00'),
            {**letter('没有寄出日期', '但回信有日期', None), 'replied_at': '2026-05-20T21:00:00+08:00'},
            letter('排在文件末尾的信', '也没有日期', None)]
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as memory:
        import_letters({'schema_version': 'olivia.letters.v1', 'letters': rows}, adapter=memory)
        listed = {row['metadata']['user_content']: row for row in memory.list_legacy()}
        estimates = estimate_dates(listed.values())
    replied = estimates[listed['没有寄出日期']['source_record_id']]
    assert replied.basis == 'reply_time' and replied.label() == '大约2026年5月20日'
    # An export sorts undated letters to the end; one dated side proves nothing.
    assert listed['排在文件末尾的信']['source_record_id'] not in estimates


def test_undated_original_is_bounded_by_the_dated_letter_that_re_sent_it(tmp_path):
    rows = [letter(RESENT, '收到你抄的那封信了。', '2026-09-10T21:00:00+08:00'),
            letter('再见', FAREWELL, None)]  # undated and last in the file: no neighbour estimate
    with LocalMemoryAdapter(tmp_path / 'memory.sqlite3') as memory:
        import_letters({'schema_version': 'olivia.letters.v1', 'letters': rows}, adapter=memory)
        assert estimate_dates(memory.list_legacy()) == {}
        match, = note(lookup(adapter_for(memory), FAREWELL)[0])['matches']
    assert match['from'].startswith('你（林离）')
    assert match['when'] == '早于2026年9月10日（原信没写日期，那天的信里已经抄过它）'
    assert match['before']['opening'] == '再见' and match['before']['when'].startswith('早于2026年9月10日')
    with LocalMemoryAdapter(tmp_path / 'alone.sqlite3') as memory:
        import_letters({'schema_version': 'olivia.letters.v1', 'letters': rows[1:]}, adapter=memory)
        match, = note(lookup(adapter_for(memory), FAREWELL)[0])['matches']
    assert match['when'] == '很久以前（原信没写日期）'
