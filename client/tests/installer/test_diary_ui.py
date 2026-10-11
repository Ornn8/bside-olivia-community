"""Exercise the shipping diary entry: list, unread mark, reading, confirmed comment and delete."""
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_diary_list_read_comment_and_delete():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    diary = (source[source.index('  const DIARY_ROUTE ='):source.index('  const mountWorldPage =')]
             + source[source.index('  const goWorld ='):source.index('  const openWardrobe =')]
             + source[source.index('  const diaryStyle ='):source.index('  const mountCamerasPage =')])
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 870})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('''<style>
          html,body{height:100%;margin:0;background:#111214;color:#e9e3d8;font:16px/1.5 "Microsoft YaHei",sans-serif}
          #app{padding:118px 48px 32px 120px;height:100%;box-sizing:border-box}#page{height:100%}
        </style><div id="app"><main id="page"></main></div>''')
        page.add_script_tag(content=helpers + '''
          window.writes=[];
          const body='今天你说下周三要考乐理，复习了一下午还是记不住转调。\\n我嘴上说「我陪你复习呀」，其实心里有点心疼。';
          let entries=[{day:'2026-10-07',title:'下周三的乐理',mood:'心疼',excerpt:body.slice(0,40),short:false,seen:false,commented:false,
                        written_at:'2026-10-07T19:00:00Z'},
                       {day:'2026-10-05',title:'早安',mood:'开心',excerpt:'你只说了一句早，我也回了早呀。',short:true,seen:true,commented:true,
                        written_at:'2026-10-05T19:00:00Z'}];
          let comments=[];
          const routeRequest=async(path,payload,options={})=>{
            if(payload&&options.confirmed!==true)throw Error('unconfirmed write');
            if(payload)window.writes.push([path,payload]);
            if(path.startsWith('/toy/diary?'))return {entries,total:entries.length,unseen:entries.filter(e=>!e.seen).length,enabled:true};
            if(path.startsWith('/toy/diary/entry')){const e=entries.find(x=>path.endsWith(x.day));e.seen=true;return {...e,body,comments};}
            if(path==='/toy/diary/comment'){comments.push({text:payload.text,written_at:'2026-10-08T01:00:00Z'});entries[0].commented=true;return {};}
            if(path==='/toy/diary/delete'){entries=entries.filter(e=>e.day!==payload.day);return {deleted:payload.day};}
            throw Error('unexpected '+path);
          };
        ''' + diary + '''
          mountDiaryPage(document.querySelector('#page'));
        ''')
        page.get_by_text('下周三的乐理').wait_for()
        assert page.locator('.od-new').count() == 1
        assert page.get_by_text('10月7日 星期三').is_visible()
        output = Path('.evidence'); output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / 'diary-list.png'), full_page=True)
        page.get_by_text('下周三的乐理').click()
        page.get_by_text('其实心里有点心疼').wait_for()
        page.locator('textarea').fill('谢谢你陪我复习')
        page.get_by_role('button', name='留言').click()
        page.get_by_text('谢谢你陪我复习').wait_for()
        page.screenshot(path=str(output / 'diary-entry.png'), full_page=True)
        page.get_by_role('button', name='删除这篇').click()
        page.get_by_role('button', name='确认删除').click()
        page.get_by_text('早安').wait_for()
        assert page.get_by_text('下周三的乐理').count() == 0
        writes = page.evaluate('window.writes')
        assert [w[0] for w in writes] == ['/toy/diary/comment', '/toy/diary/delete']
        assert errors == []
        browser.close()
