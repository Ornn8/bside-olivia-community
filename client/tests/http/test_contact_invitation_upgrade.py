import json
from types import SimpleNamespace

from runtime.personal_chat.contact_invitation import candidate, preview_configured, status


HIGH = SimpleNamespace(familiarity=70, trust=70, comfort=70, closeness=0)
LOW = SimpleNamespace(familiarity=70, trust=70, comfort=0, closeness=0)


def old_completed_letter():
    return {
        'letter_id': 'old-user-letter',
        'content': '升级前已经存在的正常交流',
        'created_at': 1000,
        'letter_status': 'COMPLETED',
    }


def test_three_high_dimensions_do_not_require_post_upgrade_marker():
    rows = [old_completed_letter()]
    assert 'contact_qualification' not in rows[0]
    assert status(rows, HIGH)['state'] == 'eligible'
    intent = candidate(rows, HIGH, 2000)
    assert intent is not None
    assert intent['kind'] == 'contact_invitation'
    assert intent['source_id'] == 'reply:old-user-letter:1'
    assert status(rows, LOW)['state'] == 'locked'


def test_proactive_opt_in_allows_invitation_before_transport_setup(tmp_path):
    root = tmp_path.resolve()
    settings = root / 'proactive' / 'settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'enabled': True}), encoding='utf-8')

    assert not (root / 'personal-chat' / 'config.json').exists()
    assert preview_configured(root, {}) is True


def test_disabled_proactive_contact_does_not_open_setup_gate(tmp_path):
    root = tmp_path.resolve()
    settings = root / 'proactive' / 'settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'enabled': False}), encoding='utf-8')

    assert preview_configured(root, {}) is False
