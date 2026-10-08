from pathlib import Path
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_free_choices_purchase_confirmation_and_owned_buttons():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    component = source[source.index('  const wardrobeStyle ='):source.index('  const goWorld =')]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width':1200,'height':900})
        errors = []
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.set_content('<style>body{background:#111214;color:#eee;font:16px sans-serif;margin:24px}#app{height:850px}</style><main id="app"></main>')
        page.route('https://wardrobe.test/**', lambda route:route.fulfill(content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="400"><rect width="300" height="400" fill="#c5bcac"/><text x="55" y="210" font-size="22">Outfit preview</text></svg>'))
        page.add_script_tag(content=helpers+'''
          window.writes=[];let nextId=0;
          const videoReplyRequestId=()=> 'purchase-'+(++nextId);
          const purchases={price_cents:500,free_limit:3,free_remaining:3,owned:[]};
          const styles=[{style_id:'original',label:'原版日常',description:'免费基础服装',looks:[]},
            {style_id:'mori',label:'森女系',description:'自然柔和的日常穿搭',looks:[1,2,3,4].map(i=>({look_id:'mori-0'+i,label:'森女服装 '+i,image_url:'https://wardrobe.test/'+i+'.svg'}))}];
          const routeRequest=async(path,body,options)=>{
            if(body){if(!options.confirmed)throw Error('unconfirmed');window.writes.push(body);
              purchases.owned.push(body.look_id);if(body.max_charge_cents===0)purchases.free_remaining--;}
            return {wardrobe:{style_id:'original'},wardrobe_styles:styles,purchases:structuredClone(purchases)};
          };
        '''+component+"mountWardrobeSetting(document.querySelector('#app'));")
        page.wait_for_timeout(200)
        assert not errors, errors
        assert page.get_by_role('tab').count(), page.locator('body').inner_text()
        page.get_by_role('tab',name='森女系',exact=True).click()
        assert page.get_by_role('button',name='指定这个风格',exact=True).is_disabled()
        for index in range(3):
            page.get_by_role('button',name='免费领取',exact=True).first.click()
            assert page.evaluate('window.writes.length') == index
            page.get_by_role('button',name='确认免费领取',exact=True).click()
            page.wait_for_function('(n)=>window.writes.length===n',arg=index+1)
        assert page.get_by_role('button',name='指定这个风格',exact=True).is_enabled()
        page.get_by_role('button',name='¥5 解锁',exact=True).click()
        assert page.evaluate('window.writes.length') == 3
        output=Path('.evidence');output.mkdir(exist_ok=True)
        page.screenshot(path=str(output/'wardrobe-purchase-confirm.png'),full_page=True)
        page.get_by_role('button',name='确认购买 · ¥5',exact=True).click()
        page.wait_for_function('window.writes.length===4')
        assert page.evaluate('window.writes.map(x=>x.max_charge_cents)') == [0,0,0,500]
        assert page.get_by_role('button',name='已拥有',exact=True).count() == 4
        assert page.get_by_role('button',name='已拥有',exact=True).first.is_disabled()
        page.set_viewport_size({'width':480,'height':920})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(output/'wardrobe-purchase-mobile.png'),full_page=True)
        assert not errors
        browser.close()
