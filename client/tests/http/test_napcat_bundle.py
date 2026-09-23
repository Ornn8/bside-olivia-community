import hashlib
import zipfile
import json
import pytest
from runtime.personal_chat import napcat_bundle as bundle, napcat_installer as installer, napcat_dependencies as deps


@pytest.mark.parametrize('url,digest', [
    ('https://evil.invalid/Olivia-QQ-v4.18.28-full.zip', bundle.BUNDLE_SHA256),
    ('https://' + bundle.R2_HOST + '/Olivia-QQ-v4.18.28-full.zip', '0' * 64),
])
def test_ticket_rejects_wrong_host_or_digest(monkeypatch, url, digest):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return bundle.TICKET_URL
        def read(self, limit):
            return json.dumps({'url': url, 'sha256': digest, 'size_bytes': bundle.BUNDLE_SIZE}).encode()
    monkeypatch.setattr(bundle.urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(installer.NapCatSetupError, match='NAPCAT_SOURCE_INVALID'):
        bundle._ticket()


def test_r2_bundle_retries_ticket_and_extracts_only_verified_files(tmp_path, monkeypatch):
    upstream = tmp_path / 'upstream.zip'
    payloads = {installer.NAPCAT_ASSET: b'napcat', 'qq-9.9.31.exe': b'qq', '7zr-26.03.exe': b'7z'}
    with zipfile.ZipFile(upstream, 'w') as z:
        for name, value in payloads.items(): z.writestr(name, value)
        z.writestr('../untrusted', b'ignored')
    digest = lambda value: hashlib.sha256(value).hexdigest()
    monkeypatch.setattr(bundle, 'BUNDLE_SHA256', digest(upstream.read_bytes()))
    monkeypatch.setattr(bundle, 'BUNDLE_SIZE', upstream.stat().st_size)
    monkeypatch.setattr(installer, 'NAPCAT_SHA256', digest(b'napcat'))
    monkeypatch.setattr(deps, 'QQ_SHA256', digest(b'qq'))
    monkeypatch.setattr(deps, 'EXTRACTOR_SHA256', digest(b'7z'))
    tickets=[]
    def ticket():
        tickets.append(1)
        return 'https://example.invalid/signed'
    monkeypatch.setattr(bundle, '_ticket', ticket)
    def download(path, **kwargs):
        if len(tickets)==1: raise installer.NapCatSetupError('NAPCAT_DOWNLOAD_FAILED')
        path.write_bytes(upstream.read_bytes())
    monkeypatch.setattr(installer, '_download_archive', download)
    bundle.ensure_bundle(tmp_path / 'data')
    bundle.ensure_bundle(tmp_path / 'data')
    assert len(tickets)==2
    root=installer._root(tmp_path/'data')
    assert (root/installer.NAPCAT_ASSET).read_bytes()==b'napcat'
    assert (root/'dependencies/qq-9.9.31.exe').read_bytes()==b'qq'
    assert not (root/'untrusted').exists()
