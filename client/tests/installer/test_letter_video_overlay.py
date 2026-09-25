"""The enlarged sent letter must not sit in front of reply video playback."""

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_video_preview_stays_above_zoomed_letter():
    playwright = pytest.importorskip("playwright.sync_api")
    rule = next(line for line in BOOTSTRAP_JAVASCRIPT.splitlines()
                if line.strip().startswith('.tp-el-overlay:has(.video-preview-dialog)'))
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except playwright.Error:
            pytest.skip("Microsoft Edge is unavailable")
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.set_content('''<style>
            .tp-el-overlay{position:fixed;inset:0;z-index:2000}
            .video-preview-dialog{position:absolute;left:200px;top:229px;width:800px;height:450px;background:#222}
            .envelope-modal-overlay{position:fixed;inset:0;z-index:9999}
            #letter{position:absolute;left:525px;top:587px;width:516px;height:290px;background:white}
            </style><style>''' + rule + '''</style>
            <div class="tp-el-overlay"><div class="video-preview-dialog" id="video"></div></div>
            <div class="envelope-modal-overlay"><div id="letter"></div></div>''')
        # The paper overlaps the lower part of the 800x450 video rectangle.
        assert page.evaluate("document.elementFromPoint(700, 620)?.id") == "video"
        browser.close()
