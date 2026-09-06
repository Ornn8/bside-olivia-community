import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


@pytest.mark.parametrize("code,expected", [
    ("VIDEO_OFFLINE_PICKER_UNAVAILABLE", "VIDEO_OFFLINE_PICKER_UNAVAILABLE"),
    ("private/path/key", "CAPABILITY_UNAVAILABLE"),
])
def test_capability_request_keeps_only_safe_error_code(code, expected):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    source = "const requestCapability =" + BOOTSTRAP_JAVASCRIPT.split(
        "  const requestCapability =", 1
    )[1].split("  const requestUpdate =", 1)[0]
    harness = r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const context={URL,AbortController,apiBase:'http://127.0.0.1:8899',
 window:{setTimeout,clearTimeout},MEM0_CAPABILITY_PATH:'/memory',
 fetch:async()=>({ok:false,json:async()=>({error_code:process.argv[1]})})};
vm.runInNewContext(fs.readFileSync(0,'utf8')+';globalThis.request=requestCapability;',context);
context.request('/video').then(()=>{throw Error('expected failure');},error=>{
 assert.equal(error.code,process.argv[2]);
}).catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", harness, code, expected], input=source,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
