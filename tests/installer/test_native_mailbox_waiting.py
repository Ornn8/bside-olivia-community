import json
import shutil
import subprocess

import pytest

from patch_companion_settings import MAIN_JS_0627, _repair_mailbox_write_access
from patch_feapp import MAILBOX_WRITE_ANCHOR_0627


@pytest.mark.parametrize("visibility", [MAILBOX_WRITE_ANCHOR_0627, '"hide-write":!1'])
def test_native_mailbox_waits_for_reply_and_releases_failed_letters(tmp_path, visibility):
    # Verbatim MailboxView render call from the supported native 0.0.9.627 bundle.
    fragment = 'k(V4,{"mail-list":o(h),"selected-mail-id":o(T),"total-count":o(y)+1,"remaining-count":o(f),loading:o(E),'+visibility+',onSelect:A,onWrite:z},null,8,["mail-list","selected-mail-id","total-count","remaining-count","loading","hide-write"])'
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    main.write_text(fragment, encoding="utf-8")
    assert _repair_mailbox_write_access(tmp_path) == "PATCHED"
    patched = main.read_text(encoding="utf-8")
    assert _repair_mailbox_write_access(tmp_path) == "ALREADY_PATCHED"
    node = shutil.which("node")
    assert node
    script = 'const o=x=>x,V4={},T=null,y=0,f=99,E=false,A=()=>{},z=()=>{},p=false,N3=true;let h=[];const k=(component,props)=>props;'
    script += 'const render=()=>'+patched+';const output=[];for(const status of [1,2,3,4,5]){h=[{letterStatus:status}];output.push(render()["hide-write"])}h=[];output.push(render()["hide-write"]);console.log(JSON.stringify(output));'
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [True, True, True, False, False, False]


def test_native_poll_updates_status_even_when_unread_and_media_do_not_change(tmp_path):
    # Existing native poll update condition, with observable update body.
    condition = '(((B=re.received)==null?void 0:B.type)!==((K=Ee.received)==null?void 0:K.type)||re.isUnread!==Ee.isUnread)&&(updated=true)'
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    main.write_text('/*'+MAILBOX_WRITE_ANCHOR_0627+'*/\n'+condition, encoding="utf-8")
    _repair_mailbox_write_access(tmp_path)
    patched = main.read_text(encoding="utf-8")
    script = 'let B,K,updated=false;const re={letterStatus:5,isUnread:false},Ee={letterStatus:1,isUnread:false};'+patched+';if(!updated)throw Error("failed letter remains pending");'
    subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
