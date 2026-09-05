import subprocess
import shutil
import pytest
from patch_companion_settings import _repair_mailbox_write_access, MAIN_JS_0627
from patch_feapp import MAILBOX_WRITE_REPLACEMENT_0627


def test_fresh_native_store_and_account_reset_keep_local_quota(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    code = '''const b=value=>({value}),fe=()=>({t:()=>{}}),st=(name,fn)=>fn();
const R=()=>{};
const uo=st("mailbox",()=>{const{t:e}=fe(),t=b([]),s=b(0),i=b(0),m=b(0),c=b(false);
function O(){R(),t.value=[],s.value=0,i.value=0,m.value=0,c.value=true}
return {s,O}});
if(uo.s.value!==99) throw Error("fresh quota is zero");
uo.O(); if(uo.s.value!==99) throw Error("reset quota is zero");
'''
    main.write_text(code + '\n/*' + MAILBOX_WRITE_REPLACEMENT_0627 + '*/', encoding='utf-8')
    assert _repair_mailbox_write_access(tmp_path) == 'PATCHED'
    assert _repair_mailbox_write_access(tmp_path) == 'ALREADY_PATCHED'
    result = subprocess.run([node, str(main)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
