"""The shipping pets page: adopt with a name after a confirmation, then buy food once per request."""
import base64
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==')


def test_adopt_and_feed_with_confirmations():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    page_source = (source[source.index('  const itemsBreadcrumb ='):source.index('  const mountItemsPage =')]
                   + source[source.index('  const mountPetsPage ='):source.index('  const installNativeDiaryRoute =')])
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        errors, requested = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def serve(route):
            requested.append(route.request.url.rsplit('/', 1)[-1])
            route.fulfill(status=200, content_type='image/png', body=PIXEL)
        page.route('http://olivia-api.test/toy/images/ui/**', serve)
        page.set_content('''<style>html,body{height:100%;margin:0;background:#111214;color:#e9e3d8;font:16px/1.5 sans-serif}
          #app{padding:56px 48px 32px 20px;height:100%;box-sizing:border-box}#page{height:100%}</style>
          <div id="app"><main id="page"></main></div>''')
        page.add_script_tag(content=helpers + '''
          const apiBase='http://olivia-api.test/';
          const goWorld=()=>{};
          window.calls=[];
          const breeds=[{id:'orange-tabby',species:'cat',name:'橘猫',summary:'能吃能睡。',stages:['幼猫','半大的猫','成年猫']},
                        {id:'ragdoll',species:'cat',name:'布偶猫',summary:'蓝眼睛。',stages:['幼猫','半大的猫','成年猫']}];
          let pets=[];
          const state=extra=>({breeds,pets,foods:{cat:'猫粮'},adopt_cents:990,food_cents:200,bag_days:7,balance_cents:2000,...extra});
          const routeRequest=async(path,body,options={})=>{
            if(path!=='/toy/world/pets')throw Error('unexpected '+path);
            if(!body)return state();
            if(options.confirmed!==true)throw Error('unconfirmed');
            window.calls.push(body);
            if(body.action==='adopt'){pets=[{breed:body.breed,name:body.name,species:'cat',breed_name:'橘猫',stage:1,stage_name:'幼猫',
              fed_days:1,next_stage_at:7,food_days:6,adopted:1,room:'客厅'}];return state({charged_cents:990});}
            if(window.calls.filter(c=>c.action==='food').length===1)throw Error('GPU_REQUEST_FAILED');  // first attempt fails
            pets=[{...pets[0],food_days:13}];return state({charged_cents:200});
          };
        ''' + page_source + "mountPetsPage(document.querySelector('#page'));")
        page.get_by_text('布偶猫').wait_for()
        adopt = page.get_by_role('button', name='领养 · ¥9.90').first
        adopt.click()
        assert page.get_by_text('先给它起个名字吧。').is_visible()
        page.get_by_label('给橘猫起名字').fill('团子')
        adopt.click()
        page.get_by_role('button', name='确认领养「团子」（¥9.90）').click()
        page.get_by_text('「团子」到家了！').wait_for()
        assert page.get_by_text('猫粮还够吃 6 天。').is_visible()
        buy = page.get_by_role('button', name='买一袋猫粮 · ¥2.00')
        buy.click(); page.get_by_role('button', name='确认购买（¥2.00）').click()
        page.get_by_text('没有成功，没有扣费').wait_for()
        buy.click(); page.get_by_role('button', name='确认购买（¥2.00）').click()
        page.get_by_text('猫粮还够吃 13 天。').wait_for()
        calls = page.evaluate('window.calls')
        assert calls[0] == {'action': 'adopt', 'breed': 'orange-tabby', 'name': '团子'}
        food = [c['request_id'] for c in calls if c['action'] == 'food']
        assert len(food) == 2 and food[0] == food[1]   # a retry reuses its request id, so it is charged once
        assert 'pet-orange-tabby-1' in requested and 'pet-ragdoll-1' in requested
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'pets.png'), full_page=True)
        assert errors == []
        browser.close()
