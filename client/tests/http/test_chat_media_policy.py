from types import SimpleNamespace

from runtime.letter_stickers.selection import allowed_stickers, split_selection


def test_extended_styles_are_qq_only():
    view = SimpleNamespace(familiarity='high', trust='high', comfort='high', closeness='high')
    for channel in ('letter', 'wechat'):
        allowed = allowed_stickers(view, channel=channel)
        assert len(allowed) == 108
        assert split_selection('正文\n[[sticker:linli-253]]', allowed) == ('正文', 'linli-01')
    assert len(allowed_stickers(view, channel='qq')) == 272


def test_voice_context_uses_actual_deliveries_for_this_binding_only():
    from runtime.personal_chat.presentation import recent_delivery_formats
    rows = [dict(channel='qq', binding_id='same', delivery_status='DELIVERED', delivered_format='text') for _ in range(8)]
    rows += [dict(channel='qq', binding_id='same', delivery_status='DELIVERED',
                  requested_format='voice', delivered_format='text', voice_fallback='TTS_FAILED'),
             dict(channel='qq', binding_id='same', delivery_status='DELIVERED', delivered_format='audio'),
             dict(channel='qq', binding_id='other', delivery_status='DELIVERED', delivered_format='audio'),
             dict(channel='wechat', binding_id='same', delivery_status='DELIVERED', delivered_format='audio'),
             dict(channel='qq', binding_id='same', delivery_status='FAILED', delivered_format='audio')]
    assert recent_delivery_formats(rows, channel='qq', binding_id='same') == ['text'] * 5 + ['voice']
