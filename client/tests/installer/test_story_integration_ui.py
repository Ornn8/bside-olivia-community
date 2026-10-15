from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.story_ui import JAVASCRIPT


def test_story_uses_diary_tabs_and_reading_container():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    diary = (source[source.index('  const DIARY_ROUTE ='):source.index('  const mountWorldPage =')]
             + source[source.index('  const goWorld ='):source.index('  const openWardrobe =')]
             + source[source.index('  const diaryStyle ='):source.index('  const mountCamerasPage =')])
    helpers = source[source.index('  const text = (tag, value, className)'):source.index('  const setButtonsBusy =')]
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<style>html,body{height:100%;margin:0;background:#111214;color:#e9e3d8;font:16px/1.5 "Microsoft YaHei",sans-serif}#page{height:100%;padding:24px;box-sizing:border-box}</style><main id="page"></main>')
        page.add_script_tag(content=JAVASCRIPT + helpers + r'''
            const apiBase='http://127.0.0.1:27149';
            window.holdDiary=true;
            const routeRequest=async(path,body)=>{
              if(path==='/toy/relationship-story')return window.relationshipFixture;
              if(path==='/toy/relationship-story/mark'){
                const item=window.relationshipFixture.moments.find(item=>item.id===body.id);
                item.marked=body.enabled;
                window.relationshipFixture.pending_moments=window.relationshipFixture.moments.filter(item=>item.marked);
                return window.relationshipFixture;
              }
              if(path.startsWith('/toy/diary?')){
                if(window.holdDiary)return new Promise(resolve=>window.releaseDiary=()=>resolve({entries:[],total:0,enabled:true}));
                return {entries:[],total:0,enabled:true};
              }
              if(path==='/toy/story')return {enabled:true,model:'gemini-4-argon',status:'written',volumes:[
                {volume:2,title:'熟悉',description:'日常的分享让彼此不再陌生，聊天渐渐变得自在。',chapters:[
                  {id:'fixture',title:'窗边的琴声',body:'她把手边的书轻轻合上，想起他们聊过的那首曲子。\n窗外的树影慢慢移到了琴凳旁。',seen:false}]}]};
              if(path==='/toy/story/seen')throw Error('fixture seen failure');
              if(path==='/toy/story/delete')throw Error('fixture delete failure');
              return {};
            };
        ''' + diary + 'mountDiaryPage(document.querySelector("#page"));')
        page.get_by_role('button', name='我们的故事', exact=True).click()
        page.get_by_role('button', name='暂停写故事').wait_for()
        page.evaluate('()=>{window.holdDiary=false;window.releaseDiary()}')
        page.get_by_role('button', name='暂停写故事').wait_for()
        output = Path('.evidence/story-ui'); output.mkdir(parents=True, exist_ok=True)
        for name, width, height in [('desktop', 1200, 900), ('mobile', 390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(output / f'diary-story-{name}.png'), full_page=True)
        page.get_by_role('button', name='未读 · 窗边的琴声').click()
        page.get_by_text('本章可以阅读，但已读标记没有保存，请稍后重新打开。').wait_for()
        page.get_by_label('本章留言（最多200字）').fill('想看看琴房里的日常。')
        page.get_by_role('button', name='保存留言', exact=True).click()
        page.get_by_text('留言已保存，会作为下一章的参考。').wait_for()
        page.get_by_role('button', name='重写本章', exact=True).click()
        page.get_by_role('button', name='确认付费重写').wait_for()
        for name, width, height in [('desktop', 1200, 900), ('mobile', 390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(output / f'diary-story-reading-{name}.png'), full_page=True)
        page.get_by_role('button', name='删除本章', exact=True).click()
        page.get_by_role('button', name='确认删除').click()
        page.get_by_text('本章没有删除成功，请稍后重试。').wait_for()
        from runtime.story_policy import DEFAULTS, ROUTES
        page.evaluate('data=>window.relationshipFixture=data', {'version': 1, 'route': 'friend', 'routes': ROUTES,
            'volume': 3, 'title': '朋友', 'description': DEFAULTS['volumes']['friend']['3']['description'],
            'dimensions': {key: {'text': lines['medium'], 'rising': key == 'trust', 'rising_text': DEFAULTS['rising'][key]}
                           for key, lines in DEFAULTS['dimensions'].items()},
            'addressing': {'linli_calls_user': '小舟', 'user_calls_linli': '林离'},
            'moments': [{'id':'moment-one', 'marked':False, 'date': '2026-10-15', 'text': '你跟她说了心里话', 'quote': ''},
                        {'id':'moment-two', 'marked':True, 'date':'2026-10-14', 'text':'你们定下了一个约定', 'quote':'周末一起选歌。'}],
            'pending_moments':[{'id':'moment-two', 'marked':True, 'date':'2026-10-14', 'text':'你们定下了一个约定', 'quote':'周末一起选歌。'}],
            'plans': []})
        panel = source[source.index('    const relationshipPanel ='):source.index('    const relationship = relationshipPanel();')]
        page.add_script_tag(content='const alive=()=>true;\n' + panel +
            'const relation=relationshipPanel();document.querySelector("main").replaceChildren(relation);relation.open=true;')
        page.get_by_text('最近她更信任你了。', exact=False).wait_for()
        page.get_by_role('button', name='写进故事', exact=True).click()
        assert page.get_by_role('button', name='写进故事', exact=True).count()==0
        page.get_by_role('button', name='取消写进故事', exact=True).first.click()
        page.get_by_role('button', name='写进故事', exact=True).wait_for()
        for name, width, height in [('desktop', 1200, 1000), ('mobile', 390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            page.screenshot(path=str(output / f'relationship-wrapper-{name}.png'), full_page=True)
        assert errors == []
        browser.close()
