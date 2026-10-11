from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_gifts_group_custom_title_repeat_and_legacy(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    cameras = (source[source.index('  const itemsBreadcrumb ='):source.index('  const mountItemsPage =')]
               + source[source.index('  const mountCamerasPage ='):source.index('  const installNativeDiaryRoute =')])
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True,
                                    args=['--disk-cache-dir=' + str(tmp_path / 'browser-cache')])
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        page.set_content('<style>html,body{height:100%;margin:0;background:#111214;color:#e9e3d8;font:16px/1.5 sans-serif}'
                         '#page{height:100%;padding:24px;box-sizing:border-box}</style><main id="page"></main>')
        page.add_script_tag(content=helpers + '''
          const apiBase='http://synthetic.test/', WORLD_ROUTE='#/world', ITEMS_ROUTE='#/world/items';
          const goWorld=()=>{},refreshDiaryBadge=async()=>{};
          window.orders=[];
          const catalog={cameras:[],items:[
            {id:'vinyl-record',category:'music',name:'黑胶',summary:'收藏喜欢的音乐',use:'一起听音乐',
             price_cents:0,owned:true,repeatable:true,customizable:true,custom:{title:'已有专辑'}},
            {id:'flowers',category:'consumable',name:'鲜花',summary:'桌边的一束花',use:'装点房间',
             price_cents:0,owned:false,repeatable:true,customizable:false,custom:null}]};
          const routeRequest=async(path,body)=>{if(body)window.orders.push(body);return catalog;};
        ''' + cameras + "mountCamerasPage(document.querySelector('#page'));")
        page.get_by_role('heading', name='音乐', exact=True).wait_for()
        assert page.get_by_role('heading', name='日常心意', exact=True).count() == 1
        page.get_by_role('button', name='再送一次', exact=False).click()
        dialog = page.get_by_role('dialog')
        field = dialog.get_by_label('专辑名（不超过 60 字）')
        field.fill('字' * 61)
        dialog.get_by_role('button', name='确认送出', exact=False).click()
        assert page.evaluate('orders.length') == 0
        assert dialog.get_by_text('请填写 1–60 字的名称。').is_visible()
        field.fill('合成专辑')
        output = Path('.evidence/piano-ui'); output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'gifts-custom-desktop.png'))
        dialog.get_by_role('button', name='确认送出', exact=False).click()
        page.wait_for_function('orders.length===1')
        assert page.evaluate('orders[0]') == {'item': 'vinyl-record', 'custom': {'title': '合成专辑'}}
        page.get_by_role('button', name='再送一次', exact=False).click()
        dialog.get_by_label('专辑名（不超过 60 字）').fill('另一张专辑')
        dialog.get_by_role('button', name='确认送出', exact=False).click()
        page.wait_for_function('orders.length===2')
        page.set_viewport_size({'width': 600, 'height': 800})
        page.screenshot(path=str(output / 'gifts-narrow.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.evaluate("delete catalog.items;mountCamerasPage(document.querySelector('#page'))")
        assert page.get_by_role('heading', name='拍摄设备', exact=True).count() == 1
        assert page.get_by_role('heading', name='音乐', exact=True).count() == 0
        browser.close()
