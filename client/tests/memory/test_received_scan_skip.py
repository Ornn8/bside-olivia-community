"""A memory scan writes only received messages the index does not hold yet (2026-10-22: every scan rewrote every
message the user ever sent, so heavy accounts waited tens of seconds for memory before each reply)."""
from runtime.memory.received_user_originals import index_received_rows
from runtime.memory.source_retrieval import SourceRetrieval


class Adapter:
    enabled = True

    def __init__(self, index, state=True):
        self.index, self.writes = index, []
        if state:
            self.received_index_state = lambda *, user_id: index.received_state(user_id)

    def index_received_user(self, *, user_id, source_id, user_message, occurred_at, exchange_sources=()):
        self.writes.append(source_id)
        result = self.index.put_received(user_id, source_id, user_message, occurred_at)
        for exchange in exchange_sources:
            self.index.alias_received(user_id, exchange, (source_id,))
        return result


def chat(i, messages, revision=1):
    return {'letter_id': f'chat-{i}', 'channel': 'qq', 'binding_id': 'b1', 'created_at': 1790000000 + 60 * i,
            'source_messages': messages, 'letter_status': 'COMPLETED', 'reply_revision': revision}


def test_an_unchanged_history_is_not_written_again_and_new_messages_are(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.sqlite3')
    rows = [chat(i, {f'm{i}': f'第{i}句'}) for i in range(5)]
    first = Adapter(index)
    assert len(index_received_rows(first, rows, user_id='local-user')) == 5 and len(first.writes) == 5
    again = Adapter(index)
    assert index_received_rows(again, rows, user_id='local-user') == () and again.writes == []
    rows.append(chat(9, {'m9': '刚到家'}))
    rows[0] = chat(0, {'m0': '第0句'}, revision=2)         # the same message now belongs to a new exchange too
    later = Adapter(index)
    index_received_rows(later, rows, user_id='local-user')
    assert len(later.writes) == 2
    texts, aliases = index.received_state('local-user')
    assert len(texts) == 6 and sum(exchange.startswith('reply:chat-0:') for exchange, _ in aliases) == 2


def test_without_a_readable_index_state_every_message_is_written_as_before(tmp_path):
    index = SourceRetrieval(tmp_path / 'index.sqlite3')
    rows = [chat(i, {f'm{i}': f'第{i}句'}) for i in range(3)]
    index_received_rows(Adapter(index), rows, user_id='local-user')
    plain = Adapter(index, state=False)
    index_received_rows(plain, rows, user_id='local-user')
    assert len(plain.writes) == 3
    broken = Adapter(index)

    def unreadable(*, user_id):
        raise OSError('locked')
    broken.received_index_state = unreadable
    index_received_rows(broken, rows, user_id='local-user')
    assert len(broken.writes) == 3
