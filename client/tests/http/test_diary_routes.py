"""The diary page reads entries, takes confirmed comments and deletes; replies see the diary."""
from tests.http.test_expression_context_routes import run_isolated


def test_diary_routes_and_reply_context(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio, json
from datetime import datetime, timezone
from pathlib import Path
import local_server as server
from runtime.diary.diary import DiaryStore

store = DiaryStore(Path(server._os.environ['OLIVIA_LOCAL_DATA_ROOT']) / 'diary-route-test.sqlite3')
store.save('2026-10-07', {'title': '纪念日', 'mood': '开心', 'body': '你说8月26日是我们的纪念日。',
    'facts': [{'kind': 'anniversary', 'text': '8月26日是纪念日', 'date': '2026-08-26', 'quote': '纪念日'}]},
    now=datetime(2026, 10, 8, tzinfo=timezone.utc), model='m', short=False)
server.diary_store = store
server.letters_adapter.diary = store

async def call(method, path, body=None, query=None, confirmed=False):
    return await server.route(method, path, body, query or {}, companion_confirmed=confirmed)

listing = asyncio.run(call('GET', '/toy/diary'))
data = listing['data']
assert data['total'] == 1 and data['unseen'] == 1 and data['enabled'] is True
entry = asyncio.run(call('GET', '/toy/diary/entry', query={'day': '2026-10-07'}))['data']
assert entry['body'].startswith('你说8月26日') and store.page()['unseen'] == 0
assert asyncio.run(call('GET', '/toy/diary/entry', query={'day': '2026-01-01'}))['code'] == 404
denied = asyncio.run(call('POST', '/toy/diary/comment', {'day': '2026-10-07', 'text': '收到'}))
assert denied['code'] == 403
commented = asyncio.run(call('POST', '/toy/diary/comment', {'day': '2026-10-07', 'text': '收到'}, confirmed=True))
assert commented['data']['comments'][0]['text'] == '收到'
assert asyncio.run(call('POST', '/toy/diary/comment', {'day': '2026-10-07', 'text': ''}, confirmed=True))['code'] == 400
off = asyncio.run(call('POST', '/toy/diary/settings', {'enabled': False}, confirmed=True))
assert off['data']['enabled'] is False and not store.enabled()

estimate = asyncio.run(call('GET', '/toy/diary/memoir'))['data']
assert estimate['months'] == [] and estimate['running'] is False
bad = asyncio.run(call('POST', '/toy/diary/memoir/start', {'months': ['2020-01']}, confirmed=True))
assert bad['code'] == 400
assert asyncio.run(call('POST', '/toy/diary/memoir/start', {'months': ['2020-01']}))['code'] == 403

fragments = server.letters_adapter.recent_letter_fragments('还记得纪念日吗', now=datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
diary = [f for f in fragments if f.fragment_id == 'chat.diary']
assert diary and '8月26日是纪念日' in diary[0].text and '收到' in diary[0].text

assert asyncio.run(call('POST', '/toy/diary/delete', {'day': '2026-10-07'}, confirmed=True))['data']['deleted']
fragments = server.letters_adapter.recent_letter_fragments('还记得纪念日吗', now=datetime(2026, 10, 8, 12, tzinfo=timezone.utc))
assert not [f for f in fragments if f.fragment_id == 'chat.diary']
''')


def test_remembered_day_becomes_the_first_proactive_opportunity(tmp_path):
    run_isolated(tmp_path, r'''
from datetime import datetime, timezone
from pathlib import Path
import local_server as server
from runtime.diary.diary import DiaryStore, SHANGHAI
store = DiaryStore(Path(server._os.environ['OLIVIA_LOCAL_DATA_ROOT']) / 'diary-due.sqlite3')
today = datetime.now(SHANGHAI).date()
fact = {'kind': 'anniversary', 'text': '我们的纪念日', 'date': today.replace(year=today.year - 1).isoformat(), 'quote': 'q'}
store.save('2026-01-01', {'title': 't', 'mood': 'm', 'body': 'b', 'facts': [fact]},
           now=datetime(2026, 1, 2, tzinfo=timezone.utc), model='m', short=False)
server.diary_store = store
rows = [{'letter_id': 'a', 'content': '在吗', 'reply_text': '在呀', 'letter_status': 'COMPLETED',
         'life_received_at': '2026-10-01T10:00:00+08:00', 'created_at': 1790000000}]
items = server._diary_due_candidates(rows, {'initiative_profile': {'tier': 'close', 'caution': 'none'}})
assert len(items) == 1 and items[0]['kind'] == 'diary_due' and items[0]['source_id'] == 'reply:a:1'
assert items[0]['remembered']['text'] == '我们的纪念日' and items[0]['not_before'] < items[0]['expires_at']
rows.append({'origin': 'proactive', 'proactive_candidate_id': items[0]['id']})
assert server._diary_due_candidates(rows, {}) == []
''')


def test_diary_photo_is_local_media_only_and_missing_is_harmless(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import local_server as server
from runtime.diary.diary import DiaryStore
root = Path(server._os.environ['OLIVIA_LOCAL_DATA_ROOT'])
media = root / 'media'
media.mkdir(exist_ok=True)
photo = media / ('photo-' + 'a' * 32 + '.png')
photo.write_bytes(b'local fixture')
store = DiaryStore(root / 'diary-photo.sqlite3')
server.diary_store = store
store.save('2026-10-07', {'title':'t','mood':'m','body':'b','facts':[]},
    now=datetime.now(timezone.utc), model='m', short=True, photo=str(photo))
def read():
    return asyncio.run(server.route('GET','/toy/diary/entry',None,{'day':'2026-10-07'}))['data']
assert read()['photo'] == '/toy/media/' + photo.name
assert server._diary_public_entry({'photo':str(root / 'elsewhere.png')})['photo'] is None
photo.unlink()
assert read()['photo'] is None and read()['body'] == 'b'
''')


def test_rewrite_route_needs_confirmation_and_queues_the_day(tmp_path):
    run_isolated(tmp_path, r'''
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import local_server as server
from runtime.diary.diary import DiaryStore

store = DiaryStore(Path(server._os.environ['OLIVIA_LOCAL_DATA_ROOT']) / 'diary-rewrite-route.sqlite3')
store.save('2026-10-07', {'title': '写反了', 'mood': '平静', 'body': '他的视角。', 'facts': []},
           now=datetime(2026, 10, 8, tzinfo=timezone.utc), model='m', short=False)
server.diary_store = store
started = []

async def rewrite_now():
    started.append(store.rewrite_days())

server._diary_rewrite_now = rewrite_now

async def call(body, confirmed=True):
    result = await server.route('POST', '/toy/diary/rewrite', body, {}, companion_confirmed=confirmed)
    await asyncio.sleep(0)
    return result

assert asyncio.run(call({'day': '2026-10-07'}, confirmed=False))['code'] == 403
assert asyncio.run(call({'day': '2026-10-01'}))['code'] == 404
assert asyncio.run(call({'day': '2026-10-07', 'extra': 1}))['code'] == 400
assert store.entry('2026-10-07') is not None and started == []
assert asyncio.run(call({'day': '2026-10-07'}))['data'] == {'rewriting': '2026-10-07'}
assert store.entry('2026-10-07') is None and started == [['2026-10-07']]
''')
