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
          const breeds=[{id:'orange-tabby',name:'橘猫',summary:'能吃能睡。',stages:['幼猫','半大的猫','成年猫']},
                        {id:'ragdoll',name:'布偶猫',summary:'蓝眼睛。',stages:['幼猫','半大的猫','成年猫']}];
          let pets=[],items=[{id:'cat-tree',kind:'home',name:'猫爬架',summary:'三层。',price_cents:990,owned:false},
                             {id:'bell-collar',kind:'wear',name:'铃铛项圈',summary:'红色。',price_cents:490,owned:false}];
          const state=extra=>({breeds,items,pets,adopt_cents:990,max_pets:3,feeds_per_day:2,balance_cents:2000,...extra});
          const routeRequest=async(path,body,options={})=>{
            if(path!=='/toy/world/pets')throw Error('unexpected '+path);
            if(!body)return state();
            if(options.confirmed!==true)throw Error('unconfirmed');
            window.calls.push(body);
            if(body.action==='adopt'){pets=[{breed:body.breed,name:body.name,breed_name:'橘猫',personality:'greedy',personality_name:'贪吃',
              stage:1,stage_name:'幼猫',days:0,next_stage_in:7,build:'normal',build_name:'匀称',feeds_left_today:2,adopted:1,room:'厨房'}];
              return state({charged_cents:990});}
            if(body.action==='feed'){pets=[{...pets[0],feeds_left_today:pets[0].feeds_left_today-1}];return state({fed:true});}
            items=items.map(item=>item.id===body.item?{...item,owned:true}:item);return state({charged_cents:990});
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
        assert page.get_by_text('橘猫 · 幼猫 · 贪吃 · 匀称').is_visible()
        page.get_by_role('button', name='让她喂一下（今天还能喂 2 次）').click()     # free: no confirmation
        page.get_by_role('button', name='让她喂一下（今天还能喂 1 次）').click()
        assert page.get_by_role('button', name='今天已经喂饱了').is_disabled()
        page.get_by_role('button', name='买给它 · ¥9.90').click()
        page.get_by_role('button', name='确认购买（¥9.90）').click()
        page.get_by_text('已经买了，会出现在她的照片里').wait_for()
        calls = page.evaluate('window.calls')
        assert calls == [{'action': 'adopt', 'breed': 'orange-tabby', 'name': '团子'},
                         {'action': 'feed', 'breed': 'orange-tabby'}, {'action': 'feed', 'breed': 'orange-tabby'},
                         {'action': 'item', 'item': 'cat-tree'}]
        assert {'pet-orange-tabby-1', 'pet-ragdoll-1', 'pet-item-cat-tree'} <= set(requested)
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'pets.png'), full_page=True)
        assert errors == []
        browser.close()
