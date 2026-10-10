"""The shipping camera page loads pictures from the local API and its title clears the top navigation."""
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_camera_images_use_api_base_and_title_clears_navigation():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    cameras = (source[source.index('  const itemsBreadcrumb ='):source.index('  const mountItemsPage =')]
               + source[source.index('  const mountCamerasPage ='):source.index('  const installNativeDiaryRoute =')])
    navigation = source[source.index('  const mountMainNavigation ='):source.index('  const finishInitialSetup =')]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        errors, requested = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        import base64
        pixel = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==')

        def serve(route):
            requested.append(route.request.url)
            route.fulfill(status=200, content_type='image/png', body=pixel)
        page.route('http://olivia-api.test/toy/images/ui/**', serve)
        page.set_content('''<style>
          html,body{height:100%;margin:0;background:#111214;color:#e9e3d8;font:16px/1.5 "Microsoft YaHei",sans-serif}
          #app{padding:56px 48px 32px 20px;height:100%;box-sizing:border-box}#page{height:100%}
        </style><div id="app"><main id="page"></main></div>''')
        page.add_script_tag(content=helpers + '''
          const apiBase='http://olivia-api.test/';
          const WORLD_ROUTE='#/world',ITEMS_ROUTE='#/world/items';
          const goWorld=()=>{};const refreshDiaryBadge=async()=>{};
          const routeRequest=async path=>{
            if(path==='/toy/world/gifts')return {balance_cents:2519,cameras:[
              {id:'fujifilm-x100vi',name:'Fujifilm X100VI',year:2024,summary:'旁轴数码相机',specs:['23mm f/2'],use:'日常出门',price_cents:500,owned:false},
              {id:'ricoh-gr-iiix',name:'Ricoh GR IIIx',year:2021,summary:'口袋相机',specs:['26.1mm f/2.8'],use:'扫街',price_cents:500,owned:true}]};
            throw Error('unexpected '+path);
          };
        ''' + cameras + navigation + '''
          location.hash='#/world/cameras';
          mountCamerasPage(document.querySelector('#page'));
          mountMainNavigation();
        ''')
        page.get_by_text('Ricoh GR IIIx').wait_for()
        page.wait_for_function("[...document.querySelectorAll('.oc-card img')].every(img => img.complete && img.naturalWidth > 0)")
        assert sorted(url.rsplit('/', 1)[-1] for url in requested) == ['camera-fujifilm-x100vi', 'camera-ricoh-gr-iiix']
        title_end = page.evaluate("(() => {const r = document.createRange();"
                                  " r.selectNodeContents(document.querySelector('main header h1'));"
                                  " return r.getBoundingClientRect().right;})()")
        nav_start = page.evaluate("document.querySelector('[data-olivia-main-navigation]').getBoundingClientRect().left")
        assert title_end + 8 <= nav_start <= title_end + 24
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'cameras.png'))
        assert errors == []
        browser.close()
