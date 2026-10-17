"""Render the actual injected manager with synthetic data, without an app/profile.

Run from client: python scripts/preview_letter_manage.py --output <directory>
Requires Playwright and its Chromium, or --channel msedge for installed Edge.
"""
import argparse
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--channel')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = BOOTSTRAP_JAVASCRIPT
    helpers = source[source.index('  const text = (tag, value, className) =>'):source.index('  const setButtonsBusy =')]
    start = source.index('  const actions = () =>')
    helpers += source[start:start + source[start:].index('\n  };') + 6]
    manager = source[source.index('  const openLetterDeletion ='):source.index('  const mountMainNavigation =')]
    fixture = r'''
window.requests=[];
window.letters=Array.from({length:30},(_,i)=>({
  letter_id:'synthetic-'+i, created_at:1792230000-i*86400,
  summary:['今天路过那家花店','你还记得我喜欢什么口味吗','早上好啊','窗外的雨停了','想和你分享的小事'][i%5],
  content:['今天路过那家花店','你还记得我喜欢什么口味吗','早上好啊','窗外的雨停了','想和你分享的小事'][i%5]+'\n\n今天走了一条和平时不同的路，经过河边的时候，风很舒服。看到这片安静的风景，就想写信告诉你。\n\n这是用于界面预览的合成信件。',
  reply_text:'收到你的来信了\n\n我喜欢听你讲这些小事。下次路过的时候，也替我看看那里的花吧。\n\n林离',
  origin:i===4?'proactive':'user'
}));
window.trash=[{id:'synthetic-trash',letter_id:'synthetic-archived',part:'user',summary:'周末去河边散步吧',expires_at:1794822000,status:'deleted'}];
const routeRequest=async(path,body,options)=>{
 if(path.endsWith('/delete')){window.requests.push(body);const row=window.letters.find(x=>x.letter_id===body.letter_id);const field=body.part==='user'?'content':'reply_text';window.trash.push({id:body.letter_id+body.part,letter_id:body.letter_id,part:body.part,summary:row[field],expires_at:1794822000,status:'deleted'});row[field]='';return {}}
 if(path.endsWith('/restore')){window.requests.push(body);window.trash=[];return {}}
 if(path.includes('/deletion/list'))return {items:window.trash};
 if(path.includes('/detail'))return window.letters.find(x=>x.letter_id===new URLSearchParams(path.split('?')[1]).get('letter_id'));
 return {list:window.letters};
};
'''
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel)
        page = browser.new_page(viewport={'width':1280, 'height':900}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', lambda route: route.abort())
        page.set_content('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
          *{box-sizing:border-box} body{margin:0;background:#18191c;color:#ede9e1;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}
          header{padding:26px 40px;border-bottom:1px solid #414143;font-size:20px} main{padding:40px;color:#bcb7ad}
          button{font:inherit} .synthetic{position:fixed;bottom:12px;left:20px;font-size:12px;color:#aaa}
          </style><header>信箱　　世界　　物品栏　　曲库</header><main>我的信箱</main>
          <span class="synthetic">合成信件 · 管理窗口预览（背景为测试外壳）</span></html>''')
        page.add_script_tag(content=helpers + fixture + manager + '\nwindow.openManager=openLetterDeletion;')
        page.evaluate('openManager()')
        dialog = page.locator('[data-letter-deletion]')
        assert dialog.locator('input').count() == 0
        box = dialog.bounding_box()
        assert abs(box['x'] * 2 + box['width'] - 1280) < 2
        assert abs(box['y'] * 2 + box['height'] - 900) < 2

        def shot(name):
            page.screenshot(path=str(args.output / (name + '.png')))

        shot('01-normal-list')
        for index in range(3):
            dialog.locator('.lm-list > div').nth(index).get_by_role('button').click()
        for index in (0, 2, 4):
            dialog.locator('input').nth(index).check()
        assert dialog.locator('.lm-count').inner_text() == '已选 3 封'
        shot('02-three-selected')
        dialog.get_by_role('button', name='删除所选').click()
        prompt = page.locator('[data-letter-confirm]')
        assert prompt.is_visible()
        assert prompt.get_by_role('button', name='取消').evaluate('(el)=>el===document.activeElement')
        shot('03-confirmation')
        page.keyboard.press('Escape')
        assert dialog.is_visible() and not prompt.count()
        assert not page.evaluate('requests.length')
        dialog.get_by_role('button', name='删除所选').click()
        prompt.get_by_role('button', name='确定').click()
        page.wait_for_function('requests.length === 3')
        assert page.evaluate('letters[0].reply_text.length > 0')
        dialog.get_by_role('button', name='最近删除').click()
        shot('04-recently-deleted')
        dialog.get_by_role('button', name='恢复', exact=True).first.click()
        assert '会一起恢复' in prompt.inner_text()
        prompt.get_by_role('button', name='取消').click()
        page.evaluate('window.letters=[]')
        dialog.get_by_role('button', name='返回信箱').click()
        assert dialog.get_by_text('信箱里还没有信件。').is_visible()
        shot('05-empty')
        page.evaluate("window.letters=Array.from({length:40},(_,i)=>({letter_id:'long-'+i,created_at:1792230000-i*86400,summary:'第 '+(i+1)+' 封合成信件：今天也想和你聊聊天',content:'第 '+(i+1)+' 封合成信件：今天也想和你聊聊天',reply_text:'她的回信：愿你今天过得轻松一些。'}))")
        dialog.get_by_role('button', name='最近删除').click()
        dialog.get_by_role('button', name='返回信箱').click()
        page.set_viewport_size({'width':390, 'height':780})
        dialog.locator('.lm-title').first.click()
        assert dialog.evaluate('(el)=>el.scrollWidth<=el.clientWidth')
        shot('06-narrow')
        page.set_viewport_size({'width':1280, 'height':900})
        dialog.locator('.lm-list').evaluate('(el)=>el.scrollTop=1700')
        assert dialog.locator('.lm-list').evaluate('(el)=>el.scrollTop') > 0
        assert dialog.get_by_role('button', name='删除所选').is_visible()
        shot('07-long-list-scrolled')
        assert not errors, errors
        browser.close()
    print('Seven synthetic screenshots saved; preview, batch deletion, cancel, restore confirmation, centering and narrow layout checks passed.')


if __name__ == '__main__':
    main()
