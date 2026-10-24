"""He can dress her in one exact look he owns (owner 2026-10-23); an older server keeps the plain owned label."""
from pathlib import Path
import pytest
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def mount(page, *, pinnable):
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    component = source[source.index('  const wardrobeStyle ='):source.index('  const goWorld =')]
    page.set_content('<style>body{background:#111214;color:#eee;font:16px sans-serif;margin:24px}#app{height:850px}</style><main id="app"></main>')
    page.route('https://wardrobe.test/**', lambda route: route.fulfill(content_type='image/svg+xml',
        body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="400"><rect width="300" height="400" fill="#c5bcac"/></svg>'))
    page.add_script_tag(content=helpers + '''
      window.writes=[];let nextId=0;const pinnable=%s;
      const videoReplyRequestId=()=> 'wear-'+(++nextId);
      const purchases={price_cents:500,free_limit:3,free_remaining:1,owned:['mori-01','mori-02']};
      const styles=[{style_id:'original',label:'原版日常',description:'免费基础服装',looks:[]},
        {style_id:'mori',label:'森女系',description:'自然柔和的日常穿搭',looks:[1,2,3].map(i=>({look_id:'mori-0'+i,label:'森女服装 '+i,image_url:'https://wardrobe.test/'+i+'.svg'}))}];
      let pinned=null,style='mori';
      const routeRequest=async(path,body,options)=>{
        if(body){if(!options.confirmed)throw Error('unconfirmed');window.writes.push(body);
          if('style_id' in body){style=body.style_id;pinned=null;}else if(!('max_charge_cents' in body))pinned=body.look_id;}
        const state={wardrobe:{style_id:style},wardrobe_styles:styles,purchases:structuredClone(purchases),
          daily_outfit:pinned?{look_id:pinned}:null};
        if(pinnable)state.pinned_look=pinned;
        return state;
      };
    ''' % ('true' if pinnable else 'false') + component + "mountWardrobeSetting(document.querySelector('#app'));")
    page.wait_for_timeout(200)


def test_an_owned_look_can_be_worn_and_shows_it_is_being_worn():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width':1200, 'height':900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        mount(page, pinnable=True)
        page.get_by_role('tab', name='森女系', exact=True).click()
        assert page.get_by_role('button', name='穿这件', exact=True).count() == 2
        assert page.get_by_role('button', name='免费领取', exact=True).count() == 1
        page.get_by_role('button', name='穿这件', exact=True).nth(1).click()
        page.wait_for_function('window.writes.length===1')
        assert page.evaluate('window.writes') == [{'request_id': 'wear-1', 'look_id': 'mori-02'}]
        assert page.get_by_role('button', name='正在穿', exact=True).is_disabled()
        assert page.get_by_role('button', name='穿这件', exact=True).count() == 1
        assert '林离今天穿「森女服装 2」' in page.locator('body').inner_text()
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'wardrobe-pin.png'), full_page=True)
        page.get_by_role('tab', name='原版日常', exact=True).click()
        page.get_by_role('button', name='指定这个风格', exact=True).click()
        page.wait_for_function('window.writes.length===2')
        page.get_by_role('tab', name='森女系', exact=True).click()
        assert page.get_by_role('button', name='穿这件', exact=True).count() == 2  # A style choice hands it back to her.
        assert not errors
        browser.close()


def test_an_older_server_keeps_the_owned_label():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width':1200, 'height':900})
        mount(page, pinnable=False)
        page.get_by_role('tab', name='森女系', exact=True).click()
        assert page.get_by_role('button', name='穿这件', exact=True).count() == 0
        assert page.get_by_role('button', name='已拥有', exact=True).count() == 2
        assert page.get_by_role('button', name='已拥有', exact=True).first.is_disabled()
        browser.close()
