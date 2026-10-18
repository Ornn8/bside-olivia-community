import json
import os
from pathlib import Path
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
import pytest

sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright


def test_account_display_shows_multipliers_only(tmp_path):
    models = BOOTSTRAP_JAVASCRIPT.split('  const mountRelayModels = ',1)[1].split('  const mountModelRoutes = ',1)[0]
    statement = BOOTSTRAP_JAVASCRIPT.split('  const drawUnifiedStatement = ',1)[1].split('  const mountRelayAccount = ',1)[0]
    output = Path(os.environ.get('TIER_SCREENSHOTS',tmp_path))
    output.mkdir(parents=True,exist_ok=True)
    catalog = {'selected_model':'claude-sonnet-5-5','models':[
        {'id':'claude-sonnet-5-5','display_name':'Claude Sonnet 5.5','input_multiplier':'1.00000','output_multiplier':'1.00000',
         'pricing':{'input_per_million_yuan':'9.283122','output_per_million_yuan':'46.41561'}},
        {'id':'claude-haiku-5-5','display_name':'Claude Haiku 5.5','input_multiplier':'0.24776','output_multiplier':'0.30970',
         'pricing':{'input_per_million_yuan':'2.3','output_per_million_yuan':'14.375'}}]}
    account = {'items':[], 'minimum_charge_yuan':'0.01','jev_minimum_charge_yuan':'0.005',
        'jev_pricing':{'input_multiplier':'0.5','input_per_million_yuan':'0.845','own_model_input_per_million_yuan':'0.505'}}
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome')
        page=browser.new_page(viewport={'width':1000,'height':850})
        page.set_content('''<style>body{background:#18191c;color:#eee;font:15px/1.65 "Microsoft YaHei";margin:24px}
            main{max-width:800px;margin:auto}p{margin:0}.text-text-secondary{color:#bbb}.text-caption-m{font-size:12px}
            button{background:#292b30;color:#eee;border:1px solid #666;padding:8px}</style>
            <main><section id="models"></section><h3>余额与消费明细</h3><section id="statement"></section></main>''')
        page.add_script_tag(content='''const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
            const button=(label,fn)=>{const e=text('button',label);e.onclick=fn;return e};
            const actions=()=>document.createElement('div');const mountModelRoutes=()=>{};
            const requestSetup=async()=>('''+json.dumps(catalog)+''');
            const mountRelayModels='''+models+'\nconst drawUnifiedStatement='+statement+
            '\nmountRelayModels(document.querySelector("#models"));drawUnifiedStatement(document.querySelector("#statement"),'+json.dumps(account)+');')
        page.screenshot(path=str(output/'account-display-debug.png'),full_page=True)
        body = page.inner_text('body')
        assert '输入 1× · 输出 1×' in body and '输入 0.25× · 输出 0.31×' in body, body
        assert '¥9.283122' not in body and '百万' not in body and 'JEV 判断输入' not in body
        assert 'JEV 判断每次最低 ¥0.01，' in page.inner_text('body')
        assert '¥0.005' not in page.inner_text('body')
        page.screenshot(path=str(output/'account-display-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output/'account-display-mobile.png'),full_page=True)
        browser.close()
