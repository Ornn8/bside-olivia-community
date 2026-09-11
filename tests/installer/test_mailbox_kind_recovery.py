import shutil
import subprocess

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from patch_companion_settings import MAIN_JS_0627, _repair_mailbox_write_access
from patch_feapp import MAILBOX_WRITE_ANCHOR_0627

NATIVE = 'n("div",{class:ae(["mail-item-icon",o(a).iconBgClass])},[k(p,{type:o(a).iconType,class:ae(["text-[24px]",o(a).iconClass])},null,8,["type","class"])],2)'
OLD_GUARD = 'm.mail.received&&m.mail.received.type!=="video"&&["voice_reply","singing_video","voice_song_video"].includes(m.mail.replyKind)'


def run_node(script):
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


def test_missing_custom_element_keeps_native_icon_and_old_patch_migrates(tmp_path):
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    for original in (NATIVE, '(' + OLD_GUARD + '?n("olivia-mail-kind",{kind:m.mail.replyKind},null,8,["kind"]):' + NATIVE + ')'):
        main.write_text(original + ';const fixture={' + MAILBOX_WRITE_ANCHOR_0627 + '};', encoding='utf-8')
        _repair_mailbox_write_access(tmp_path)
        patched = main.read_text(encoding='utf-8')
        _repair_mailbox_write_access(tmp_path)
        assert main.read_text(encoding='utf-8') == patched
        run_node('''const assert=require('assert');const n=(tag)=>tag,k=()=>null,ae=x=>x,o=x=>x,a={},p={};
const m={mail:{received:{type:'text'},replyKind:'voice_reply'}};const window={};
const render=()=>''' + patched.split(';const fixture=')[0] + ''';
assert.equal(render(),'div');window.customElements={get:()=>undefined};assert.equal(render(),'div');
window.customElements.get=()=>class{};
for(const kind of ['voice_reply','singing_video','voice_song_video']){m.mail.replyKind=kind;assert.equal(render(),'olivia-mail-kind')}
m.mail.replyKind='text_letter';assert.equal(render(),'div');
m.mail.replyKind='voice_reply';m.mail.received.type='video';assert.equal(render(),'div');''')


def test_existing_audio_component_does_not_skip_mail_icon_registration():
    section = BOOTSTRAP_JAVASCRIPT.split('  const style = document.createElement', 1)[0] + '\n})();'
    run_node('''const assert=require('assert'),registry=new Map([['olivia-letter-audio',class{}]]);
const customElements={get:n=>registry.get(n),define:(n,c)=>{assert(!registry.has(n));registry.set(n,c)}};
const window={customElements};class HTMLElement{};
''' + section + '''
assert(registry.has('olivia-mail-kind'),'mail icons must register even with existing audio');
''' + section + '''
assert.equal(registry.size,2);''')
