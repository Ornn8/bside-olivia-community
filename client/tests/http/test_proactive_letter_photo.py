"""A letter she starts herself may carry a photo; the writer is told first, and the letter never claims one that is absent."""
import asyncio
from datetime import datetime, timezone

import pytest

from runtime import proactive_config
from runtime.image_reply import proactive_photo as requested_by_letter, secondary_photo_allowed
from runtime.reply import proactive_photo
from tests.http.test_expression_context_routes import run_isolated
from tests.http.test_proactive_expression_context import SETUP

NOW = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_config():
    proactive_config.configure(None)
    yield
    proactive_config.configure(None)


class Port:
    def __init__(self, answer='yes', fail=False):
        self.answer, self.fail, self.asked = answer, fail, []

    async def ask(self, state, questions, *, purpose):
        self.asked.append((state, questions, purpose))
        if self.fail:
            raise RuntimeError('JEV_UNAVAILABLE')
        return {'photo': self.answer}


def state(**changes):
    value = dict(kind='life_share', now=NOW, world={'stale': False, 'current': {
        'activity': '慢练', 'location': '琴房', 'source_id': 'day:private'}}, previous_letter='上一封', previous_user_letter='他的来信',
        unanswered=0)
    return proactive_photo.question_state(**{**value, **changes})


# --- the question

def test_the_question_is_contrastive_and_carries_no_keyword_lists():
    criteria = proactive_photo.QUESTIONS['photo']['criteria']
    assert set(criteria) == {'yes', 'no'}
    for item in criteria.values():
        assert set(item) == {'what', 'not_for', 'examples'} and item['examples']


def test_the_question_sees_her_present_moment_and_how_the_last_letters_went():
    value = state(unanswered=2)
    assert value['her_present_moment'] == {'activity': '慢练', 'location': '琴房'}  # Only the fields the question needs.
    assert value['letters_since_his_last_word'] == 2 and value['her_previous_letter'] == '上一封'
    assert state(world={'stale': True, 'current': {'activity': '旧的'}})['her_present_moment'] is None
    assert state(world=None)['her_present_moment'] is None
    assert len(state(previous_letter='长' * 5000)['her_previous_letter']) == proactive_photo.STATE_TEXT_LIMIT


def test_a_yes_from_the_cloud_is_the_only_way_to_a_photo():
    async def scenario():
        yes, no = Port('yes'), Port('no')
        assert await proactive_photo.decide(yes, state()) is True
        assert yes.asked[0][2] == proactive_photo.PURPOSE
        assert await proactive_photo.decide(no, state()) is False
        assert await proactive_photo.decide(Port(fail=True), state()) is False  # Not asking is not a photo.
        assert await proactive_photo.decide(None, state()) is False
    asyncio.run(scenario())


# --- what the writer is told

def instruction(**kwargs):
    import local_server
    return local_server._proactive_instruction({'kind': 'life_share', 'relationship_tier': 'trusted'}, planning=False, **kwargs)


def test_the_writer_is_told_whether_a_photo_goes_with_the_letter():
    with_photo, without = instruction(photo=True), instruction(photo=False)
    assert proactive_photo.writer_note(True) in with_photo and proactive_photo.writer_note(False) not in with_photo
    assert proactive_photo.writer_note(False) in without and proactive_photo.writer_note(True) not in without
    assert proactive_photo.WRITER_NOTES[True] != proactive_photo.WRITER_NOTES[False]
    plain = instruction()
    assert all(note not in plain for note in proactive_photo.WRITER_NOTES.values())


def test_a_letter_written_with_a_photo_asks_the_planner_for_one_and_the_cloud_can_switch_it_off():
    own = {'origin': 'proactive', 'letter_id': 'a', 'proactive_photo': True}
    assert requested_by_letter(own) and not requested_by_letter({**own, 'proactive_photo': False})
    assert secondary_photo_allowed(own)
    assert not secondary_photo_allowed({'origin': 'proactive', 'channel': 'qq', 'letter_id': 'q'})  # QQ stays voice only.
    proactive_config.adopt({'version': 'v', 'rules': {'initiative_photo': False}})
    assert not secondary_photo_allowed(own)


# --- the letter

