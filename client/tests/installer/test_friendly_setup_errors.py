from __future__ import annotations

import shutil
import subprocess

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def run_js(source: str) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, '-e', source], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr


def test_update_errors_are_readable_and_reuse_one_diagnostic_node() -> None:
    helper = BOOTSTRAP_JAVASCRIPT.split('  const setDiagnosticDetails =', 1)[1].split('  const memoryClearFailureMessage =', 1)[0]
    panel = BOOTSTRAP_JAVASCRIPT.split('  const renderLocalUpdatePanel =', 1)[1].split('  const loadDialogData =', 1)[0]
    run_js('''const assert=require('assert');
class Element {constructor(tag){this.tag=tag;this.children=[];this.style={};}
append(...items){for(const item of items){if(item.parentElement){item.parentElement.children=item.parentElement.children.filter(x=>x!==item)}item.parentElement=this;this.children.push(item)}}
replaceChildren(...items){this.children=[];this.append(...items)}setAttribute(){} }
const document={createElement:tag=>new Element(tag)};
const text=(tag,value)=>Object.assign(new Element(tag),{textContent:value});
const actions=()=>new Element('div'),button=(label,click)=>Object.assign(text('button',label),{click});
const setupInput=()=>({wrapper:new Element('label'),input:{value:'a'.repeat(64)}});
const setButtonsBusy=(items,busy)=>items.forEach(x=>x.disabled=busy),confirmAction=async()=>true;
let fail=true;
const requestUpdate=async({action})=>{
if(action==='select')return {status:'SELECTED',package_path:'fixture.oliviapatch'};
if(fail)throw {code:'UPDATE_SYNTHETIC_FAILURE',message:'private path and key must not be shown'};
return {version:'1.3.6'};};
const setDiagnosticDetails=''' + helper + '\nconst renderLocalUpdatePanel=' + panel + '''
(async()=>{const root=new Element('section');renderLocalUpdatePanel(root);
const all=n=>[n,...n.children.flatMap(all)];
const buttons=all(root).filter(x=>x.tag==='button');
for(const label of ['选择补丁并更新','手动校验并安装','回滚上一版本','选择补丁并更新']){
await buttons.find(x=>x.textContent===label).click();
const diagnostics=all(root).filter(x=>x.tag==='details'&&x.__oliviaCodeNode);
assert.equal(diagnostics.length,1);assert.equal(diagnostics[0].hidden,false);
assert.equal(diagnostics[0].__oliviaCodeNode.textContent,'UPDATE_SYNTHETIC_FAILURE');
const plain=all(root).filter(x=>x.tag==='p'&&!diagnostics.includes(x.parentElement)).map(x=>x.textContent).join('');
assert.doesNotMatch(plain,/UPDATE_|private path/);
}
fail=false;await buttons.find(x=>x.textContent==='选择补丁并更新').click();
const diagnostic=all(root).find(x=>x.__oliviaCodeNode);assert.equal(diagnostic.hidden,true);
setDiagnosticDetails(root.children[0],['BAD CODE with secret','VALID_CODE','VALID_CODE']);
assert.equal(root.children[0].__oliviaDiagnosticDetails.__oliviaCodeNode.textContent,'VALID_CODE');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_memory_status_keeps_machine_codes_only_in_details() -> None:
    source = BOOTSTRAP_JAVASCRIPT.split('  const renderCompanionStatus =', 1)[1].split('  const scheduleMemoryStatusRefresh =', 1)[0]
    run_js('''const assert=require('assert');let codes;
const setDiagnosticDetails=(_target,value)=>codes=value;
const renderCompanionStatus=''' + source + '''
const target={dataset:{}};
renderCompanionStatus(target,{memory:{state:'unavailable',reason_code:'MEM0_INIT_IMPORT_FAILED'}});
assert.doesNotMatch(target.textContent,/MEM0_/);assert.match(target.textContent,/尚未准备好/);
assert.deepEqual(codes,['MEM0_INIT_IMPORT_FAILED']);
renderCompanionStatus(target,{memory:{state:'available'}});assert.deepEqual(codes,[]);
''')
