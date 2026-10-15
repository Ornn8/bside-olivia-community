"""Render the shipped relationship panel with qualitative stage descriptions."""
import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.story_ui import JAVASCRIPT


def test_relationship_panel_descriptions_and_small_screen(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    renderer = BOOTSTRAP_JAVASCRIPT.split('    const relationshipPanel = ', 1)[1].split('    const relationship = relationshipPanel();', 1)[0]
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome')
        except playwright.Error:
            pytest.skip('Chrome is required for the optional native UI browser check')
        page = browser.new_page(viewport={'width': 620, 'height': 380})
        page.set_content('<style>body{background:#18191c;color:#eee;font:16px/1.7 "Microsoft YaHei",sans-serif;padding:24px}p{margin:0}.text-text-secondary{color:#aaa}summary{font-weight:600}</style><main id="panel"></main>')
        page.add_script_tag(content=JAVASCRIPT + '''
const alive=()=>true;
const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
const button=(label,handler)=>{const e=text('button',label);e.onclick=handler;return e};
const PRIVATE_WORLD_PATH='/relationship';
const routeRequest=async()=>window.payload;
window.makePanel=''' + renderer)
        for width in (620, 390):
            page.set_viewport_size({'width':width,'height':600})
            page.evaluate('value=>window.payload=value', {'version':1,'route':'friend','routes':{'friend':'挚友'},
                'volume':3,'title':'朋友','description':'你们愿意分享生活。','dimensions':{'trust':{'text':'她很信任你。','rising':True,'rising_text':'最近她更信任你了。'}},
                'addressing':{},'moments':[],'plans':[]})
            page.evaluate("async()=>{const panel=window.makePanel();document.querySelector('#panel').replaceChildren(panel);panel.open=true;await panel.refresh()}")
            content=page.locator('#panel').inner_text()
            assert '挚友' in content and '最近她更信任你了。' in content
            assert '读我们的故事' in content and '保存相处方式' in content
            assert page.locator('progress, meter').count()==0
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / 'relationship-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 420})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / 'relationship-mobile.png'), full_page=True)
        browser.close()
