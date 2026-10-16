"""Shipped UI: real fetch envelopes, escaping, route mutation and narrow layout."""
import json
import os
from pathlib import Path
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def payload():
    from runtime.relationship_policy import DEFAULTS, ROUTES
    return {'status':'READY', 'version':7, 'route':'romantic', 'routes':ROUTES, 'volume':4,
            'title':'信赖', 'description':DEFAULTS['volumes']['romantic']['4']['description'],
            'dynamic':'最近她更信任你了。',
            'dimensions':{k:{'text':v['medium'],'rising':k=='trust'} for k,v in DEFAULTS['dimensions'].items() if k!='tension'},
            'moments':[{'date':'2026-10-16','text':'你们一起完成了一件事','quote':'就叫它小小的晴天吧。'},
                       {'date':'2026-10-15','text':'你跟她说了心里话','quote':''},
                       {'date':'2026-10-14','text':'你认真地关心了她','quote':'别着急，弹给我听就好。'}],
            'plans':[{'title':'周末一起听新曲子','detail':'等你忙完，我们挑一首都喜欢的。'}],
            'addressing':{'linli_calls_user':'阿晴','user_calls_linli':'小离'},
            'hint':'聊聊那个还没完成的约定吧。'}


def test_relationship_panel_descriptions_and_small_screen(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    renderer = BOOTSTRAP_JAVASCRIPT.split('    const relationshipPanel = ', 1)[1].split('    const relationship = relationshipPanel();', 1)[0]
    reader = BOOTSTRAP_JAVASCRIPT.split('  const requestJson = ',1)[1].split('  const publishProactiveState',1)[0]
    screenshots = Path(os.environ.get('RELATIONSHIP_SCREENSHOTS', tmp_path))
    screenshots.mkdir(parents=True, exist_ok=True)
    data, requests = payload(), []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome')
        page = browser.new_page(viewport={'width':860,'height':1000})
        page.route('http://relationship.test/', lambda r:r.fulfill(content_type='text/html',body='''
          <style>body{background:#18191c;color:#eee;font:15px/1.75 "Microsoft YaHei",sans-serif;margin:0;padding:32px}
          main{max-width:650px;margin:auto}p{margin:0}summary{font-weight:600;cursor:pointer}
          .text-text-secondary{color:#bbb}.text-caption-m{font-size:12px}
          button{background:#292b30;border:1px solid #666;color:#eee}button[aria-pressed=true]{background:#eee;color:#18191c}
          button:focus-visible{outline:2px solid #b6caf5;outline-offset:3px}
          @media(max-width:450px){body{padding:20px}}</style><main id="panel"></main>'''))
        def api(r):
            if r.request.method == 'POST':
                value = r.request.post_data_json
                requests.append((value, r.request.headers))
                assert value['version'] == data['version']
                data.update(route=value['route'],version=data['version']+1)
            r.fulfill(content_type='application/json',body=json.dumps({'code':0,'data':data}))
        page.route('http://relationship.test/toy/relationship**',api)
        page.goto('http://relationship.test/')
        page.add_script_tag(content='''
const alive=()=>true;
const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
const button=(label,handler)=>{const e=text('button',label);e.onclick=handler;return e};
const apiBase='http://relationship.test',CONFIRM_HEADER='x-olivia-companion-confirm',CONFIRM_VALUE='1';
const VIDEO_CAPABILITY_PATH='vc',VIDEO_REPLY_SETTINGS_PATH='vs',MEMORY_PATH='m',DAILY_LIFE_PATH='d',STATUS_PATH='s',PROACTIVE_STATUS_PATH='p',LOCAL_LETTER_IMPORT_PATH='l';
const requestJson=''' + reader + '\nwindow.makePanel=' + renderer)
        page.evaluate("async()=>{const panel=window.makePanel();document.querySelector('#panel').replaceChildren(panel);panel.open=true;await panel.refresh()}")
        page.get_by_text('恋人 · 第四卷「信赖」').wait_for()
        assert page.locator('dt').all_text_contents() == ['熟悉','信任','安心','亲近']
        assert '她叫你：阿晴' in page.locator('#panel').inner_text()
        assert '我们的故事' not in page.locator('#panel').inner_text()
        page.screenshot(path=str(screenshots/'relationship-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(screenshots/'relationship-mobile.png'),full_page=True)
        page.get_by_role('button',name='挚友',exact=True).click()
        page.get_by_text('挚友 · 第四卷「信赖」').wait_for()
        assert requests[0][0] == {'route':'friend','version':7}
        assert requests[0][1]['x-olivia-companion-confirm'] == '1'
        data.update(moments=[],plans=[],addressing={})
        page.evaluate("async()=>await document.querySelector('details').refresh()")
        assert '暂时没有未完成的约定' in page.locator('#panel').inner_text()
        data['moments']=[{'date':'2026-10-16','text':'<img src=x onerror=alert(1)>','quote':'<script>danger</script>'}]
        page.evaluate("async()=>await document.querySelector('details').refresh()")
        assert page.locator('#panel img, #panel script').count()==0
        page.route('http://relationship.test/toy/relationship',lambda r:r.fulfill(status=503,body='{}'))
        page.evaluate("async()=>await document.querySelector('details').refresh()")
        assert page.get_by_role('button',name='重试读取关系').is_visible()
        browser.close()
