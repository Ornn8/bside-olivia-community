"""Browser regression for attachments inside the native fixed-height flex card."""
import io

import pytest
from PIL import Image

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize('status', ['COMPLETED', 'FAILED', 'RETRY_PENDING'])
def test_photo_follows_fixed_paper_without_covering_body(status):
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    start = source.index("  if (typeof customElements")
    component = source[start:source.index('  const text =', start)]
    css = '\n'.join(line for line in source.splitlines()
                    if line.strip().startswith(('.mail-box-reply-content', '.mail-responsive-card:has(olivia-photo', '.olivia-photo-stack', 'olivia-photo', '.olivia-letter-photo-print')))
    image = io.BytesIO()
    Image.new('RGB', (768, 1024), 'gray').save(image, 'PNG')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='msedge', headless=True)
        except playwright.Error:
            pytest.skip('Microsoft Edge is unavailable')
        page = browser.new_page()
        errors = []
        acknowledgements = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: acknowledgements.append(request.url) if '/toy/image/ack' in request.url else None)
        page.route('http://photo.test/**', lambda route: route.fulfill(
            content_type='image/png', body=image.getvalue()) if '/media/' in route.request.url else route.fulfill(
                headers={'Access-Control-Allow-Origin': '*'}, json={'code': 0, 'data': {'imageStatus': status, 'imageErrorCode': 'GPU_AUTH_FAILED',
                                        'replyImageUrl': 'http://photo.test/toy/media/photo.png'}}))
        page.route('http://photo.test/', lambda route: route.fulfill(content_type='text/html', body='<html></html>'))
        page.goto('http://photo.test/')
        page.set_content('''<style>
          #stack{height:720px;display:flex;flex-direction:column}
          .mail-responsive-card{width:650px;height:365px;flex:1 0 290px;aspect-ratio:16/9}
          .mail-box-reply-content{display:flex;flex-direction:column;position:relative;width:650px;height:365px;box-sizing:border-box;background:#f3eee4}
          textarea{height:230px;margin:20px 24px;width:calc(100% - 48px);box-sizing:border-box;font-size:16px;line-height:24px}
          #date{position:absolute;bottom:20px;right:20px}
          #native-expand{position:absolute;bottom:8px;right:12px;width:32px;height:32px;z-index:2}
        </style><style>''' + css + '''</style>
          <div id="stack"><div class="mail-responsive-card"><div class="mail-box-reply-content mail-box-reply-content-text">
          <textarea class="mail-box-reply-content-textarea" readonly></textarea><div id="date">日期</div></div><olivia-photo letter-id="test"></olivia-photo><button id="native-expand">展开</button></div>
          <div id="next">下一封信</div></div>''')
        page.locator('textarea').evaluate('(el)=>el.value="完整正文。".repeat(300)')
        page.add_script_tag(content="const apiBase='http://photo.test';" + component)
        page.wait_for_timeout(300)
        assert not errors, errors
        if status == 'COMPLETED':
            page.wait_for_function("document.querySelector('olivia-photo img')?.naturalWidth > 0")
        else:
            page.wait_for_function("document.querySelector('olivia-photo').textContent.length > 0")
        for width in (650, 320):
            page.locator('.mail-responsive-card').evaluate('(el,w)=>el.style.width=w+"px"', width)
            page.locator('.mail-box-reply-content').evaluate('(el,w)=>el.style.width=w+"px"', width)
            page.wait_for_timeout(100)
            boxes = page.evaluate('''()=>{
              const b=document.querySelector('textarea'), p=document.querySelector('olivia-photo');
              return {body:b.getBoundingClientRect().toJSON(), photo:p.getBoundingClientRect().toJSON(),
                paper:b.parentElement.getBoundingClientRect().toJSON(), next:document.querySelector('#next').getBoundingClientRect().top,
                date:document.querySelector('#date').getBoundingClientRect().top};
            }''')
            assert boxes['paper']['height'] == 365, 'Attachments must not stretch the paper'
            assert boxes['body']['height'] == 230, 'Attachments must not squeeze the body'
            if status == 'COMPLETED':
                native = page.locator('#native-expand').bounding_box()
                assert boxes['photo']['right'] + 8 <= native['x'], 'Photo tab must leave room for the native corner action'
                assert boxes['photo']['top'] < boxes['paper']['bottom'], 'Closed photo must be tucked under the paper'
                assert 12 <= boxes['photo']['bottom'] - boxes['paper']['bottom'] <= 44, 'Only one small photo corner should peek out'
                assert boxes['next'] - boxes['paper']['bottom'] <= 60, 'Tucked photos must not add a full row'
                assert page.locator('olivia-photo img').is_visible(), 'The corner must show a real photo, not just a text button'
                assert not page.locator('olivia-photo img').evaluate('''img=>{const r=img.getBoundingClientRect();return img===document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)}'''), 'Paper must cover the main body of the closed photo'
            else:
                assert boxes['photo']['top'] >= boxes['paper']['bottom']
            assert boxes['next'] >= boxes['photo']['bottom']
        assert page.locator('textarea').evaluate('(el)=>{el.scrollTop=el.scrollHeight;return el.scrollTop>0}'), 'Long letters remain scrollable'
        if status == 'COMPLETED':
            button = page.locator('olivia-photo button')
            assert not acknowledgements, 'A tucked photo has not been opened yet'
            def open_corner():
                page.wait_for_timeout(320)
                point = page.locator('olivia-photo img').evaluate('''img=>{
                  const r=img.getBoundingClientRect(),paper=img.closest('.mail-responsive-card').querySelector('.mail-box-reply-content').getBoundingClientRect();
                  for(let y=paper.bottom+4;y<r.bottom;y+=3)for(let x=r.left+4;x<r.right;x+=3)
                    if(document.elementFromPoint(x,y)===img)return {x,y};
                  return null;
                }''')
                assert point, 'A real image corner must remain visible and clickable below the paper'
                page.mouse.click(point['x'], point['y'])
            with page.expect_request('**/toy/image/ack'):
                open_corner()
            assert button.get_attribute('aria-expanded') == 'true'
            assert page.locator('olivia-photo').evaluate('el=>el.getAnimations().length') > 0
            page.wait_for_timeout(320)
            assert page.locator('olivia-photo img').is_visible()
            assert page.locator('olivia-photo img').evaluate('''img=>{const r=img.getBoundingClientRect();return img.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}'''), 'Opened photo must be above the paper'
            page.keyboard.press('Escape')
            assert button.get_attribute('aria-expanded') == 'false'
            open_corner()
            page.get_by_text('收起照片', exact=True).click()
            assert button.get_attribute('aria-expanded') == 'false'
            open_corner()
            page.locator('#next').click()
            assert button.get_attribute('aria-expanded') == 'false'
        if status == 'FAILED':
            assert '验证失败' in page.locator('olivia-photo').inner_text()
        if status == 'RETRY_PENDING':
            assert '自动重试' in page.locator('olivia-photo').inner_text()
        browser.close()


