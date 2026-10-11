import pytest

from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT


@pytest.mark.parametrize('has_saved,replacement', [(True, ''), (True, '987654321'), (False, '')])
def test_saved_qq_reconnect_button_uses_explicit_binding_without_revealing_owner(has_saved, replacement, tmp_path):
    pw = pytest.importorskip('playwright.sync_api')
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome')
        page = browser.new_page(viewport={'width': 1100, 'height': 900})
        page.set_default_timeout(3000)
        page.set_content('<style>body{background:#111;color:#eee;font:15px sans-serif;padding:25px}</style><main><div data-olivia-group-body="chat"></div></main>')
        page.evaluate('''(hasSaved) => {
          window.requests = [];
          window.fetch = async (url, options) => {
            if (String(url).endsWith('/configure')) {
              window.requests.push(JSON.parse(options.body));
              return {ok:true, json:async () => ({status:'CONNECTING'})};
            }
            return {ok:true, json:async () => ({selected_channels:['qq'], configured:{qq:hasSaved},
              listeners:{qq:hasSaved ? 'CONNECTED' : 'SETUP_REQUIRED'},
              qq:hasSaved ? {state:'CONNECTED', owner_masked:'*****4321', can_reuse_owner:true} : {state:'IDLE'},
              napcat:{state:'ONEBOT_READY',installed:true}})};
          };
        }''', has_saved)
        page.evaluate('(js) => {const s=document.createElement("script");s.dataset.apiBase="http://127.0.0.1:8899";s.textContent=js;document.body.append(s)}', PERSONAL_CHAT_SETUP_JAVASCRIPT)
        owner = page.get_by_label('用于和机器人聊天的个人 QQ 号')
        owner.wait_for()
        assert owner.input_value() == ''
        if replacement:
            owner.fill(replacement)
        label = '使用已保存QQ连接' if has_saved and not replacement else '连接并保存'
        button = page.get_by_role('button', name=label, exact=True)
        if has_saved:
            assert button.is_enabled()
            button.click()
            page.wait_for_function('window.requests.length === 1')
            assert page.evaluate('window.requests[0]') == ({'managed': True, 'reuse_saved_owner': True}
                if not replacement else {'managed': True, 'owner': replacement})
        else:
            assert button.is_disabled()
            assert page.evaluate('window.requests') == []
        assert '123454321' not in page.content()
        page.screenshot(path=str(tmp_path / 'saved-owner-reconnect.png'), full_page=True)
        browser.close()


@pytest.mark.parametrize("outcome", ["READY_RESTART", "CONNECTING", "FAILED"])
def test_qq_save_keeps_draft_and_reports_actual_outcome(outcome, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.set_content('<style>body{background:#111;color:#eee;font:15px sans-serif;padding:25px}</style><main><div data-olivia-group-body="chat"></div></main>')
        page.evaluate('''() => {
          window.saved = false;
          window.fetch = async (url, options) => {
            if (String(url).endsWith('/configure')) return new Promise(resolve => { window.finishSave = () => {
              window.saved = window.outcome !== 'FAILED';
              resolve({ok: window.saved, json: async () => window.saved ? {status:window.outcome} : {error:'QQ_LOGIN_UNAVAILABLE'}});
            }; });
            return {ok:true, json:async () => ({selected_channels:['qq'], configured:{qq:window.saved},
              listeners:{qq:window.saved ? window.outcome === 'CONNECTING' ? 'CONNECTED' : 'CONFIGURED_RESTART' : 'SETUP_REQUIRED'},
              qq:window.saved ? {state:window.outcome,owner_masked:'*****4321'} : {state:'IDLE'},
              napcat:{state:'ONEBOT_READY',installed:true}})};
          };
        }''')
        page.evaluate('(v) => window.outcome=v', outcome)
        page.evaluate('(js) => {const s=document.createElement("script");s.dataset.apiBase="http://127.0.0.1:8899";s.textContent=js;document.body.append(s)}', PERSONAL_CHAT_SETUP_JAVASCRIPT)
        owner = page.get_by_label('用于和机器人聊天的个人 QQ 号')
        owner.fill('987654321')
        page.get_by_role('button', name='连接并保存', exact=True).click()
        assert page.get_by_role('button', name='正在验证并保存…').is_disabled()
        assert owner.is_disabled()
        page.evaluate('() => window.finishSave()')
        page.get_by_role('button', name='连接并保存', exact=True).wait_for()
        assert owner.input_value() == '987654321'
        assert owner.is_enabled()
        if outcome == 'FAILED':
            assert page.get_by_text('QQ_LOGIN_UNAVAILABLE', exact=True).is_visible()
            assert page.get_by_role('status').count() == 0
        else:
            feedback = page.get_by_role('status').inner_text()
            assert '已保存聊天 QQ：*****4321' in feedback
            assert ('请重启 Olivia 后生效' in feedback) == (outcome == 'READY_RESTART')
            assert ('接收连接已建立' in feedback) == (outcome == 'CONNECTING')
        page.screenshot(path=str(tmp_path / f'qq-save-{outcome}.png'), full_page=True)
        browser.close()
