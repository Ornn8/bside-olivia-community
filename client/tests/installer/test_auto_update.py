import asyncio
import base64
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from runtime import auto_update, ed25519

SEED = bytes(range(32))


def test_ed25519_matches_rfc8032():
    seed = bytes.fromhex('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60')
    public = ed25519.public_key(seed)
    assert public.hex() == 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
    signature = ed25519.sign(seed, b'')
    assert signature.hex().startswith('e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015')
    assert ed25519.verify(public, b'', signature) and not ed25519.verify(public, b'x', signature)


def bundle(version, manifest_digest='b' * 64):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        name = f'Olivia-{version}.oliviapatch'
        archive.writestr(name, b'patch')
        archive.writestr(name + '.sha256', hashlib.sha256(b'patch').hexdigest())
        archive.writestr(name + '.manifest.sha256', manifest_digest)
    return data.getvalue()


def signed(version, zip_bytes, **overrides):
    value = {'schema': 1, 'version': version, 'zip': f'Olivia-{version}.zip', 'bytes': len(zip_bytes),
             'sha256': hashlib.sha256(zip_bytes).hexdigest(), 'manifest_sha256': 'b' * 64,
             'notes': {'title': 'QQ 里她发照片时会先说句话。', 'items': [{'title': '发照片不再只有一张图', 'text': '她会先回你一句。'}]},
             **overrides}
    raw = json.dumps(value, ensure_ascii=False).encode()
    return raw, base64.b64encode(ed25519.sign(SEED, raw)).decode()


@pytest.fixture
def release(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_update, 'PUBLIC_KEY', ed25519.public_key(SEED))
    monkeypatch.setattr(auto_update, 'running_version', lambda: '2.3.9')
    monkeypatch.setattr(auto_update, '_state', {'state': 'idle', 'version': None, 'notes': None, 'checked_at': 0.0,
                                                'failures': {}, 'restarting': False})
    monkeypatch.delenv('OLIVIA_UPDATE_BASE', raising=False)
    (tmp_path / '.olivia-update-state.json').write_text('{}', encoding='utf-8')
    return tmp_path


def test_only_a_signed_manifest_is_believed(release):
    zip_bytes = bundle('2.4.0')
    raw, signature = signed('2.4.0', zip_bytes)
    assert auto_update.parse_manifest(raw, signature)['version'] == '2.4.0'
    with pytest.raises(ValueError, match='UPDATE_SIGNATURE_INVALID'):
        auto_update.parse_manifest(raw.replace(b'2.4.0', b'2.4.1', 1), signature)
    raw, signature = signed('2.4.0', zip_bytes, zip='evil.zip')
    with pytest.raises(ValueError, match='UPDATE_MANIFEST_INVALID'):
        auto_update.parse_manifest(raw, signature)


def test_versions_compare_as_numbers_and_dev_builds_never_update():
    assert auto_update.version_key('2.4.0') > auto_update.version_key('2.3.10') > auto_update.version_key('2.3.9')
    assert auto_update.version_key('2.2.8.2') > auto_update.version_key('2.2.8')
    assert auto_update.version_key('2.3.4+call.10') is None and auto_update.version_key(None) is None


class Response:
    def __init__(self, body, status):
        self.status, self.body = status, body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self, limit):
        return self.body[:limit]

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class Session:
    def __init__(self, files):
        self.files, self.urls = files, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, allow_redirects=False):
        self.urls.append(url)
        return Response(self.files.get(url, b''), 200 if url in self.files else 404)


def site(version, zip_bytes, **overrides):
    raw, signature = signed(version, zip_bytes, **overrides)
    base = auto_update.BASE
    return {base + 'update/latest.json': raw, base + 'update/latest.json.sig': signature.encode(),
            base + f'Olivia-{version}.zip': zip_bytes}


def test_a_newer_release_is_installed_in_the_background_and_waits_for_the_user(release, monkeypatch):
    zip_bytes = bundle('2.4.0')
    installed = []
    monkeypatch.setattr(auto_update, '_install', lambda root, path, manifest: installed.append((path.read_bytes(), manifest['version'])))
    session = Session(site('2.4.0', zip_bytes))
    state = asyncio.run(auto_update.check(release, session_factory=lambda: session))
    assert state['state'] == 'ready' and state['version'] == '2.4.0' and state['notes']['items'][0]['title'] == '发照片不再只有一张图'
    assert installed == [(zip_bytes, '2.4.0')]
    assert not list((release / 'data' / 'updates').glob('*.zip'))            # the download is not kept
    notes = json.loads((release / 'data' / 'updates' / 'notes.json').read_text(encoding='utf-8'))
    assert notes['version'] == '2.4.0' and notes['seen'] is False
    again = Session(site('2.4.0', zip_bytes))
    asyncio.run(auto_update.check(release, session_factory=lambda: again))
    assert len(installed) == 1 and len(again.urls) == 2                      # already waiting: nothing downloaded


