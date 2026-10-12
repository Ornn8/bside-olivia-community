import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from runtime.diary.diary import DiaryStore, write_day, write_month
from tests.memory.test_diary import DAY, NOW, ROWS, ENTRY


class Writer:
    config = SimpleNamespace(model='chosen-model')

    def __init__(self, revision=None):
        self.calls = []
        self.revision = revision

    async def complete_structured_scoped(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        value = ENTRY if len(self.calls) == 1 else self.revision
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(text=json.dumps(value))


def test_revision_preserves_facts_and_retries_without_calls(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    revised = {'title': '改好了', 'mood': '暖', 'body': '今天的具体画面。' * 35}
    writer = Writer(revised)
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert store.entry('2026-10-07')['body'] == revised['body']
    assert [call[1]['request_id'] for call in writer.calls] == ['diary:2026-10-07', 'diary-revise:2026-10-07']
    with store._db() as db:
        facts = json.loads(db.execute('SELECT facts FROM diary_entries').fetchone()[0])
    assert len(facts) == 2
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert len(writer.calls) == 2


@pytest.mark.parametrize('revision', [TimeoutError(), {}, {'title': 't', 'mood': 'm', 'body': 'x'},
                                      {**ENTRY, 'facts': []}])
def test_bad_revision_keeps_draft(tmp_path, revision):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer(revision)
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert store.entry('2026-10-07')['body'] == ENTRY['body']
    assert len(writer.calls) == 2


def test_final_save_failure_reuses_completed_revision(tmp_path, monkeypatch):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer({'title': 't', 'mood': 'm', 'body': '具体画面' * 70})
    save = store.save
    monkeypatch.setattr(store, 'save', lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError()))
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    monkeypatch.setattr(store, 'save', save)
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert len(writer.calls) == 2
    assert store.entry('2026-10-07')['body'] == writer.revision['body']


def test_memoir_uses_two_passes(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer({'title': 't', 'mood': 'm', 'body': '那时的具体画面' * 60})
    asyncio.run(write_month(store, writer, '2026-10', ROWS, persona='p', now=NOW))
    assert writer.calls[1][1]['request_id'] == 'diary-memoir-revise:2026-10'
    assert store.entry('2026-10')['body'] == writer.revision['body']


def test_style_rotation_and_length_tiers():
    from runtime.diary.diary import choose_style, length_range, STYLES
    recent = [{'style': style} for style in STYLES[:3]]
    assert choose_style('2026-10-07', recent, False) not in STYLES[:3]
    assert choose_style('2026-10-07', recent, False) == choose_style('2026-10-07', recent, False)
    assert choose_style('2026-10-07', recent, True) == 'short'
    assert length_range(10, []) == (60, 150)
    assert length_range(100, []) == (250, 600)
    assert length_range(100, [{'kind': 'photo'}]) == (250, 900)
    assert length_range(2000, []) == (250, 900)


def test_packet_budget_drops_low_priority_first():
    from runtime.diary.diary import fit_packet, MAX_PACKET_CHARS
    packet = {'messages': [{'user': 'm' * 18000, 'linli': ''}], 'sent': [{'title': 's' * 6000}],
              'day_life': ['l' * 6000], 'recent_entries': [{'body': 'r' * 6000}], 'persona': 'p' * 6000}
    result = fit_packet(packet)
    assert len(json.dumps(result, ensure_ascii=False)) <= MAX_PACKET_CHARS
    assert result['messages'] == packet['messages']
    assert not result['recent_entries']
    result = fit_packet(packet, limit=MAX_PACKET_CHARS)
    assert result['sent'] == packet['sent']
    assert not result['day_life'] and not result['recent_entries']


def test_delivered_photos_only_and_matching_fallback(tmp_path):
    from runtime.diary.diary import sent_material, choose_photo, local_photo
    one = tmp_path / 'one.png'; one.write_bytes(b'image')
    two = tmp_path / 'two.png'; two.write_bytes(b'image')
    rows = [dict(ROWS[0], image_delivery_status='DELIVERED', image_delivered_at=DAY.isoformat(),
                 image_description={'summary': '窗边的钢琴'}, prepared_image=str(one)),
            dict(ROWS[0], image_delivery_status='DELIVERED', image_delivered_at=NOW.isoformat(),
                 image_description={'summary': '明天'}, prepared_image=str(two)),
            dict(ROWS[0], image_delivery_status='PENDING', prepared_image=str(two))]
    sent, photos = sent_material(rows, '2026-10-07')
    assert len(sent) == len(photos) == 1
    photos.append({'path': str(two), 'description': '花园'})
    assert choose_photo(photos, '我坐在窗边的钢琴前') == str(one)
    assert choose_photo(photos, '毫无关联') == str(two)
    assert local_photo('https://example.com/a.png') is None
    assert local_photo(str(one)) == str(one)
    one.unlink()
    assert local_photo(str(one)) is None


def test_old_database_upgrades_without_losing_entry(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE diary_entries (day TEXT PRIMARY KEY, written_at TEXT NOT NULL, title TEXT NOT NULL, '
                   'mood TEXT NOT NULL, body TEXT NOT NULL, facts TEXT NOT NULL, model TEXT, short INTEGER NOT NULL DEFAULT 0, '
                   'seen INTEGER NOT NULL DEFAULT 0)')
        db.execute("INSERT INTO diary_entries VALUES ('2026-10-01','now','old','warm','body','[]','m',0,1)")
    store = DiaryStore(path)
    assert store.entry('2026-10-01')['body'] == 'body'
    assert store.entry('2026-10-01')['style'] == 'ordinary'
    assert store.entry('2026-10-01')['photo'] is None
    DiaryStore(path)


def test_life_timestamps_fields_and_limits(tmp_path):
    from runtime.diary.diary import day_life
    store = DiaryStore(tmp_path / 'life.db')
    with store._db() as db:
        db.execute('CREATE TABLE life_moments (occurred_at TEXT, kind TEXT, payload TEXT)')
        for _ in range(50):
            db.execute('INSERT INTO life_moments VALUES (?,?,?)', ('2026-10-07T02:00:00+00:00', 'daily',
                       json.dumps({'location': '窗边', 'activity': '练琴', 'note': '左手有点僵' * 30,
                                   'meals': [{'status': 'planned', 'food': '面条'}]})))
    life = day_life(store, '2026-10-07', '蓝色外套')
    assert len(life) == 40
    assert life[0]['time'] == '10:00' and life[0]['location'] == '窗边'
    assert life[0]['meals'] == 'planned:面条' and life[0]['outfit'] == '蓝色外套'
    assert all(sum(map(len, item.values())) <= 120 for item in life)


def test_interrupted_revision_reuses_draft(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    class Interrupted(Writer):
        async def complete_structured_scoped(self, messages, **kwargs):
            if self.calls:
                raise asyncio.CancelledError()
            return await super().complete_structured_scoped(messages, **kwargs)
    writer = Interrupted()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    restarted = DiaryStore(store.path)
    asyncio.run(write_day(restarted, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert len(writer.calls) == 1
    assert restarted.entry('2026-10-07')['body'] == ENTRY['body']


def test_optional_enrichment_failure_does_not_lose_day(tmp_path, monkeypatch):
    import runtime.diary.diary as diary
    store = DiaryStore(tmp_path / 'diary.db')
    def broken(*args, **kwargs):
        raise ValueError('unavailable')
    monkeypatch.setattr(diary, 'sent_material', broken)
    monkeypatch.setattr(store, 'recent', broken)
    writer = Writer(TimeoutError())
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert store.entry('2026-10-07')['body'] == ENTRY['body']


def test_sent_audio_video_and_piano_require_own_receipts():
    from runtime.diary.diary import sent_material
    base = dict(ROWS[0], speech_script={'title': '森林故事'}, speech_intent={'mode': 'story'},
                piano_meta={'title': '月光'}, speech_delivery_status='DELIVERED', piano_delivery_status='DELIVERED')
    pending = dict(base, speech_delivery_status='UNKNOWN', piano_delivery_status='READY')
    videos = [{'delivery_status': state, 'delivered_at': DAY.isoformat(), 'input': {'spoken_text': '琴练完啦'}}
              for state in ['DELIVERED', 'UNKNOWN', 'PENDING']]
    sent, _ = sent_material([base, pending], '2026-10-07', videos)
    assert [item['kind'] for item in sent] == ['story', 'piano', 'daily_video']
    assert sent[-1]['spoken_text'] == '琴练完啦'


def test_same_materials_model_and_total_budget_across_both_passes(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer({'title': 't', 'mood': 'm', 'body': '画面' * 150})
    rows = [dict(ROWS[0], content='消息' * 15000)]
    asyncio.run(write_day(store, writer, '2026-10-07', rows, persona='人设' * 9000, now=NOW))
    first, second = [json.loads(call[0][1]['content']) for call in writer.calls]
    draft = second.pop('draft')
    assert first == second and draft['body'] == ENTRY['body']
    from runtime.diary.diary import MAX_PACKET_CHARS
    assert all(len(call[0][1]['content']) <= MAX_PACKET_CHARS for call in writer.calls)
    with store._db() as db:
        assert db.execute('SELECT model FROM diary_entries').fetchone()[0] == 'chosen-model'


def test_diary_model_stays_selected_when_routes_change_between_passes(tmp_path, monkeypatch):
    from runtime import model_routes
    from original_client_relay_api import RELAY_BASE
    routes = {'diary': 'selected-diary'}
    monkeypatch.setattr(model_routes, 'load', lambda: routes)
    used = []
    class Routed(Writer):
        config = SimpleNamespace(model='main-model', base_url=RELAY_BASE)

        async def complete_structured_scoped(self, messages, **kwargs):
            used.append(model_routes.routed_model(RELAY_BASE, self.config.model))
            routes['diary'] = 'changed-by-user'
            return await super().complete_structured_scoped(messages, **kwargs)

    store = DiaryStore(tmp_path / 'diary.db')
    with model_routes.using('diary'):
        asyncio.run(write_day(store, Routed({}), '2026-10-07', ROWS, persona='p', now=NOW))
        assert model_routes.routed_model(RELAY_BASE, 'main-model') == 'changed-by-user'
    assert used == ['selected-diary', 'selected-diary']


def test_provider_failure_never_selects_fallback(tmp_path):
    primary = Writer({})
    class Other:
        primary = None
        async def complete_structured_scoped(self, *args, **kwargs):
            raise AssertionError('unexpected fallback')
    wrapper = Other(); wrapper.primary = primary
    store = DiaryStore(tmp_path / 'diary.db')
    asyncio.run(write_day(store, wrapper, '2026-10-07', ROWS, persona='p', now=NOW))
    assert len(primary.calls) == 2


def test_delete_during_revision_does_not_retain_work_or_recreate_entry(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    class DeleteWhileWriting(Writer):
        async def complete_structured_scoped(self, messages, **kwargs):
            if self.calls:
                store.delete('2026-10-07')
            return await super().complete_structured_scoped(messages, **kwargs)
    writer = DeleteWhileWriting({})
    assert asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW)) == 'skipped'
    assert store.entry('2026-10-07') is None and store.work('2026-10-07') == (None, None)


@pytest.mark.parametrize('failed_stage, calls', [('draft', 1), ('revising', 1), ('final', 2)])
def test_checkpoint_failure_still_saves_completed_entry(tmp_path, monkeypatch, failed_stage, calls):
    store = DiaryStore(tmp_path / 'diary.db')
    checkpoint = store.checkpoint
    def unreliable(day, stage, work):
        if stage == failed_stage:
            raise sqlite3.OperationalError('write failed')
        checkpoint(day, stage, work)
    monkeypatch.setattr(store, 'checkpoint', unreliable)
    writer = Writer({})
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    assert store.entry('2026-10-07')['body'] == ENTRY['body']
    assert len(writer.calls) == calls


def test_gifts_and_upcoming_facts_have_date_boundaries(tmp_path):
    from runtime.diary.diary import day_gifts
    store = DiaryStore(tmp_path / 'diary.db')
    store.remember_gifts([{'id': 'camera', 'name': '小相机'}], DAY)
    store.remember_pets([{'breed': 'cat', 'name': '小猫'}], DAY)
    wardrobe = {'wardrobe_styles': [{'label': '蓝', 'looks': [{'look_id': 'blue', 'label': '外套'}]}],
                'purchases': {'owned': ['blue']}}
    store.remember_wardrobe(wardrobe, DAY)
    assert {item['kind'] for item in day_gifts(store, '2026-10-07')} == {'gift', 'pet', 'clothes'}
    store.remember_wardrobe(wardrobe, NOW)
    assert day_gifts(store, '2026-10-08') == []
    fact = {'kind': 'plan', 'text': '明天见', 'date': '2026-10-08', 'quote': '明天见'}
    store.save('2026-10-06', {**ENTRY, 'facts': [fact]}, now=NOW, model='m', short=False)
    store.save('2026-10-09', {**ENTRY, 'facts': [{**fact, 'text': '后来才约好的'}]}, now=NOW, model='m', short=False)
    writer = Writer({})
    asyncio.run(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW))
    packet = json.loads(writer.calls[0][0][1]['content'])
    assert [fact['text'] for fact in packet['due_facts']] == ['明天见']


def test_outfit_remains_available_for_yesterdays_diary(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    state = {'wardrobe_styles': [{'label': '蓝', 'looks': [{'look_id': 'blue', 'label': '外套'}]}],
             'purchases': {'owned': ['blue']}, 'daily_outfit': {'date': '2026-10-07', 'look_id': 'blue'}}
    store.remember_wardrobe(state, DAY)
    store.remember_wardrobe({**state, 'daily_outfit': {'date': '2026-10-08', 'look_id': 'blue'}}, NOW)
    assert store.wardrobe(DAY)['today'] == '蓝·外套'


def test_concurrent_requests_share_one_draft_and_edit(tmp_path):
    store = DiaryStore(tmp_path / 'diary.db')
    writer = Writer({})
    async def together():
        return await asyncio.gather(*(write_day(store, writer, '2026-10-07', ROWS, persona='p', now=NOW)
                                      for _ in range(2)))
    assert sorted(asyncio.run(together())) == ['skipped', 'written']
    assert len(writer.calls) == 2
