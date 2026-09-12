import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from patch_companion_settings import MAIN_JS_0627, _repair_mailbox_write_access
from patch_feapp import MAILBOX_WRITE_ANCHOR_0627

NATIVE = 'n("div",{class:ae(["mail-item-icon",o(a).iconBgClass])},[k(p,{type:o(a).iconType,class:ae(["text-[24px]",o(a).iconClass])},null,8,["type","class"])],2)'
OLD_GUARD = 'm.mail.received&&m.mail.received.type!=="video"&&["voice_reply","singing_video","voice_song_video"].includes(m.mail.replyKind)'
READY = 'window.customElements&&window.customElements.get("olivia-mail-kind")&&'

# Native contracts, exercised without settings bootstrap or customElements.
REGISTRY = 'function lookup(t){const s={book:"book",send:"send",video:"video",userCenter:Np},i=j(()=>s[t.type]||null);return i.value}'
MAPPING = '''function appearance(s){const u=!s.mail.received,d=s.mail.received?.type;
return s.mail.rejected?{iconType:"circledWarning"}:s.mail.special?{iconType:"fav"}:{iconType:u?"send":d==="video"?"video":"book",iconClass:"",iconBgClass:"native"}}'''


@pytest.mark.parametrize('prefix', [None, '', READY])
def test_native_icons_and_legacy_migration(tmp_path, prefix):
    original = NATIVE if prefix is None else '(' + prefix + OLD_GUARD + '?n("olivia-mail-kind",{kind:m.mail.replyKind},null,8,["kind"]):' + NATIVE + ')'
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    main.write_text(REGISTRY + '\n' + MAPPING + '\nfunction row(a){return ' + original + '};const fixture={' + MAILBOX_WRITE_ANCHOR_0627 + '};', encoding='utf-8')
    _repair_mailbox_write_access(tmp_path)
    patched = main.read_text(encoding='utf-8')
    _repair_mailbox_write_access(tmp_path)
    assert main.read_text(encoding='utf-8') == patched
    assert 'olivia-mail-kind' not in patched
    assert 'customElements' not in patched
    script = '''const assert=require('assert');
const n=(tag,props,children,flag)=>({tag,props,children,flag}),_=n,k=n,r=()=>{},ae=x=>x,o=x=>x,j=f=>({get value(){return f()}}),Np={},p={};
''' + patched.split(';const fixture=')[0] + '''
const s={mail:{received:{type:'text'}}};
for(const [kind,name,color,paths] of [
 ['voice_reply','oliviaVoice','#81796d',1],
 ['singing_video','oliviaSong','#936f79',1],
 ['voice_song_video','oliviaVoiceSong','#958054',2]]){
 s.mail.replyKind=kind;
 const a=appearance(s); assert.equal(a.iconType,name);
 const icon=lookup({type:a.iconType}).render();
 assert.equal(icon.tag,'svg');assert.equal(icon.children.length,paths);
 assert(icon.children.every(x=>x.tag==='path'&&x.props.d));
 const rendered=row(a);assert.equal(rendered.tag,'div');
 assert.equal(rendered.props.style.background,color);
 assert.equal(rendered.flag,6,'class and style must update on reused rows');
 assert.equal(rendered.children[0].props.type,name);
 s.mail.rejected=true;assert.equal(appearance(s).iconType,'circledWarning');
 assert.equal(row(appearance(s)).props.style,undefined);delete s.mail.rejected;
 s.mail.special=true;assert.equal(appearance(s).iconType,'fav');delete s.mail.special;
 s.mail.received=null;assert.equal(appearance(s).iconType,'send');
 assert.equal(row(appearance(s)).props.style,null);
 s.mail.received={type:'video'};assert.equal(appearance(s).iconType,'video');
 assert.equal(row(appearance(s)).props.style,null);
 s.mail.received={type:'text'};
}
for(const kind of ['text_letter','unknown',undefined]){
 s.mail.replyKind=kind;assert.equal(appearance(s).iconType,'book');
 assert.equal(row(appearance(s)).props.style,null);
}
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


def test_mail_icons_do_not_require_settings_bootstrap():
    assert 'olivia-mail-kind' not in BOOTSTRAP_JAVASCRIPT
    assert "customElements.get('olivia-letter-audio')" in BOOTSTRAP_JAVASCRIPT
