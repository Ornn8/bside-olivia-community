"""Render the shipped relationship panel with qualitative stage descriptions."""
import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


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
        page.add_script_tag(content='''
const alive=()=>true;
const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
const button=(label,handler)=>{const e=text('button',label);e.onclick=handler;return e};
const PRIVATE_WORLD_PATH='/relationship';
const requestJson=async()=>window.payload;
window.makePanel=''' + renderer)
        for stage, phrase in [('unknown','还在慢慢建立'), ('acquaintance','刚认识不久'), ('familiar','渐渐熟了'),
                               ('friend','已经是朋友'), ('trusted_friend','彼此信赖的朋友'),
                               ('close','她会想你'), ('committed','确认了彼此的关系')]:
            page.evaluate('value=>window.payload=value', {'status':'READY', 'relationship_stage':stage, 'levels': {'tension':'low'}})
            page.evaluate("async()=>{const panel=window.makePanel(); document.querySelector('#panel').replaceChildren(panel);panel.open=true;await panel.refresh()}")
            content = page.locator('#panel').inner_text()
            assert phrase in content and '关系阶段：' in content
            assert '这里读取既有关系记录' in content
            assert '较高' not in content and '较低' not in content
            assert page.locator('dl, dd, dt').count() == 0
        page.evaluate("()=>{window.payload.relationship_stage='trusted_friend';window.payload.levels.tension='high'}")
        page.evaluate("async()=>await document.querySelector('details').refresh()")
        assert '最近你们之间有点小别扭。' in page.locator('#panel').inner_text()
        page.screenshot(path=str(tmp_path / 'relationship-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 420})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / 'relationship-mobile.png'), full_page=True)
        browser.close()
