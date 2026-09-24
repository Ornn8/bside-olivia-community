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
                    if line.strip().startswith(('.mail-box-reply-content', '.mail-responsive-card:has(olivia-photo', 'olivia-photo', '.olivia-letter-photo-print')))
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
          .mail-box-reply-content{display:flex;flex-direction:column;position:relative;width:650px;height:365px;box-sizing:border-box}
          textarea{height:230px;margin:20px 24px;width:calc(100% - 48px);box-sizing:border-box;font-size:16px;line-height:24px}
          #date{position:absolute;bottom:20px;right:20px}
        </style><style>''' + css + '''</style>
          <div id="stack"><div class="mail-responsive-card"><div class="mail-box-reply-content mail-box-reply-content-text">
          <textarea class="mail-box-reply-content-textarea" readonly></textarea><div id="date">日期</div></div><olivia-photo letter-id="test"></olivia-photo></div>
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
                assert boxes['photo']['top'] < boxes['paper']['bottom'] < boxes['photo']['bottom']
                assert boxes['next'] - boxes['paper']['bottom'] <= 60, 'Tucked photos must not add a full row'
            else:
                assert boxes['photo']['top'] >= boxes['paper']['bottom']
            assert boxes['next'] >= boxes['photo']['bottom']
        assert page.locator('textarea').evaluate('(el)=>{el.scrollTop=el.scrollHeight;return el.scrollTop>0}'), 'Long letters remain scrollable'
        if status == 'COMPLETED':
            button = page.locator('olivia-photo button')
            assert not acknowledgements, 'A tucked photo has not been opened yet'
            with page.expect_request('**/toy/image/ack'):
                page.get_by_text('随信附照', exact=True).click()
            assert button.get_attribute('aria-expanded') == 'true'
            assert page.locator('olivia-photo img').evaluate('''img=>{const r=img.getBoundingClientRect();return img.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}'''), 'Opened photo must be above the paper'
            page.keyboard.press('Escape')
            assert button.get_attribute('aria-expanded') == 'false'
            page.get_by_text('随信附照', exact=True).click()
            page.get_by_text('点击收起', exact=True).click()
            assert button.get_attribute('aria-expanded') == 'false'
            page.get_by_text('随信附照', exact=True).click()
            page.locator('#next').click()
            assert button.get_attribute('aria-expanded') == 'false'
        if status == 'FAILED':
            assert '验证失败' in page.locator('olivia-photo').inner_text()
        if status == 'RETRY_PENDING':
            assert '自动重试' in page.locator('olivia-photo').inner_text()
        browser.close()