def test_the_same_or_an_older_release_and_a_rejected_one_are_left_alone(release, monkeypatch):
    monkeypatch.setattr(auto_update, '_install', lambda *a: pytest.fail('must not install'))
    for version in ('2.3.9', '2.3.8'):
        session = Session(site(version, bundle(version)))
        assert asyncio.run(auto_update.check(release, session_factory=lambda: session))['state'] == 'idle'
    (release / '.olivia-update-probation.json').write_text(json.dumps({'version': '2.4.0', 'rejected': True}))
    session = Session(site('2.4.0', bundle('2.4.0')))
    assert asyncio.run(auto_update.check(release, session_factory=lambda: session))['state'] == 'idle'


def test_a_download_that_does_not_match_is_not_installed(release, monkeypatch):
    monkeypatch.setattr(auto_update, '_install', lambda *a: pytest.fail('must not install'))
    files = site('2.4.0', bundle('2.4.0'))
    files[auto_update.BASE + 'Olivia-2.4.0.zip'] = bundle('2.4.0', 'c' * 64)  # same size, other bytes
    with pytest.raises(RuntimeError, match='UPDATE_DOWNLOAD_INVALID'):
        asyncio.run(auto_update.check(release, session_factory=lambda: Session(files)))
    assert auto_update._state['failures'] == {'2.4.0': 1} and not list((release / 'data' / 'updates').iterdir())


def test_the_bundle_must_carry_the_signed_manifest_digest(tmp_path):
    path = tmp_path / 'Olivia-2.4.0.zip'
    path.write_bytes(bundle('2.4.0', 'c' * 64))
    with pytest.raises(RuntimeError, match='UPDATE_BUNDLE_INVALID'):
        auto_update._install(tmp_path, path, {'version': '2.4.0', 'manifest_sha256': 'b' * 64})


def test_idle_means_no_call_no_reply_in_progress_and_no_recent_chat():
    now = 1_800_000_000
    server = SimpleNamespace(_call_seen_at=0, store=SimpleNamespace(personal_chats=[
        {'created_at': now - 3600, 'delivery_status': 'DELIVERED'}]))
    assert auto_update.idle(server, now)
    server._call_seen_at = now - 60
    assert not auto_update.idle(server, now)
    server._call_seen_at = 0
    server.store.personal_chats.append({'created_at': now - 300, 'delivery_status': 'DELIVERED'})
    assert not auto_update.idle(server, now)
    server.store.personal_chats[-1] = {'created_at': now - 3000, 'delivery_status': 'PENDING'}
    assert not auto_update.idle(server, now)


def test_a_failed_first_start_goes_back_once(tmp_path):
    from installer.version_launcher import PROBATION_NAME, STATE_NAME, _probation_rollback
    new = {'version': '2.4.0', 'manifest_sha256': 'a' * 64, 'payload_path': 'versions/local_backend/2.4.0-' + 'a' * 64}
    old = {'version': '2.3.9', 'manifest_sha256': 'b' * 64, 'payload_path': 'versions/local_backend/2.3.9-' + 'b' * 64}
    (tmp_path / 'data' / 'logs').mkdir(parents=True)
    (tmp_path / STATE_NAME).write_text(json.dumps({'schema_version': 'olivia.update-state.v1',
        'active_components': {'local_backend': new}, 'previous_components': {'local_backend': old}}))
    (tmp_path / PROBATION_NAME).write_text(json.dumps({'version': '2.4.0'}))
    assert _probation_rollback(tmp_path)
    state = json.loads((tmp_path / STATE_NAME).read_text())
    assert state['active_components']['local_backend'] == old and state['previous_components']['local_backend'] == new
    assert json.loads((tmp_path / PROBATION_NAME).read_text())['rejected'] is True
    assert not _probation_rollback(tmp_path)                                   # never twice
    from installer.version_launcher import _rolled_back
    assert _rolled_back(tmp_path)                                              # no "restart failed" box then
    assert 'update_rolled_back' in (tmp_path / 'data' / 'logs' / 'launcher.jsonl').read_text()


class Server:
    @staticmethod
    def ok(data=None):
        return {'code': 200, 'data': data}

    @staticmethod
    def err(code, msg, data=None):
        return {'code': code, 'msg': msg, 'data': data}

    @staticmethod
    def CORS_HEADERS(request):
        return {}


def call(path, method='GET', confirmed=True):
    request = SimpleNamespace(path=path, method=method,
                              headers={'X-Olivia-Companion-Action': 'confirmed'} if confirmed else {})
    response = asyncio.run(auto_update.autoupdate_http(Server(), request))
    return response.status, json.loads(response.body)


