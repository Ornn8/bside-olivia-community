"""Exercise the shipping world entry, wardrobe route, browsing and save rollback."""
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.wardrobe import DAILY_STYLES


def test_world_wardrobe_entry_browse_confirm_rollback_and_layout():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    component = source[source.index('  const wardrobeStyle ='):source.index('  const mountMusicSettings =')]
    navigation = source[source.index('  const WORLD_ROUTE ='):source.index('  const finishInitialSetup =')]
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 870})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('''<style>
          html,body{height:100%;margin:0;background:#111214;color:#e9e3d8;font:16px/1.5 "Microsoft YaHei",sans-serif}
          #app{padding:118px 48px 32px 120px;height:100%;box-sizing:border-box}
          #page{height:100%}button,a{-webkit-app-region:no-drag}
          @media(max-width:650px){#app{padding:110px 16px 24px}}
        </style><div id="app"><main id="page"></main></div>''')
        labels=['原版日常','森女系','叛逆学院','暗黑古着','工装酷感','领带短裤']
        styles=[{'style_id':sid,'label':label,'description':'测试风格参考搭配','looks':[
            {'look_id':sid+'-01','label':label+'参考图','image_url':'http://wardrobe.test/'+sid+'.png'}] if sid!='original' else []}
            for sid,label in zip(DAILY_STYLES,labels)]
        # Use accepted references for the local screenshot pass when available; CI needs no private assets.
        gallery=Path('.evidence/wardrobe-concepts/style-overview')
        import json
        sources={}
        if (gallery/'classification.json').is_file():
            sources={c['id']:gallery/c['looks'][0]['src'] for c in json.loads((gallery/'classification.json').read_text('utf-8'))['categories'] if c['id'] in DAILY_STYLES[1:]}
        def serve_image(route):
            sid=route.request.url.rsplit('/',1)[-1].removesuffix('.png')
            if sid in sources:route.fulfill(path=str(sources[sid]))
            else:route.fulfill(status=503,body='synthetic unavailable')
        page.route('http://wardrobe.test/**',serve_image)
        page.evaluate('(payload)=>{window.styles=payload}', styles)
        page.add_script_tag(content=helpers + '''
          let style='original';window.failSave=false;window.failLoad=false;window.writes=[];
          const videoReplyRequestId=()=> 'video_reply_setting:synthetic';
          const routeRequest=async(path,body)=>{
            if(path!='/toy/world/wardrobe')throw Error('incorrect endpoint');
            if(body){window.writes.push(body);if(window.failSave)throw Error('synthetic failure');style=body.style_id;return {wardrobe:{style_id:style},daily_outfit:null};}
            if(window.failLoad)throw Error('synthetic read failure');
            return {wardrobe:{style_id:style},wardrobe_styles:window.styles};
          };
          const STATUS_PATH='/status';const requestJson=async()=>({capabilities:{private_world:{state:'available'}}});
          const renderPrivateWorldPanel=async(panel)=>panel.append(text('h3','今天的生活'));
          const openDialog=()=>{};const records=new Map();
          window.__oliviaNativeView={h:()=>{},router:{hasRoute:name=>records.has(name),
            addRoute:record=>records.set(record.name,record),replace:()=>{},
            push:path=>{
              window.location.hash='#'+path;
              const record=[...records.values()].find(r=>r.path===path);
              document.querySelector('#page').replaceChildren();
              record.component.mounted.call({$el:document.querySelector('#page')});
              mountMainNavigation();
            }}};
        ''' + navigation + component + '''
          installNativeWorldRoute();installNativeWardrobeRoute();
          window.__oliviaNativeView.router.push('/world');
        ''')
        assert page.get_by_role('button', name='打开林离的衣橱').is_visible()
        output = Path('.evidence');output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'wardrobe-world-entry.png'), full_page=True)
        page.get_by_role('button', name='打开林离的衣橱').click()
        page.get_by_role('tab', name='原版日常').wait_for()
        assert page.locator('.ow-current strong').inner_text() == '原版日常'
        assert page.get_by_role('link', name='世界', exact=True).get_attribute('aria-current') == 'page'
        page.get_by_role('tab', name='森女系').click()
        assert page.get_by_role('tabpanel').get_by_role('heading').inner_text() == '森女系'
        assert page.evaluate('window.writes.length') == 0
        assert page.locator('.ow-current strong').inner_text() == '原版日常'
        page.get_by_role('button', name='指定这个风格').click()
        page.wait_for_function("document.querySelector('.ow-current strong').textContent==='森女系'")
        assert page.evaluate('window.writes[0].style_id') == 'mori'
        assert page.evaluate('Object.keys(window.writes[0]).sort().join()') == 'request_id,style_id'
        assert page.get_by_role('status').inner_text() == '风格已生效，林离会按新风格挑选搭配。'
        assert page.get_by_role('button', name='已指定此风格').is_disabled()
        assert page.locator('.ow-footer').inner_text().find('立即生效') >= 0
        page.evaluate('window.failSave=true')
        page.get_by_role('tab', name='暗黑古着').click()
        page.get_by_role('button', name='指定这个风格').click()
        page.get_by_role('status').filter(has_text='没有保存成功').wait_for()
        assert page.locator('.ow-current strong').inner_text() == '森女系'
        assert page.get_by_role('tabpanel').get_by_role('heading').inner_text() == '暗黑古着'
        page.evaluate('window.failSave=false')
        page.get_by_role('tab', name='原版日常').focus()
        page.keyboard.press('Home')
        page.keyboard.press('ArrowDown')
        assert page.get_by_role('tab', name='森女系').get_attribute('aria-selected') == 'true'
        page.keyboard.press('ArrowRight')
        assert page.get_by_role('tab', name='叛逆学院').get_attribute('aria-selected') == 'true'
        assert page.evaluate('window.writes.length') == 2
        page.get_by_role('button', name='指定这个风格').click()
        page.wait_for_function("document.querySelector('.ow-current strong').textContent==='叛逆学院'")
        assert page.locator('.ow-garment').count() == 1
        assert page.get_by_role('button',name='选择这套',exact=True).count()==0
        page.wait_for_function("[...document.images].every(i=>i.complete)")
        assert not errors
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.mouse.move(1, 1)
        page.locator('[data-olivia-wardrobe]').evaluate('(panel)=>panel.scrollTop=0')
        page.screenshot(path=str(output / 'wardrobe-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 480, 'height': 920})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(output / 'wardrobe-compact.png'), full_page=True)
        page.get_by_role('link', name='返回世界').click()
        assert page.get_by_role('button', name='打开林离的衣橱').is_visible()
        page.evaluate('window.failLoad=true')
        page.get_by_role('button', name='打开林离的衣橱').click()
        page.get_by_role('button', name='重新读取', exact=True).wait_for()
        assert page.get_by_role('button', name='指定这个风格').is_disabled()
        page.evaluate('window.failLoad=false')
        page.get_by_role('button', name='重新读取', exact=True).click()
        page.wait_for_function("document.querySelector('.ow-current strong').textContent==='叛逆学院'")
        assert not errors
        browser.close()
