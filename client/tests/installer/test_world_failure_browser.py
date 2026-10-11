"""Real JS transport/render errors preserve the last view and report finite data."""
import json
from pathlib import Path
import re

import pytest
from jsonschema import validate

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize('mode,stage,kind,status', [
    ('http', 'request', 'Error', 503),
    ('json', 'response', 'SyntaxError', 200),
    ('shape', 'response', 'Error', 200),
    ('render', 'render', 'TypeError', 200),
])
def test_world_failure_retains_view_and_reports_actual_stage(tmp_path, mode, stage, kind, status):
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    renderer = source.split('  const renderPrivateWorldPanel =', 1)[1].split('  const setupInput =', 1)[0]
    read = source.split('  const requestJson =', 1)[1].split('  const publishProactiveState =', 1)[0]
    mutation = source.split('  const requestMutation =', 1)[1].split('  const requestSetup =', 1)[0]
    css = re.search(r'const mountWorldPage = .*?style.textContent=`(.*?)`;', source, re.S).group(1)
    constants = '\n'.join(re.findall(r'  const [A-Z][A-Z_]* = [^\n]+;', source))
    payload = dict(schema_version='olivia.daily-life.v1', status='READY', stale=False, refreshing=False,
        current=dict(activity='练琴', location='琴房', note='private-letter-fixture', occurred_at='2026-10-05T12:00:00Z'),
        projects=[], shared=[], moments=[], rhythm={},
        world=dict(schedule=dict(date='2026-10-05', classes=[]), meals=[]),
        emotion=dict(status='available', reactions=[], concerns=[]))
    schema = json.loads(Path('contracts/daily_life_diagnostic.schema.json').read_text(encoding='utf-8'))
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome')
        except playwright.Error:
            pytest.skip('Chrome is required for optional browser acceptance')
        page = browser.new_page(viewport={'width':1200, 'height':900})
        page.set_content('<style>body{background:#101112;color:#eee8de;font:15px/1.65 "Microsoft YaHei",sans-serif;padding:22px}*{box-sizing:border-box}</style><div data-olivia-world-page style="height:820px"><section id="world" data-world-main></section></div>')
        page.add_style_tag(content=css)
        page.evaluate('(value)=>window.payload=value', payload)
        page.add_script_tag(content='''
const apiBase='http://127.0.0.1:7777';
const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
const button=(label,handler)=>{const e=text('button',label);e.onclick=handler;return e};
const actions=()=>document.createElement('div'),stack=actions;
const privateWorldState=c=>c.state,stateLabels={available:'可用'},setDiagnosticDetails=()=>{};
window.reports=[];window.failureMode=null;
const nativeTimeout=window.setTimeout.bind(window);
window.setTimeout=(fn,ms)=>[1500,60000].includes(ms)?0:nativeTimeout(fn,ms);
window.fetch=async(url,options)=>{
  if(new URL(url).pathname.endsWith('/diagnostic')){
    window.reports.push(JSON.parse(options.body));
    return new Response(JSON.stringify({status:'RECORDED'}),{status:200});
  }
  if(window.failureMode==='http')return new Response(JSON.stringify({status:'UNAVAILABLE',error_code:'DAILY_LIFE_UNAVAILABLE'}),{status:503});
  if(window.failureMode==='json')return new Response('private-secret-malformed-json',{status:200});
  if(window.failureMode==='shape')return new Response(JSON.stringify({status:'READY',schema_version:'invalid-private-secret'}),{status:200});
  const result=JSON.parse(JSON.stringify(window.payload));
  if(window.failureMode==='render')result.world.today_activities={private_secret:'not-an-array'};
  return new Response(JSON.stringify(result),{status:200});
};
''' + constants + '\nconst requestJson ='+read+'\nconst requestMutation ='+mutation+'\nwindow.renderWorld ='+renderer+';')
        page.evaluate("async()=>{await renderWorld(document.querySelector('#world'),{state:'available'})}")
        assert page.get_by_role('tab').count() == 3
        page.screenshot(path=str(tmp_path/'world-before.png'), full_page=True)
        page.evaluate('(value)=>window.failureMode=value', mode)
        page.get_by_role('button', name='更新近况', exact=True).click()
        page.wait_for_function('window.reports.length === 1')
        report = page.evaluate('window.reports[0]')
        validate(report, schema)
        assert report == dict(endpoint='daily_life', method='POST', failure_stage=stage,
                              exception_type=kind, http_status=status)
        assert 'private-' not in json.dumps(report)
        assert page.get_by_role('tab').count() == 3
        assert '近况暂时无法读取，请稍后重试。' in page.locator('#world').inner_text()
        page.screenshot(path=str(tmp_path/'world-failed-retained.png'), full_page=True)
        page.evaluate('window.failureMode=null')
        page.get_by_role('button', name='更新近况', exact=True).click()
        page.wait_for_function("!document.querySelector('#world').innerText.includes('近况暂时无法读取，请稍后重试。')")
        assert page.get_by_role('tab').count() == 3
        page.screenshot(path=str(tmp_path/'world-recovered.png'), full_page=True)
        # An initial GET failure has no previous view to retain, but uses the
        # same shipped transport helpers and reports its actual stage.
        page.evaluate('(value)=>window.failureMode=value', mode)
        page.evaluate("async()=>{await renderWorld(document.querySelector('#world'),{state:'available'})}")
        page.wait_for_function('window.reports.length === 2')
        initial = page.evaluate('window.reports[1]')
        validate(initial, schema)
        assert initial == {**report, 'method':'GET'}
        assert page.get_by_role('tab').count() == 0
        page.screenshot(path=str(tmp_path/'world-initial-failure.png'), full_page=True)
        browser.close()
