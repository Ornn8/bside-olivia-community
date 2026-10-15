import json
from pathlib import Path
import subprocess

import pytest

from runtime.story_ui import JAVASCRIPT


def test_story_node_controls_require_paid_confirmation():
    script = r'''
      class Element {
        constructor(tag){this.tag=tag;this.children=[];this.style={};this.isConnected=true;this.textContent='';}
        append(...items){this.children.push(...items)}
        replaceChildren(...items){this.children=items}
        setAttribute(){}
      }
      global.window={location:{hash:''}};global.document={createElement:tag=>new Element(tag)};
      const text=(tag,value)=>{const el=new Element(tag);el.textContent=value;return el};
      const button=(label,action)=>{const el=text('button',label);el.action=action;return el};
      const assert=require('node:assert/strict');const writes=[];
      const data={enabled:false,model:'gemini-4-argon',volumes:[{volume:1,title:'初识',description:'初识。',
        chapters:[{id:'one',title:'窗前',body:'窗前。',seen:false}]}]};
      const request=async(path,body,options)=>{if(body){assert.equal(options.confirmed,true);writes.push(path)}return data};
    ''' + JAVASCRIPT + r'''
      (async()=>{
        const main=new Element('main');await window.oliviaStoryView(main,{text,button,request});
        const find=(el,label)=>el.textContent===label?el:el.children.map(child=>find(child,label)).find(Boolean);
        assert.ok(find(main,'开始写我们的故事'));
        await find(main,'未读 · 窗前').action();
        await find(main,'重写本章').action();
        assert.deepEqual(writes,['/toy/story/seen']);
        await find(main,'确认付费重写').action();
        assert.deepEqual(writes,['/toy/story/seen','/toy/story/rewrite']);
      })().catch(e=>{console.error(e);process.exitCode=1});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_story_visual_and_relationship_text(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    from runtime.story_policy import DEFAULTS, ROUTES
    data = {'enabled': False, 'status': 'paused', 'model': 'gemini-4-argon',
        'volumes': [{'volume': 1, 'title': '初识', 'description': '你们刚刚走进彼此的生活。', 'chapters': []}]}
    relation = {'version': 1, 'route': 'friend', 'routes': ROUTES, 'volume': 3, 'title': '朋友',
        'description': '你们愿意分享生活里的小事，也开始在意彼此的心情。',
        'dimensions': {key: {'text': values['medium'], 'rising': key == 'trust'} for key, values in DEFAULTS['dimensions'].items()},
        'addressing': {'linli_calls_user': '小舟', 'user_calls_linli': '林离'},
        'moments': [{'date': '2026-10-15', 'text': '你跟她说了心里话', 'quote': ''}], 'plans': []}
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.set_content('<style>body{margin:0;background:#161719;color:#ded9d1;font:16px/1.8 "Microsoft YaHei",sans-serif}main{max-width:900px;margin:32px auto;padding:24px}button,select{font:inherit;color:inherit;background:#292b2e;border:1px solid #666;border-radius:8px;padding:8px 14px;margin:6px}h2,h3{margin-top:28px}</style><main></main>')
        page.add_script_tag(content=JAVASCRIPT)
        page.evaluate('''data=>{window.text=(tag,value)=>{const e=document.createElement(tag);e.textContent=value;return e};
            window.button=(label,action)=>{const e=text('button',label);e.onclick=action;return e};
            window.request=async()=>data;
            return window.oliviaStoryView(document.querySelector('main'),{text,button,request});}''', data)
        page.get_by_role('button', name='开始写我们的故事').wait_for()
        output = Path('.evidence/story-ui'); output.mkdir(parents=True, exist_ok=True)
        for name, width, height in [('desktop', 1200, 900), ('mobile', 390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(output / f'story-{name}.png'), full_page=True)
        page.evaluate('data=>window.oliviaRelationshipView(document.querySelector("main"),data,{text,button,request,refresh:()=>{}})', relation)
        assert page.get_by_text('你跟她说了心里话', exact=False).is_visible()
        assert page.locator('progress,input[type=range]').count() == 0
        for name, width, height in [('desktop', 1200, 1000), ('mobile', 390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(output / f'relationship-{name}.png'), full_page=True)
        browser.close()
