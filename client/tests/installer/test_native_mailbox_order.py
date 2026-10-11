import shutil
import subprocess

import pytest

from patch_companion_settings import MAIN_JS_0627, _repair_mailbox_write_access
from patch_feapp import MAILBOX_WRITE_REPLACEMENT_0627


def run_patched(tmp_path, source):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    main.write_text(source + '\n/*' + MAILBOX_WRITE_REPLACEMENT_0627 + '*/', encoding='utf-8')
    _repair_mailbox_write_access(tmp_path)
    once = main.read_bytes()
    _repair_mailbox_write_access(tmp_path)
    assert main.read_bytes() == once
    result = subprocess.run([node, str(main)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_poll_preserves_server_order_and_cached_detail(tmp_path):
    # Native polling inserts each missing row at the front. The server returns
    # newest first, including imported rows without timestamps at the end.
    run_patched(tmp_path, r'''
const assert=require('node:assert/strict');
const t={value:[]}; let ue=[];
const N=()=>{},z=async()=>{};
async function poll(){
for(const re of ue){const ye=t.value.findIndex(Ee=>Ee.id===re.id);
if(ye===-1)t.value.unshift(re);else{const Ee=t.value[ye];
true&&(Ee.detailLoaded?await z(re.id):t.value[ye]=re)}}N()}
const ids=()=>t.value.map(row=>row.id);
(async()=>{
  ue=[{id:'newest'},{id:'older'},{id:'unknown-a'},{id:'unknown-b'}];
  await poll(); assert.deepEqual(ids(),ue.map(row=>row.id),'cold poll reversed');
  const detail={id:'newest',detailLoaded:true,sent:{content:'cached detail'}};
  t.value[0]=detail;
  ue=[{id:'latest-a'},{id:'latest-b'},...ue];
  await poll(); assert.deepEqual(ids(),ue.map(row=>row.id),'batch insertion reversed');
  assert.equal(t.value[2],detail,'loaded detail lost');
  t.value.reverse();
  await poll(); assert.deepEqual(ids(),ue.map(row=>row.id),'old reversed state persisted');
  t.value.push({id:'older-page-a'},{id:'older-page-b'});
  await poll(); assert.deepEqual(ids(),[...ue.map(row=>row.id),'older-page-a','older-page-b']);
  await poll(); assert.equal(new Set(ids()).size,t.value.length,'duplicate rows');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_unknown_mail_time_reaches_both_cards_as_null(tmp_path):
    run_patched(tmp_path, r'''
const assert=require('node:assert/strict');
function cards(i,x){var G,M,P,E;return[
{timestamp:((G=i.mail.received)==null?void 0:G.timestamp)??0,type:'text'},
{timestamp:((P=i.mail.sent)==null?void 0:P.timestamp)??0,type:'text'},
{timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??0,type:'text'}
]}
const mail={received:{timestamp:null},sent:{timestamp:null}};
assert.deepEqual(cards({mail},{selectedMail:mail}).map(c=>c.timestamp),[null,null,null]);
mail.received.timestamp=mail.sent.timestamp=1791522000000;
assert.deepEqual(cards({mail},{selectedMail:mail}).map(c=>c.timestamp),Array(3).fill(1791522000000));
''')
