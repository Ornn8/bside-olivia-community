"""Render the actual wardrobe component with all styles and new garment cards."""
import os
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.wardrobe import DAILY_LOOK_COUNTS


def test_expanded_wardrobe_category_previews_and_claim():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    component = source[source.index('  const wardrobeStyle ='):source.index('  const goWorld =')]
    names = ['森女系','叛逆学院','暗黑古着','工装酷感','领带短裤','法式复古','大地色古着','日系学院','甜美千金','复古洋娃娃','异域舞台']
    labels = {'fantasy-01':'黑金猫耳舞台装','fantasy-02':'月砂斜肩缎面长裙','fantasy-03':'绯夜丝绒束腰长裙'}
    styles = [{'style_id':'original','label':'原版日常','description':'原版搭配','looks':[]}]
    for (sid, count), name in zip(DAILY_LOOK_COUNTS.items(), names):
        styles.append({'style_id':sid,'label':name,'description':'林离会从已拥有的衣服中挑选今天的搭配。',
            'looks':[{'look_id':f'{sid}-{i:02}', 'label':labels.get(f'{sid}-{i:02}', f'{name} {i}'),
                      'image_url':f'https://wardrobe.test/{sid}-{i:02}.png'} for i in range(1,count+1)]})
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width':1200,'height':960})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<style>body{background:#111214;color:#eee;font:16px "Microsoft YaHei",sans-serif;margin:24px}#app{height:900px}</style><main id="app"></main>')
        preview_root = os.environ.get('OLIVIA_WARDROBE_PREVIEW_ROOT')
        def image(route):
            filename = route.request.url.rsplit('/',1)[-1]
            path = Path(preview_root)/'images'/filename if preview_root else None
            if path and path.is_file():
                route.fulfill(path=str(path), content_type='image/png')
            else:
                route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450"><rect width="300" height="450" fill="#ccc"/></svg>')
        page.route('https://wardrobe.test/**', image)
        page.evaluate('(styles)=>window.styles=styles', styles)
        page.add_script_tag(content=helpers+'''
            window.writes=[];
            const videoReplyRequestId=()=> 'fixture-claim';
            const purchases={price_cents:500,free_limit:3,free_remaining:2,owned:['fantasy-02']};
            const routeRequest=async(path,body,options)=>{
              if(body){if(!options.confirmed)throw Error('unconfirmed');window.writes.push(body);
                purchases.owned.push(body.look_id);purchases.free_remaining--;}
              return {wardrobe:{style_id:'fantasy'},wardrobe_styles:window.styles,purchases:structuredClone(purchases)};
            };
        '''+component+"mountWardrobeSetting(document.querySelector('#app'));")
        page.get_by_role('tab',name='异域舞台',exact=True).wait_for()
        assert page.get_by_role('tab').count() == 12
        page.get_by_role('tab',name='异域舞台',exact=True).click()
        assert page.locator('.ow-garment').count() == 3
        for label in labels.values(): assert page.get_by_text(label, exact=True).is_visible()
        page.wait_for_function("[...document.images].every(i=>i.complete && i.naturalWidth>0)")
        assert page.get_by_role('button',name='已拥有',exact=True).count() == 1
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output/'wardrobe-expanded-desktop.png'), full_page=True)
        page.get_by_role('button',name='免费领取',exact=True).last.click()
        assert page.evaluate('window.writes.length') == 0
        page.get_by_role('button',name='确认免费领取',exact=True).click()
        page.wait_for_function('window.writes.length===1')
        assert page.evaluate('window.writes[0].look_id') == 'fantasy-03'
        assert page.get_by_role('button',name='已拥有',exact=True).count() == 2
        page.set_viewport_size({'width':480,'height':920})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(output/'wardrobe-expanded-compact.png'), full_page=True)
        assert not errors
        browser.close()
