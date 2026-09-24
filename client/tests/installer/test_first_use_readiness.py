from __future__ import annotations

import shutil
import subprocess
import asyncio

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from original_client_setup_api import LLMSetupError, LLMSetupService


def run_js(source: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, '-e', source], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('state,reason', [('missing', 'SETUP_MEMORY_REQUIRED'), ('ready', 'LLM_SETUP_MEMORY_PREPARING')])
def test_start_using_does_not_complete_before_memory_is_usable(state: str, reason: str) -> None:
    source = BOOTSTRAP_JAVASCRIPT.split('  const finishInitialSetup =', 1)[1].split('  const openDialog =', 1)[0]
    run_js('''const assert=require('assert');
const SETUP_COMPLETE_PATH='complete', MEM0_CAPABILITY_PATH='memory', STATUS_PATH='status';
let completed=false;
const requestSetup=async()=>{completed=true};
const requestCapability=async()=>({state:STATE});
const requestJson=async()=>({capabilities:{memory:{state:'unavailable'}}});
const capabilityState=x=>x.state;
const window={location:{hash:''}};
const finishInitialSetup='''.replace('STATE', repr(state)) + source + '''
(async()=>{await assert.rejects(()=>finishInitialSetup(false),e=>e.code===REASON);
assert.equal(completed,false); assert.equal(window.location.hash,'');})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('REASON', repr(reason)))


def test_cancelled_memory_package_picker_can_be_opened_again() -> None:
    source = BOOTSTRAP_JAVASCRIPT.split('  const renderMem0CapabilityPanel =', 1)[1].split('  const videoCapabilityViewState =', 1)[0]
    run_js('''const assert=require('assert');
class Element {constructor(){this.children=[];} append(...x){this.children.push(...x)}
replaceChildren(...x){this.children=x} setAttribute(){} }
const text=()=>new Element(), stack=()=>new Element(), actions=()=>new Element(),field=()=>new Element();
const formatBytes=()=>'',button=(label,click)=>({label,click}),setButtonsBusy=(items,busy)=>items.forEach(x=>x.disabled=busy);
let calls=0; const MEM0_CAPABILITY_PATH='status',MEM0_CAPABILITY_ACTION_PATH='action';
const requestCapability=async(path)=>path==='status'?{state:'missing'}:(calls++,{status:'CANCELLED'});
let mem0RuntimeProgressStartedAt=null;
const renderMem0CapabilityPanel=''' + source + '''
(async()=>{const panel=new Element();await renderMem0CapabilityPanel(panel);
const visit=n=>n.label?[n]:n.children.flatMap(visit);
const btn=visit(panel).find(x=>x.label.includes('ZIP'));
await btn.click();assert.equal(btn.disabled,false);await btn.click();assert.equal(calls,2);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


@pytest.mark.parametrize('available', [False, True, 'error'])
def test_setup_completion_checks_live_memory_before_persisting(tmp_path, available) -> None:
    def ready():
        if available == 'error':
            raise OSError('unavailable')
        return available

    async def probe(*args):
        pass
    service = LLMSetupService(tmp_path, memory_ready=ready, probe=probe)
    payload = {'base_url': 'http://127.0.0.1:8999/v1', 'model': 'local-model', 'api_key': ''}
    asyncio.run(service.test(payload))
    service.save(payload)
    if available is True:
        service.complete(skipped=False)
        assert service.status()['setup_completed'] is True
    else:
        with pytest.raises(LLMSetupError, match='LLM_SETUP_MEMORY_PREPARING'):
            service.complete(skipped=False)
        assert not service._complete_path.exists()
