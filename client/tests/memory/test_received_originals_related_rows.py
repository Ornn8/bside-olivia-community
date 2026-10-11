"""Projecting only the rows that share an original gives the same receipts as projecting the archive."""
from datetime import datetime, timedelta, timezone
import random

import pytest

from runtime.memory.received_user_originals import received_originals, rows_sharing_originals


BASE = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)


def random_rows(rng, count):
    """Letters and chats with overlapping originals, failed letters, proactive and malformed rows."""
    rows = []
    for index in range(count):
        when = (BASE + timedelta(minutes=rng.randint(0, 600))).isoformat()
        kind = rng.random()
        if kind < 0.3:
            rows.append({'letter_id': f'L{rng.randint(0, count // 2)}', 'content': rng.choice(['你好', '晚安', '在吗']),
                         'life_received_at': when, 'reply_revision': rng.randint(1, 2),
                         'letter_status': rng.choice(['COMPLETED', 'FAILED']), 'reply_text': '回复'})
        elif kind < 0.9:
            ids = [f'm{rng.randint(0, count)}' for _ in range(rng.randint(1, 3))]
            texts = {key: rng.choice(['累了', '想你', '早安']) if rng.random() < 0.9 else '改过的话' for key in ids}
            rows.append({'letter_id': f'C{index}', 'channel': rng.choice(['qq', 'qq', 'wechat']),
                         'binding_id': rng.choice(['a', 'b']), 'source_messages': texts,
                         'content': '\n'.join(texts.values()), 'life_received_at': when,
                         'delivery_status': rng.choice(['DELIVERED', 'FAILED'])})
        elif kind < 0.93:
            rows.append({'letter_id': f'P{index}', 'origin': 'proactive', 'content': '主动', 'life_received_at': when})
        elif kind < 0.96:
            rows.append({'letter_id': '', 'content': '没有编号', 'life_received_at': when})
        elif kind < 0.98:
            rows.append('not a row')
        else:
            rows.append({'letter_id': f'R{index}', 'channel': 'fax', 'content': '未知渠道', 'life_received_at': when})
    return rows


@pytest.mark.parametrize('seed', range(40))
def test_related_rows_give_the_same_receipts_as_the_whole_archive(seed):
    rng = random.Random(seed)
    rows = random_rows(rng, 120)
    for _ in range(10):
        anchor = rng.choice([row for row in rows if isinstance(row, dict) and row.get('letter_id')])
        current = [row for row in rows if isinstance(row, dict) and row.get('letter_id') == anchor['letter_id']]
        for include_undelivered in (True, False):
            wanted = {item.source_id for item in received_originals(current, include_undelivered=True)}
            everything = sorted((item for item in received_originals(rows, include_undelivered=include_undelivered)
                                 if item.source_id in wanted), key=lambda item: item.source_id)
            related = rows_sharing_originals(rows, current)
            narrowed = sorted((item for item in received_originals(related, include_undelivered=include_undelivered)
                               if item.source_id in wanted), key=lambda item: item.source_id)
            assert narrowed == everything


def test_a_message_first_received_alone_keeps_its_earlier_receipt_time_when_merged_later():
    early, late = BASE - timedelta(minutes=1), BASE
    first = {'letter_id': 'first', 'channel': 'qq', 'binding_id': 'acct', 'source_messages': {'m1': '请慢慢来'},
             'content': '请慢慢来', 'life_received_at': early.isoformat(), 'delivery_status': 'FAILED'}
    merged = {**first, 'letter_id': 'merged', 'source_messages': {'m1': '请慢慢来', 'm2': '不用急'},
              'content': '请慢慢来\n不用急', 'life_received_at': late.isoformat()}
    bystanders = [{**first, 'letter_id': f'other-{index}', 'source_messages': {f'x{index}': '无关'}} for index in range(500)]
    rows = [first, *bystanders, merged]
    related = rows_sharing_originals(rows, [merged])
    assert related == [first, merged]  # the earlier row carries m1's immutable receipt time
    receipts = {item.user_message: item.occurred_at for item in received_originals(related, include_undelivered=True)}
    assert receipts == {'请慢慢来': early, '不用急': late}
    # Projecting the current row alone would restamp m1, which the emotion ledger refuses as a conflict.
    alone = {item.user_message: item.occurred_at for item in received_originals([merged], include_undelivered=True)}
    assert alone['请慢慢来'] == late != early


def test_nothing_is_related_to_an_anchor_without_originals():
    assert rows_sharing_originals([{'letter_id': 'a', 'content': 'x'}], [{'channel': 'qq'}]) == []
    assert rows_sharing_originals([], [{'letter_id': 'a'}]) == []
