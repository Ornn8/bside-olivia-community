"""Browser interaction check for the capability-gated native card enhancement."""
import json
import os
from pathlib import Path

import pytest

from runtime.native_piano_ui import JAVASCRIPT


def test_native_audio_panel_hides_when_disabled_and_uploads_midi(tmp_path, monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    edge = Path(os.environ.get('ProgramFiles(x86)', '')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.is_file():
        pytest.skip('A local browser is required for native panel interaction evidence')
    monkeypatch.setenv('TEMP', str(tmp_path))
    monkeypatch.setenv('TMP', str(tmp_path))
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={'width': 1100, 'height': 850})
        submitted = []
        enabled = [False]
        def respond(route):
            path = route.request.url.split('http://127.0.0.1:8765')[-1]
            if path == '/toy/midi/capabilities':
                data = {'enabled': enabled[0], 'max_seconds': 120, 'max_midi_bytes': 524288,'price_ranges_cents':{'midi':[7,13],'original':[11,29]},'pieces':[{'title':'Evening'}]}
            elif path.startswith('/toy/midi/listJobs'):
                data = {'list': [{'jobId': 'abc', 'filename': 'evening.midi', 'state': 3,
                                  'description': '给你弹了《Evening》的开头。',
                                  'info': {'audioUrl': 'http://127.0.0.1:8765/toy/midi/audio/abc.mp3'}}]}
            elif path == '/toy/genObjectUploadUrl':
                data = {'url': 'http://127.0.0.1:8765/toy/midi/upload/token', 'key': 'token', 'headers': {}}
            elif path == '/toy/midi/upload/token':
                assert route.request.method == 'PUT'
                route.fulfill(status=204);return
            elif path == '/toy/midi/generate':
                submitted.append(route.request.post_data_json);data = {'jobId': 'abc'}
            else:
                route.fulfill(status=404);return
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'code': 0, 'data': data}))
        page.route('http://127.0.0.1:8765/**', respond)
        page.clock.install()
        page.set_content('<script data-olivia-companion-settings data-api-base="http://127.0.0.1:8765/"></script><main style="max-width:720px;margin:80px auto"><h1>Music</h1><div class="midi-upload-card-large">Native MIDI</div></main>')
        page.add_script_tag(content=JAVASCRIPT)
        page.wait_for_timeout(150)
        assert not page.locator('.midi-upload-card-large').is_visible()
        assert not page.locator('.piano-entry').is_visible()
        enabled[0] = True
        # Trigger the real capability timer without waiting a minute.
        page.clock.fast_forward(61000)
        page.locator('.piano-entry').wait_for(state='visible')
        page.locator('.piano-entry').click()
        page.locator('audio').wait_for(state='attached')
        assert page.locator('audio').count() == 1
        assert page.get_by_text('下载 MP3', exact=True).get_attribute('href').endswith('?download=1')
        page.locator('input[type=file]').set_input_files({'name': 'evening.midi', 'mimeType': 'audio/midi', 'buffer': b'MThd\x00\x00\x00\x06'})
        page.get_by_label('演奏段落').select_option('中段')
        assert '0.07' in page.locator('dialog').inner_text()
        assert 'Evening' in page.get_by_label('TA 给我的曲子').inner_text()
        with page.expect_response('http://127.0.0.1:8765/toy/midi/generate'):
            page.get_by_text('生成 MP3', exact=True).click()
        assert submitted == [{'midiUrl': 'token', 'title': 'evening', 'seconds': 60, 'section':'中段'}]
        page.screenshot(path=str(tmp_path / 'native-piano-panel.png'))
        page.get_by_text('关闭', exact=True).click()
        assert page.locator('dialog').count() == 0
        browser.close()


def test_library_page_gets_a_piano_entry_next_to_local_import(tmp_path, monkeypatch):
    # The client never renders the native MIDI cards (their flag is off), so 2.3.0's entry was never visible.
    playwright = pytest.importorskip('playwright.sync_api')
    edge = Path(os.environ.get('ProgramFiles(x86)', '')) / 'Microsoft/Edge/Application/msedge.exe'
    if not edge.is_file():
        pytest.skip('A local browser is required for native panel interaction evidence')
    monkeypatch.setenv('TEMP', str(tmp_path))
    monkeypatch.setenv('TMP', str(tmp_path))
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={'width': 1100, 'height': 850})
        def respond(route):
            path = route.request.url.split('http://127.0.0.1:8765')[-1]
            if path == '/toy/midi/capabilities':
                data = {'enabled': True, 'max_seconds': 120, 'max_midi_bytes': 524288,
                        'price_ranges_cents': {'midi': [7, 13], 'original': [11, 29]}, 'pieces': []}
            elif path.startswith('/toy/midi/listJobs'):
                data = {'list': []}
            else:
                route.fulfill(status=404);return
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'code': 0, 'data': data}))
        page.route('http://127.0.0.1:8765/**', respond)
        page.goto('about:blank#/studio')
        page.set_content('<script data-olivia-companion-settings data-api-base="http://127.0.0.1:8765/"></script>'
                         '<nav data-olivia-main-navigation><button>曲库</button></nav>')
        page.add_script_tag(content=JAVASCRIPT)
        page.evaluate("window.location.hash='#/studio'")
        page.locator('[data-olivia-piano-entry]').wait_for(state='visible')
        # The local-import entry arrives later: the piano entry moves after it and takes its look.
        page.evaluate("""() => { const b = document.createElement('button'); b.textContent = '导入本地演奏';
            b.className = 'nav-pill'; b.dataset.oliviaLocalSongsEntry = '';
            document.querySelector('[data-olivia-main-navigation]').append(b); }""")
        page.wait_for_function("document.querySelector('[data-olivia-piano-entry]').previousElementSibling"
                               "?.hasAttribute('data-olivia-local-songs-entry')")
        assert page.locator('[data-olivia-piano-entry]').get_attribute('class') == 'nav-pill'
        assert page.locator('[data-olivia-main-navigation] button').all_inner_texts() == ['曲库', '导入本地演奏', '钢琴小样']
        page.locator('[data-olivia-piano-entry]').click()
        page.locator('dialog').wait_for(state='visible')
        page.get_by_text('关闭', exact=True).click()
        page.evaluate("window.location.hash='#/collection'; document.body.append(document.createElement('i'))")
        page.locator('[data-olivia-piano-entry]').wait_for(state='detached')
        browser.close()
