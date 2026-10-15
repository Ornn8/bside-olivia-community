"""Exercise the shipped world renderer, including truthful stale/empty states."""
import json
import re

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_world_tabs_emotion_provenance_and_stale_state(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    renderer = source.split('  const renderPrivateWorldPanel =', 1)[1].split('  const setupInput =', 1)[0]
    css = re.search(r'const mountWorldPage = .*?style.textContent=`(.*?)`;', source, re.S).group(1)
    payload = dict(schema_version='olivia.daily-life.v1', stale=False, refreshing=False,
        current=dict(activity='练完琴，在宿舍休息', location='宿舍', note='练习告一段落。', occurred_at='2026-09-28T08:38:00Z'),
        rhythm=dict(activity='按作息休息', note='节律参考'), projects=[], shared=[], moments=[],
        world=dict(schedule=dict(date='2026-09-28', classes=[dict(start='2026-09-28T09:00:00+08:00', end='2026-09-28T10:00:00+08:00', title='音乐分析')]), meals=[]),
        emotion=dict(status='available', reactions=[dict(reaction='relieved', quote='<img src=x onerror=alert(1)>终于弹顺了', goal_or_need='把曲子弹稳', occurred_at='2026-09-28T08:20:00Z')], concerns=[dict(summary='明天的课堂展示')]))
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome')
        except playwright.Error:
            pytest.skip('Chrome is required for the optional native UI browser check')
        page = browser.new_page(viewport={'width':1200, 'height':900})
        errors=[]
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content('<style>body{background:#101112;color:#eee8de;font:15px/1.65 "Microsoft YaHei",sans-serif;padding:22px}*{box-sizing:border-box}p{margin:8px 0}.text-text-secondary{color:#acb0b4}button{font:inherit}</style><div data-olivia-world-page style="height:820px"><section id="world" data-world-main></section></div>')
        page.add_style_tag(content=css)
        page.evaluate('(value)=>window.payload=value', payload)
        page.add_script_tag(content='''
const text=(tag,value,cls='')=>{const e=document.createElement(tag);e.textContent=value;e.className=cls;return e};
const button=(label,handler)=>{const e=text('button',label);e.onclick=handler;return e};
const actions=()=>document.createElement('div'),stack=actions;
const privateWorldState=c=>c.state,stateLabels={available:'可用'},setDiagnosticDetails=()=>{};
const DAILY_LIFE_PATH='/life', PRIVATE_WORLD_PATH='/relationship';
const requestJson=async path=>path==='/relationship'?{status:'READY',levels:{},relationship_stage:'unknown'}:window.payload;
const requestMutation=async()=>window.payload;
window.setTimeout=()=>0;
window.renderWorld = '''+renderer+';')
        def render():
            page.evaluate("async()=>{await window.renderWorld(document.querySelector('#world'),{state:'available'})}")
        render()
        assert '心情：松了口气' in page.locator('#world').inner_text()
        assert '挂心的事：明天的课堂展示' in page.locator('#world').inner_text()
        concern = '上钢琴专业课时，有一部分内容还没理解；具体疑问尚未记录。'
        page.evaluate('(summary)=>window.payload.emotion.concerns=[{summary}]', concern)
        render()
        assert '挂心的事：' + concern in page.locator('#world').inner_text()
        page.screenshot(path=str(tmp_path/'world-concern-source.png'), full_page=True)
        # A concern quoting the user's message and the user's own shared plan say whose words they are.
        page.evaluate("()=>{window.payload.emotion.concerns=[{summary:'用户说：我明天要面试，好紧张。'}];"
                      "window.payload.shared=[{title:'我周末带你去看海',actor:'user',status:'planned'}]}")
        render()
        assert '挂心的事：你说：我明天要面试，好紧张。' in page.locator('#world').inner_text()
        assert '你说：我周末带你去看海' in page.locator('#world').inner_text()
        page.evaluate("()=>{window.payload.emotion.concerns=[{summary:'明天的课堂展示'}];window.payload.shared=[]}")
        render()
        assert page.locator('#world img').count()==0
        assert '课表计划' in page.locator('#world').inner_text()
        assert '已结束' not in page.locator('#world').inner_text()
        for slot in ('早餐', '午餐', '晚餐'):
            assert slot + ' · 用餐安排待同步' in page.locator('#world').inner_text()
        page.screenshot(path=str(tmp_path/'world-meals-unknown.png'),full_page=True)
        payload['world']['today_activities'] = [
            dict(source_id='practice', occurred_at='2026-09-28T02:30:00Z', activity='练习困难片段', activity_kind='practice', note='放慢速度后把衔接弹顺了一些。'),
            dict(source_id='morning', occurred_at='2026-09-28T00:40:00Z', activity='整理乐谱', activity_kind='study', note='找出今天需要的练习页。'),
            dict(source_id='rest', occurred_at='2026-09-28T03:00:00Z', last_recorded_at='2026-09-28T04:00:00Z', record_count=3, activity='休息', activity_kind='rest', note='安静坐了一会儿。'),
            dict(source_id='yesterday', occurred_at='2026-09-27T06:00:00Z', activity='昨天的旧活动', activity_kind='rest', note='旧记录'),
        ]
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        body = page.locator('#world').inner_text()
        assert body.index('08:40') < body.index('09:00') < body.index('10:30')
        assert '昨天的旧活动' not in body
        assert body.count('11:00–12:00　休息 · 同状态记录') == 1
        activity = page.locator('.olivia-world-agenda-detail').filter(has_text='练习困难片段')
        assert not activity.evaluate('el=>el.open')
        activity.locator('summary').click()
        assert '放慢速度后把衔接弹顺了一些。' in activity.inner_text()
        page.screenshot(path=str(tmp_path/'world-agenda-activities.png'),full_page=True)
        page.get_by_text('11:00–12:00　休息 · 同状态记录', exact=True).scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path/'world-rest-grouped.png'),full_page=True)
        payload['projects'] = [
            dict(id='short', title='明早有课需要早起', detail='昨夜留下的打算', status='planned', deadline_expired=True, updated_at='2026-09-27T18:00:00Z'),
            dict(id='long', title='继续研究音乐与回忆', detail='长期研究', status='planned', deadline_expired=False, updated_at='2026-09-27T18:00:00Z'),
            dict(id='observation', title='正在上课', detail='本次当时观察', status='ongoing', time_scope='transient', updated_at='2026-09-28T06:54:00Z'),
        ]
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        page.get_by_role('tab', name='牵挂与关系').click()
        assert page.get_by_text('继续研究音乐与回忆 · 打算做', exact=True).is_visible()
        expired = page.get_by_text('明早有课需要早起 · 时限已过，进展未确认', exact=True)
        assert not expired.is_visible()
        transient = page.get_by_text('正在上课 · 当时的活动记录', exact=True)
        assert not transient.is_visible()
        page.get_by_text('其他事项与已结束的约定', exact=True).first.click()
        assert expired.is_visible()
        assert transient.is_visible()
        page.get_by_role('tab', name='今天', exact=True).click()
        payload['projects'] = []
        payload['world']['meals'] = [
            dict(slot='breakfast', food='豆浆', status='eaten', stale=False, date='2026-09-28'),
            dict(slot='lunch', food='馄饨', status='eating', stale=True, date='2026-09-28'),
            dict(slot='dinner', food='青菜面', status='planned', stale=False, date='2026-09-28'),
            dict(slot='snack', food='昨天的饼干', status='eaten', stale=False, date='2026-09-27'),
        ]
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        body = page.locator('#world').inner_text()
        assert '早餐 · 已吃豆浆' in body
        assert '午餐 · 上次记录：正在吃馄饨；当前状态待更新' in body
        assert '晚餐 · 计划吃青菜面' in body
        assert '昨天的饼干' not in body
        assert '已吃馄饨' not in body and '这餐没吃' not in body
        payload['world']['meals'][1]['stale'] = False
        payload['world']['meals'].append(dict(slot='snack', food='苹果', status='eaten', date='2026-09-28'))
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        assert '午餐 · 正在吃馄饨' in page.locator('#world').inner_text()
        assert '加餐 · 已吃苹果' in page.locator('#world').inner_text()
        payload['world']['meals'][1]['stale'] = True
        payload['world']['meals'].pop()
        payload['world']['meals'][0].update(status='skipped', food='')
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        assert '早餐 · 这餐没吃' in page.locator('#world').inner_text()
        # The settings-panel variant must tell the same truth as the world page.
        page.locator('#world').evaluate("el=>el.removeAttribute('data-world-main')")
        render()
        assert '早餐 · 这餐没吃' in page.locator('#world').inner_text()
        assert '午餐 · 上次记录：正在吃馄饨；当前状态待更新' in page.locator('#world').inner_text()
        page.locator('#world').evaluate("el=>el.setAttribute('data-world-main','')")
        payload['world']['schedule']['date'] = None
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        for slot in ('早餐', '午餐', '晚餐'):
            assert slot + ' · 当天日期待同步' in page.locator('#world').inner_text()
        assert '馄饨' not in page.locator('#world').inner_text()
        payload['world']['schedule']['date'] = '2026-09-28'
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        page.locator('.olivia-world-emotion-detail summary').click()
        assert '把曲子弹稳' in page.locator('.olivia-world-emotion-detail').inner_text()
        for missing in (None, '', '   '):
            payload['emotion']['reactions'][0]['goal_or_need'] = missing
            page.evaluate('(value)=>window.payload=value', payload)
            render()
            if not page.locator('.olivia-world-emotion-detail').evaluate('el=>el.open'):
                page.locator('.olivia-world-emotion-detail summary').click()
            assert '她在意的需要：尚未明确' in page.locator('.olivia-world-emotion-detail').inner_text()
            assert 'null' not in page.locator('#world').inner_text()
        page.get_by_role('tab',name='牵挂与关系').click()
        original = dict(payload['emotion']['reactions'][0])
        payload['emotion']['reactions'].append({**original, 'occurred_at':'2026-09-28T08:21:00Z'})
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        assert page.locator('.olivia-world-emotion-entry').count() == 1
        assert '相同感受 2 次' in page.locator('.olivia-world-emotion-detail').inner_text()
        page.screenshot(path=str(tmp_path/'world-emotion-grouped.png'),full_page=True)
        payload['emotion']['reactions'].append({**original, 'goal_or_need':'不同的需要'})
        page.evaluate('(value)=>window.payload=value', payload)
        render()
        assert page.locator('.olivia-world-emotion-entry').count() == 2
        render()
        assert page.get_by_role('tab',name='牵挂与关系').get_attribute('aria-selected')=='true'
        page.get_by_role('tab',name='牵挂与关系').press('ArrowRight')
        assert page.get_by_role('tab',name='生活记录').get_attribute('aria-selected')=='true'
        payload['stale']=True
        payload['emotion']['reactions']=[]
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        # No reactions and no current mood read as calm, without system wording.
        assert '心情：平静' in page.locator('#world').inner_text()
        assert '宿舍' not in page.locator('.olivia-world-meta').inner_text()
        assert page.locator('.olivia-world-overview h3').inner_text().endswith('之后还没有新动态')
        assert '作息仅供参考' not in page.locator('#world').inner_text()
        payload['emotion']['status']='unavailable'
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        assert '心情：暂时读不到' in page.locator('#world').inner_text()
        assert '挂心的事：明天的课堂展示' not in page.locator('#world').inner_text()
        payload['emotion'].update(status='available', reactions=[original], current_affect=dict(
            status='available', label='calm', as_of='2026-09-28T08:45:00Z',
            reason='练习后的休息与刚才被理解的对话，让心情渐渐平稳。',
            basis={'kind': 'body', 'source_ids': ['private-source-not-for-display']}))
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        body = page.locator('#world').inner_text()
        assert '心情：平静' in body and '心情：松了口气' not in body
        # The reason is the category JEV chose, never the stored evidence text.
        assert '因为身体和作息的状态' in body
        assert '练习后的休息与刚才被理解的对话' not in body
        assert '判断于' not in body and 'private-source-not-for-display' not in body
        payload['emotion']['current_affect'].update(label='jealous', intensity='low')
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        assert '心情：有点吃醋' in page.locator('#world').inner_text()
        payload['emotion']['current_affect'].update(label='sad', intensity='high')
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        assert '心情：很难过' in page.locator('#world').inner_text()
        payload['emotion']['current_affect'].update(label='calm', intensity='low')
        payload['emotion']['current_affect']['status'] = 'stale'
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        assert '心情：平静' in page.locator('#world').inner_text()
        assert '待更新' not in page.locator('.olivia-world-mood').inner_text()
        for state, label in [('missing','平静'), ('unavailable','暂时读不到')]:
            payload['emotion']['current_affect'].update(status=state,label=None)
            page.evaluate('(value)=>window.payload=value',payload)
            render()
            assert '心情：'+label in page.locator('#world').inner_text()
            assert '心情：松了口气' not in page.locator('#world').inner_text()
        payload['rhythm'] = dict(payload['rhythm'], phase='sleep', planned_rest_window=dict(
            start='2026-09-28T16:00:00+00:00', end='2026-09-29T00:30:00+00:00'))
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        assert page.locator('.olivia-world-overview h3').inner_text()=='已经睡下了'
        assert '她计划 00:00–08:30 休息' in page.locator('.olivia-world-meta').inner_text()
        payload['rhythm']['phase'] = 'interrupted_rest'
        page.evaluate('(value)=>window.payload=value',payload)
        render()
        assert page.locator('.olivia-world-overview h3').inner_text()=='还醒着，在和你聊天'
        payload['rhythm'] = {key: value for key, value in payload['rhythm'].items() if key not in {'phase', 'planned_rest_window'}}
        payload['emotion']['current_affect'].update(status='available',label='calm')
        page.evaluate('(value)=>window.payload=value',payload)
        page.locator('#world').evaluate("el=>el.removeAttribute('data-world-main')")
        render()
        assert '心情：平静' in page.locator('#world').inner_text()
        page.locator('#world').evaluate("el=>el.setAttribute('data-world-main','')")
        render()
        page.get_by_role('tab',name='今天',exact=True).click()
        page.screenshot(path=str(tmp_path/'world-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path/'world-mobile.png'),full_page=True)
        assert not errors
        browser.close()
