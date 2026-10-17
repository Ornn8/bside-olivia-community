from runtime.personal_chat.context import freeze_read_window


def test_old_pending_and_skipped_messages_do_not_join_current_turn():
    rows = [dict(letter_id=name, channel='qq', binding_id='same', created_at=stamp,
                 delivery_status=status, content=name, reply_text='unsent-' + name)
            for name, stamp, status in [('sept24', 1, 'FAILED'), ('oct15', 1800000, 'GENERATED'),
                                         ('skipped', 2000000, 'SKIPPED'), ('current', 2000001, 'GENERATING')]]
    assert freeze_read_window(rows, channel='qq', binding_id='same', current_id='current') == ()
