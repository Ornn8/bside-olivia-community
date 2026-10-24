"""Her answer to what he bought goes out between his messages, not after he stops writing (owner 2026-10-23)."""
import asyncio

from runtime.personal_chat.events import PersonalMessage
from runtime.personal_chat.service import PersonalChatService


def contact(purpose='gift_goods'):
    return {'contact_id': 'contact:00000000-0000-4000-8000-000000000001', 'contact_purpose': purpose,
            'contact_format': 'text', 'contact_media': [], 'contact_brief': 'he bought her a winter coat'}


def service(rows, generate, sent):
    async def commit(row):
        pass
    return PersonalChatService(rows, lambda: None, generate, commit, {'qq': ('100', '200')})


def sender(sent):
    async def send(text):
        sent.append(text)
        return 'ack'
    return send


def test_a_message_from_him_does_not_cancel_the_answer_to_his_purchase():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        rows, sent = [], []
        async def generate(event, row):
            if row.get('origin') == 'proactive':
                started.set()
                await release.wait()
                return 'thank you for the coat'
            return 'user reply'
        chat = service(rows, generate, sent)
        gift = asyncio.create_task(chat.proactive(PersonalMessage('qq', '100', '200', 'contact-1', ''), sender(sent),
                                                  contact=contact()))
        await started.wait()
        reply = asyncio.create_task(chat.handle(PersonalMessage('qq', '100', '200', 'u', 'hello'), sender(sent)))
        await asyncio.sleep(.05)
        release.set()
        await asyncio.wait_for(asyncio.gather(gift, reply), 2)
        assert rows[0]['delivery_status'] == 'DELIVERED' and rows[0]['contact_purpose'] == 'gift_goods'
        assert sent == ['thank you for the coat', 'user reply']
    asyncio.run(scenario())


def test_her_own_initiative_still_gives_way_to_his_message():
    async def scenario():
        started = asyncio.Event()
        rows, sent = [], []
        async def generate(event, row):
            if row.get('origin') == 'proactive':
                started.set()
                await asyncio.Event().wait()
            return 'user reply'
        chat = service(rows, generate, sent)
        recall = asyncio.create_task(chat.proactive(PersonalMessage('qq', '100', '200', 'contact-1', ''), sender(sent),
                                                    contact=contact('recall')))
        await started.wait()
        await asyncio.wait_for(chat.handle(PersonalMessage('qq', '100', '200', 'u', 'hello'), sender(sent)), .5)
        await recall
        assert rows[0]['delivery_status'] == 'SKIPPED' and sent == ['user reply']
    asyncio.run(scenario())


def run_after_his_turn(purpose, *, unanswered=False):
    """The contact waits while his turn runs; meanwhile more of his words arrive and are answered with it."""
    async def scenario():
        rows, sent = [], []
        async def generate(event, row):
            return 'thank you for the coat'
        chat = service(rows, generate, sent)
        await chat.batch_lock.acquire()
        task = asyncio.create_task(chat.proactive(PersonalMessage('qq', '100', '200', 'contact-1', ''), sender(sent),
                                                  contact=contact(purpose)))
        await asyncio.sleep(0)
        chat.user_revision += 1
        if unanswered:
            rows.append({'letter_id': 'u', 'binding_id': PersonalMessage('qq', '100', '200', '', '').binding_id,
                         'source_messages': {'u': 'one more thing'}, 'channel': 'qq', 'content': 'one more thing',
                         'delivery_status': 'RECEIVED'})
        chat.batch_lock.release()
        await asyncio.wait_for(task, 2)
        return sent
    return asyncio.run(scenario())


def test_the_answer_to_his_purchase_goes_out_once_his_messages_are_answered():
    assert run_after_his_turn('gift_goods') == ['thank you for the coat']


def test_a_message_of_his_still_waiting_for_a_reply_goes_first():
    assert run_after_his_turn('gift_goods', unanswered=True) == []


def test_other_contacts_still_wait_for_him_to_stop_writing():
    assert run_after_his_turn('recall') == []
