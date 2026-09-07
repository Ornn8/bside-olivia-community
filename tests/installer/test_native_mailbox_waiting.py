import json
import shutil
import subprocess

import pytest

from patch_companion_settings import MAIN_JS_0627, _repair_mailbox_write_access
from patch_feapp import MAILBOX_WRITE_ANCHOR_0627
from patch_feapp import _repair_mailbox_waiting_footer


def test_native_waiting_card_stays_visible_and_blocks_click_until_reply():
    # Verbatim footer and sidebar render from native 0.0.9.627. Execute the
    # patched render/event functions, including a direct disabled-button click.
    footer = '''le({__name:"MailBoxFooter",props:{remainingCount:{}},emits:["write"],setup(e,{emit:t}){const{t:s}=fe(),i=t,l=()=>{i("write")};return(a,c)=>{const m=ke,u=Qs;return r(),_("div",C4,[n("div",I4,[n("div",E4,[n("div",T4,[n("div",P4,[k(m,{type:"send",class:"text-headline-m text-primary-2"})]),n("div",M4,[n("div",L4,v(o(s)("mailbox_write_mail_title")),1),n("div",R4,v(o(s)("mailbox_write_mail_remaing",{count:a.remainingCount})),1)])]),n("button",{class:"mail-footer-action-button flex-shrink-0",disabled:a.remainingCount<=0,onClick:l},v(o(s)("mailbox_write_mail")),9,B4)]),k(u)])])}}})'''
    sidebar = 'm.hideWrite?Y("",!0):(r(),F(U4,{key:0,"remaining-count":m.remainingCount,onWrite:l,class:"flex-shrink-0"},null,8,["remaining-count"]))'
    patched = _repair_mailbox_waiting_footer(footer)
    assert _repair_mailbox_waiting_footer(patched) == patched
    script = '''const le=x=>x,fe=()=>({t:x=>x}),r=()=>{},_=(tag,attrs,children)=>({tag,attrs,children}),n=_,k=()=>null,v=String,o=x=>x;
const C4={},I4={},E4={},T4={},P4={},M4={},L4={},R4={},B4={},ke={},Qs={};
const find=x=>x&&x.tag==="button"?x:Array.isArray(x&&x.children)?x.children.map(find).find(Boolean):null;
let clicks=0;const props={waiting:true,remainingCount:99};
const footer=''' + patched + ''';
const render=footer.setup(props,{emit:()=>clicks++});
const waiting=find(render(props));waiting.attrs.onClick();
if(!waiting.attrs.disabled||clicks!==0||waiting.children!=="等待林离回信")throw Error("pending letter can compose");
props.waiting=false;const ready=find(render(props));ready.attrs.onClick();
if(ready.attrs.disabled||clicks!==1)throw Error("delivered letter stays disabled");
const U4={},F=(component,props)=>props,Y=()=>null,l=()=>{};let m={hideWrite:true,remainingCount:99};
const sidebar=()=>''' + _repair_mailbox_waiting_footer(sidebar) + ''';
if(!sidebar()||sidebar().waiting!==true)throw Error("compose card disappeared");
m.hideWrite=false;if(sidebar().waiting!==false)throw Error("card did not recover");'''
    subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)


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