def test_pictures_in_the_users_letter_lie_under_the_users_paper_like_her_photo():
    playwright = pytest.importorskip('playwright.sync_api')
    source = BOOTSTRAP_JAVASCRIPT
    start = source.index("  if (typeof customElements")
    component = source[start:source.index('  const text =', start)]
    css = '\n'.join(line for line in source.splitlines()
                    if line.strip().startswith(('.mail-box-reply-content', '.mail-responsive-card:has(olivia-photo', '.olivia-photo-stack', 'olivia-photo', '.olivia-letter-photo-print')))
    image = io.BytesIO()
    Image.new('RGB', (1024, 768), 'gray').save(image, 'PNG')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='msedge', headless=True)
        except playwright.Error:
            pytest.skip('Microsoft Edge is unavailable')
        page = browser.new_page()
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url))
        page.route('http://photo.test/toy/media/**', lambda route: route.fulfill(content_type='image/png', body=image.getvalue()))
        page.route('http://photo.test/', lambda route: route.fulfill(content_type='text/html', body='<html></html>'))
        page.goto('http://photo.test/')
        names = ['letter-image-' + str(index) * 32 + '.png' for index in range(2)]
        page.set_content('''<style>
          .mail-responsive-card{width:650px;height:365px;flex:1 0 290px;aspect-ratio:16/9}
          .mail-box-write-content{display:flex;flex-direction:column;position:relative;width:650px;height:365px;box-sizing:border-box;background:#f3eee4}
          textarea{height:230px;margin:20px 24px;width:calc(100% - 48px);box-sizing:border-box}
        </style><style>''' + css + '''</style>
          <div id="stack" style="display:flex;flex-direction:column"><div class="mail-responsive-card"><div class="mail-box-write-content mail-card-content">
          <textarea class="mail-box-write-content-textarea" readonly>我的信</textarea></div>'''
          + ''.join(f'<olivia-photo src="http://photo.test/toy/media/{name}" data-index="{index}"></olivia-photo>'
                    for index, name in enumerate(names))
          + '''<olivia-photo id="foreign" src="http://elsewhere.test/toy/media/x.png" data-index="2"></olivia-photo></div>
          <div id="next">下一封信</div></div>''')
        page.add_script_tag(content="const t=(zh)=>zh;const apiBase='http://photo.test';" + component)
        page.wait_for_function("[...document.querySelectorAll('olivia-photo[data-index=\"0\"] img, olivia-photo[data-index=\"1\"] img')].filter(img=>img.naturalWidth>0).length===2")
        assert not errors, errors
        assert not [url for url in requests if '/toy/image/' in url], 'A picture the user sent needs no status or delivery receipt'
        assert page.locator('#foreign').evaluate('el=>el.childElementCount') == 0, 'Only the local service may supply a picture'
        boxes = page.evaluate('''()=>[...document.querySelectorAll('olivia-photo[src^="http://photo.test"]')].map(p=>p.getBoundingClientRect().toJSON())''')
        paper = page.locator('.mail-box-write-content').bounding_box()
        assert paper['height'] == 365, 'Pictures must not stretch the paper'
        for box in boxes:
            assert box['top'] < paper['y'] + paper['height'], 'Closed pictures are tucked under the paper'
            assert 12 <= box['bottom'] - (paper['y'] + paper['height']) <= 44, 'Only a corner peeks out'
        assert boxes[1]['right'] < boxes[0]['right'] - 40, 'Each further picture lies a little further along'
        second = page.locator('olivia-photo[data-index="1"]')
        point = second.locator('img').evaluate('''img=>{
          const r=img.getBoundingClientRect(),bottom=document.querySelector('.mail-box-write-content').getBoundingClientRect().bottom;
          for(let y=bottom+4;y<r.bottom;y+=3)for(let x=r.left+4;x<r.right;x+=3)
            if(document.elementFromPoint(x,y)===img)return {x,y};
          return null;
        }''')
        assert point, 'Each picture keeps a visible corner to click'
        page.wait_for_timeout(320)
        page.mouse.click(point['x'], point['y'])
        page.wait_for_timeout(320)
        assert second.locator('button').get_attribute('aria-expanded') == 'true'
        assert page.locator('olivia-photo[data-index="0"] button').get_attribute('aria-expanded') == 'false'
        assert second.locator('img').evaluate('''img=>{const r=img.getBoundingClientRect();return img.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}'''), 'Opened picture is above the paper'
        page.keyboard.press('Escape')
        assert second.locator('button').get_attribute('aria-expanded') == 'false'
        assert not [url for url in requests if '/toy/image/' in url]
        browser.close()