def test_the_page_reads_the_state_and_marks_notes_seen(release, monkeypatch):
    monkeypatch.setenv('OLIVIA_INSTALL_ROOT', str(release))
    status, value = call('/toy/autoupdate/state')
    assert status == 200 and value['data']['state'] == 'idle' and value['data']['current'] == '2.3.9'
    assert call('/toy/autoupdate/restart', 'POST')[0] == 409
    assert call('/toy/autoupdate/restart', 'POST', confirmed=False)[0] == 403
    (release / 'data' / 'updates').mkdir(parents=True)
    (release / 'data' / 'updates' / 'notes.json').write_text(json.dumps({'version': '2.3.9', 'notes': {'title': 't'}, 'seen': False}))
    auto_update._settle_probation(release, '2.3.9')
    assert call('/toy/autoupdate/state')[1]['data']['state'] == 'updated'
    assert call('/toy/autoupdate/seen', 'POST')[0] == 200
    assert call('/toy/autoupdate/state')[1]['data']['state'] == 'idle'
    assert json.loads((release / 'data' / 'updates' / 'notes.json').read_text())['seen'] is True


def test_the_update_notice_is_in_the_injected_bootstrap():
    from pathlib import Path
    from installer.start_local import _frontend_patcher_files
    from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
    from runtime.update_ui import JAVASCRIPT
    assert JAVASCRIPT in BOOTSTRAP_JAVASCRIPT and '/toy/autoupdate/restart' in JAVASCRIPT and '现在重启更新' in JAVASCRIPT
    source = Path(__file__).resolve().parents[2]
    assert 'runtime/update_ui.py' in {p.relative_to(source).as_posix() for p in _frontend_patcher_files(source)}


ENGLISH_NOTES = {'title': 'Photos now come with a line first.', 'items': [{'title': 'More than a bare picture', 'text': 'She answers you first.'}]}


def test_english_release_notes_follow_the_interface_language(release, monkeypatch):
    from runtime import ui_language
    monkeypatch.setenv('OLIVIA_INSTALL_ROOT', str(release))
    zip_bytes = bundle('2.4.0')
    raw, signature = signed('2.4.0', zip_bytes, notes_en=ENGLISH_NOTES)
    manifest = auto_update.parse_manifest(raw, signature)
    assert manifest['notes_en'] == ENGLISH_NOTES and manifest['notes']['items'][0]['title'] == '发照片不再只有一张图'
    auto_update._state.update(state='ready', version='2.4.0', notes=manifest['notes'], notes_en=manifest['notes_en'])
    ui_language.reset()
    assert call('/toy/autoupdate/state')[1]['data']['notes']['items'][0]['title'] == '发照片不再只有一张图'
    ui_language.set_language('en')
    try:
        assert call('/toy/autoupdate/state')[1]['data']['notes'] == ENGLISH_NOTES
    finally:
        ui_language.reset()


def test_a_release_without_english_notes_keeps_showing_the_notes_it_has(release, monkeypatch):
    from runtime import ui_language
    monkeypatch.setenv('OLIVIA_INSTALL_ROOT', str(release))
    raw, signature = signed('2.4.0', bundle('2.4.0'))
    manifest = auto_update.parse_manifest(raw, signature)
    assert manifest['notes_en'] == {'title': '', 'items': []}
    auto_update._state.update(state='ready', version='2.4.0', notes=manifest['notes'], notes_en=manifest['notes_en'])
    ui_language.set_language('en')
    try:
        assert call('/toy/autoupdate/state')[1]['data']['notes'] == manifest['notes']
        # A state saved before this field existed has no English notes at all.
        auto_update._state.pop('notes_en')
        assert call('/toy/autoupdate/state')[1]['data']['notes'] == manifest['notes']
    finally:
        ui_language.reset()


def test_english_notes_are_kept_for_the_page_after_the_restart(release, monkeypatch):
    from runtime import ui_language
    monkeypatch.setenv('OLIVIA_INSTALL_ROOT', str(release))
    (release / 'data' / 'updates').mkdir(parents=True)
    (release / 'data' / 'updates' / 'notes.json').write_text(json.dumps(
        {'version': '2.3.9', 'notes': {'title': '中文', 'items': []}, 'notes_en': ENGLISH_NOTES, 'seen': False}), encoding='utf-8')
    auto_update._settle_probation(release, '2.3.9')
    ui_language.set_language('en')
    try:
        data = call('/toy/autoupdate/state')[1]['data']
        assert data['state'] == 'updated' and data['notes'] == ENGLISH_NOTES
        assert call('/toy/autoupdate/seen', 'POST')[0] == 200
        assert call('/toy/autoupdate/state')[1]['data']['notes'] is None
    finally:
        ui_language.reset()
