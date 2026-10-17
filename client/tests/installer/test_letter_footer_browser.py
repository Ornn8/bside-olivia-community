from pathlib import Path

import pytest

sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright


def test_reading_footer_follows_body_and_short_paper_keeps_bottom():
    css = Path('runtime/letter_stickers/select.js').read_text(encoding='utf8')
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome')
        page = browser.new_page(viewport={'width':1000, 'height':700})
        page.set_content('''<base href="http://localhost/"><style>
            *{box-sizing:border-box}body{margin:0}.mail-responsive-card{width:516px;max-width:100vw}
            .mail-box-reply-content{display:flex;flex-direction:column;container-type:inline-size}
            .absolute{position:absolute}.bottom-2{bottom:8px}.right-8{right:32px}
            .mail-content-body-inner>.mail-responsive-card>.mail-card-content{min-height:0;height:auto;aspect-ratio:16/9}
            </style><div class="mail-content-body-inner"><div class="mail-responsive-card"><div class="mail-card-content mail-box-reply-content mail-box-reply-content-text olivia-letter-flow">
            <div class="olivia-letter-scroll"><div class="olivia-letter-body"></div><div class="olivia-letter-footer">
            <img class="olivia-letter-sticker" alt="" width="100" height="100">
            <div class="olivia-letter-signature">林离</div>
            <div class="absolute bottom-2 right-8">2026-10-18 02:20</div>
            </div></div></div></div></div>''')
        page.add_script_tag(content=css.replace('export function', 'function'))
        for width in [1000, 390]:
            page.set_viewport_size({'width':width, 'height':700})
            for body in ['短信正文', '每一行都应当完整可见。\n' * 120, '缩短正文']:
                page.locator('.olivia-letter-body').evaluate('(e,t)=>e.textContent=t', body)
                page.wait_for_timeout(100)
                page.evaluate("const sc=document.querySelector('.olivia-letter-scroll');sc.scrollTop=sc.scrollHeight")
                result = page.evaluate('''()=>{
                    const paper=document.querySelector('.olivia-letter-flow').getBoundingClientRect();
                    const body=document.querySelector('.olivia-letter-body').getBoundingClientRect();
                    const footer=document.querySelector('.olivia-letter-footer').getBoundingClientRect();
                    return {gap:footer.top-body.bottom,bottom:paper.bottom-footer.bottom,height:paper.height,
                        long:document.querySelector('.olivia-letter-scroll').scrollTop>0,width:paper.width,
                        overflow:document.documentElement.scrollWidth>innerWidth};}''')
                assert result['gap'] >= 0
                assert abs(result['bottom']) < 1
                assert result['long'] == (len(body)>100)
                assert not result['overflow']
                assert abs(result['height']-result['width']*9/16) < 1
        browser.close()