PUBLISH = SETUP + r'''
import os
from runtime import image_reply, cloud_contact
os.environ['OLIVIA_GPU_API_URL'] = 'https://gpu.invalid'
os.environ['OLIVIA_GPU_API_KEY'] = 'synthetic'
server.video_reply_settings_store = SimpleNamespace(image_snapshot=lambda: {'enabled': image_on})
class Port:
    async def ask(self, state, questions, *, purpose):
        asked.append((state, purpose))
        return {'photo': answer}
import runtime.reply.jev_questions as jev_questions
jev_questions.configured_questions = lambda: Port()
asked, scheduled, bodies = [], [], []
def photo_notes(messages):
    from runtime.reply.proactive_photo import WRITER_NOTES
    text = '\n'.join(m['content'] for m in messages)
    return tuple(flag for flag, note in WRITER_NOTES.items() if note in text)
async def writer(messages, *, request_id, scope):
    if request_id.endswith(':plan'):
        return SimpleNamespace(text='{"decision":"send","format":"text","title":"问候"}')
    if request_id.endswith(':title') or request_id.endswith(':title2'):
        return SimpleNamespace(text='{"title":"琴房的下午"}')
    bodies.append(photo_notes(messages))
    server.letters_adapter._now = lambda: NOW
    return SimpleNamespace(text=('给你拍了一张琴房的照片。' if len(bodies) == 1 and answer == 'yes' else '今天在琴房慢慢练。') + '\n[[signature:林离]]')
server.letters_adapter.gateway = SimpleNamespace(complete_scoped=writer, timeout_seconds_for_scope=lambda *a, **kw: 10)
async def no_wait(done, timeout, step=1.0):
    waited = 0
    while not done() and waited < timeout:
        await asyncio.sleep(0.01)
        waited += 0.01
cloud_contact.wait_until = no_wait
server.PROACTIVE_PHOTO_WAIT_SECONDS = 1
def make_photo(row):
    row.update(image_status='COMPLETED', prepared_image='/synthetic/photo.png',
               reply_image_url='http://127.0.0.1:1/toy/media/photo.png', image_plan={'attach': True})
def fake_schedule(owner, row):
    scheduled.append(dict(row))
    async def run():
        await asyncio.sleep(0.02)
        if outcome == 'made':
            make_photo(row)
        elif outcome == 'skipped':
            row['image_status'] = 'SKIPPED'
        elif outcome == 'failed':
            row['image_status'] = 'FAILED'
        else:
            await asyncio.sleep(60)  # never finishes
    task = asyncio.ensure_future(run())
    image_reply._jobs[row['letter_id']] = task
image_reply.schedule = fake_schedule
async def publish():
    turn = await server._prepare_proactive_turn(intent, now=NOW)
    plan = json.loads(await server._proactive_complete(intent, planning=True, turn=turn))
    await server._publish_proactive(intent, plan, turn=turn)
    return server.store.letters[-1]
'''


def run(tmp_path, answer='yes', image_on=True, outcome='made', check=''):
    run_isolated(tmp_path, PUBLISH + f'\nanswer, image_on, outcome = {answer!r}, {image_on!r}, {outcome!r}\n'
                 + 'letter = asyncio.run(publish())\n' + check)


def test_a_letter_she_writes_knowing_a_photo_goes_with_it_is_published_with_the_photo(tmp_path):
    run(tmp_path, check=r'''
assert letter['letter_status'] == 'COMPLETED' and letter['reply_text'] == '给你拍了一张琴房的照片。'
assert bodies == [(True,)] and len(asked) == 1 and asked[0][1] == 'proactive-letter-photo'
assert letter['proactive_photo'] is True and letter['image_status'] == 'COMPLETED'
assert letter['reply_image_url'].endswith('/photo.png') and letter['image_reply_settings'] == {'enabled': True}
# The planner was asked for the photo while the letter was still held back, on the saved view of the moment.
assert len(scheduled) == 1 and scheduled[0]['letter_status'] == 'PROCESSING' and scheduled[0]['proactive_photo'] is True
assert checked_expression_context(scheduled[0]) is not None
assert asked[0][0]['her_present_moment'] == {'activity': '慢练', 'location': '琴房'}
''')


@pytest.mark.parametrize('outcome', ['skipped', 'failed', 'never'])
def test_a_photo_that_does_not_come_leaves_a_letter_written_again_without_any_claim_of_one(tmp_path, outcome):
    run(tmp_path, outcome=outcome, check=r'''
assert letter['letter_status'] == 'COMPLETED'
assert bodies == [(True,), (False,)]  # Told of a photo first, then written again told there is none.
assert letter['reply_text'] == '今天在琴房慢慢练。' and '照片' not in letter['reply_text']
assert letter['proactive_photo'] is False and letter['image_status'] == 'SKIPPED'
assert not any(key in letter for key in ('prepared_image', 'reply_image_url', 'image_plan'))
assert checked_expression_context(letter) is not None   # The saved view is bound to the text that was published.
assert letter['reply_revision'] == 1 and letter['private_world_reply_sha256'] == hashlib.sha256(letter['reply_text'].encode()).hexdigest()
if outcome == 'never':
    assert image_reply._jobs[letter['letter_id']].cancelled() or image_reply._jobs[letter['letter_id']].done()
''')


def test_without_a_yes_the_letter_is_told_there_is_no_photo_and_nothing_is_planned(tmp_path):
    run(tmp_path, answer='no', check=r'''
assert bodies == [(False,)] and letter['letter_status'] == 'COMPLETED' and not scheduled
assert 'proactive_photo' not in letter and 'image_status' not in letter and 'image_reply_settings' not in letter
''')


def test_with_photos_switched_off_the_cloud_is_not_asked_and_the_letter_is_told_there_is_none(tmp_path):
    run(tmp_path, image_on=False, check=r'''
assert not asked and bodies == [(False,)] and not scheduled and 'proactive_photo' not in letter
''')


def test_when_the_cloud_has_switched_proactive_photos_off_nothing_is_asked_or_made(tmp_path):
    run_isolated(tmp_path, PUBLISH + r'''
answer, image_on, outcome = 'yes', True, 'made'
from runtime import proactive_config
proactive_config.adopt({'version': 'v', 'rules': {'initiative_photo': False}})
letter = asyncio.run(publish())
assert not asked and bodies == [(False,)] and not scheduled and letter['letter_status'] == 'COMPLETED'
''')
