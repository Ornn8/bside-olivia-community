import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from runtime.improve.upload import anonymize, forget, pending, read_state, set_enabled, upload_once

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def chat(content, reply, at, **extra):
    return {'letter_id': at.isoformat(), 'channel': 'qq', 'content': content, 'reply_text': reply,
            'delivery_status': 'DELIVERED', 'life_received_at': at.isoformat(), **extra}


def test_anonymize_removes_contacts_numbers_and_links():
    text = ('我QQ号是 12345678，手机13812345678，邮箱 a.b@example.com，看 https://x.cn/p?q=1 ，'
            '身份证 11010119900307123X，微信 wxid_abcdef，@小明 你好')
    clean = anonymize(text)
    for secret in ('12345678', '13812345678', 'a.b@example.com', 'https://x.cn', '11010119900307123X', 'wxid_abcdef', '小明'):
        assert secret not in clean
    assert '[手机号]' in clean and '[邮箱]' in clean and '[链接]' in clean and '[证件号]' in clean


def test_nothing_is_pending_until_enabled_and_only_after_consent(tmp_path):
    rows = [chat('以前的话', '以前的回复', NOW - timedelta(days=1)),
            chat('之后的话 13812345678', '之后的回复', NOW + timedelta(minutes=5)),
            chat('没回复', '', NOW + timedelta(minutes=6)),
            chat('失败的', '回复', NOW + timedelta(minutes=7), delivery_status='FAILED')]
    assert pending(rows, read_state(tmp_path)) == []
    set_enabled(tmp_path, True, now=NOW)
    items = pending(rows, read_state(tmp_path))
    assert [item['user'] for _, item in items] == ['之后的话 [手机号]']
    assert items[0][1]['hour'] == '2026-10-08T12:00Z' and 'letter_id' not in items[0][1]


def test_upload_advances_and_withdraw_deletes_then_rotates_identity(tmp_path):
    rows = [chat(f'话{i}', f'回{i}', NOW + timedelta(minutes=i + 1)) for i in range(3)]
    set_enabled(tmp_path, True, now=NOW)
    device = read_state(tmp_path)['device_id']
    sent = []

    async def post(path, payload):
        sent.append((path, payload))

    assert asyncio.run(upload_once(tmp_path, rows, post, client_version='2.1.14')) == 3
    assert sent[0][0] == '/improve/exchanges' and sent[0][1]['device_id'] == device
    assert 'key' not in str(sent[0][1]).lower()
    assert asyncio.run(upload_once(tmp_path, rows, post, client_version='2.1.14')) == 0
    assert read_state(tmp_path)['uploaded'] == 3
    result = asyncio.run(forget(tmp_path, post))
    assert sent[-1] == ('/improve/forget', {'device_id': device})
    assert result['enabled'] is False and read_state(tmp_path)['device_id'] is None
    set_enabled(tmp_path, True, now=NOW + timedelta(hours=1))
    assert read_state(tmp_path)['device_id'] not in (None, device)


def test_failed_upload_keeps_the_batch_and_bad_setting_is_rejected(tmp_path):
    set_enabled(tmp_path, True, now=NOW)
    rows = [chat('话', '回', NOW + timedelta(minutes=1))]

    async def fail(path, payload):
        raise OSError('offline')

    with pytest.raises(OSError):
        asyncio.run(upload_once(tmp_path, rows, fail, client_version='x'))
    assert len(pending(rows, read_state(tmp_path))) == 1
    with pytest.raises(ValueError):
        set_enabled(tmp_path, 'yes')
