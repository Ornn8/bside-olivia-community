from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from patch_companion_settings import (
    MAILBOX_WRITE_ANCHOR_0627,
    MAIN_JS_0627,
    _repair_mailbox_write_access,
)


# A synthetic shell around the supported native patch anchors; no client assets.
_WIDGET_GATE = (
    'e.isOfflineMode&&(l.value.mailWidget!==!1&&(l.value.mailWidget=!1),'
    'l.value.musicWidget!==!1&&(l.value.musicWidget=!1))'
)
_SOURCE = '''
Te.interceptors.request.use(e=>{const t=Ie();if(t.isOfflineMode)throw new Ol(e);return e});
function enforceWidgets(){const e=state,l={value:store.settingsData};
const m=()=>{WIDGET_GATE};m()}
function desktopItems(){const ee={items:[{id:"mail-widget"},{id:"music-widget"},{id:"other"}]};
const X=["mail-widget","music-widget"];const Le=ee.items.filter(tt=>!X.includes(tt.id));return Le.map(x=>x.id)}
function login(){const s=state;
s.isOfflineMode||(s.appMode===Se.PRO?Lt().proRestoreFromApi():s.appMode===Se.LITE&&(Lt().liteStartPoll(),uo().startPolling()))}
function applyClientConfig(offline=true){const B={offlineMode:offline},l=state,
E={get value(){return state.isOfflineMode}},A=()=>{state.isOfflineMode=offline;Te.defaults.baseURL=apiBase+"toy"};
B&&A(B),B&&!E.value}
function userChoice(value){const d={mailWidget:value};Object.assign(store.settingsData,d);
Object.keys(d).length>0&&(Fm(d),0)}
'''.replace("WIDGET_GATE", _WIDGET_GATE)

_HARNESS = r'''
const assert=require("node:assert/strict");
let state={isOfflineMode:true,appMode:"lite"};
const Se={LITE:"lite",PRO:"pro"};const Ie=()=>state;
let apiBase="http://127.0.0.1:18999/",managed=true;
const document={querySelector:()=>managed?{dataset:{apiBase}}:null};
const window={};const Ol=Error;
const Te={defaults:{baseURL:"http://127.0.0.1:18999/toy"},interceptors:{request:{use(){}},response:{use(){}}}};
let native={mailWidget:false,musicWidget:false},saveFails=false,readbackFails=false,waitForSave=null;
const writes=[],updates=[],values=new Map();
let storageFails=false;
const localStorage={getItem:k=>values.get(k)??null,setItem(k,v){if(storageFails)throw Error("storage");values.set(k,v)},removeItem:k=>values.delete(k)};
const Fm=async value=>{writes.push(value);if(waitForSave)await waitForSave;if(saveFails)throw Error("save");if(!readbackFails)Object.assign(native,value);return {}};
const jm=async()=>({...native});
let store={settingsData:{...native},updateSettings(value,options){updates.push(options);Object.assign(this.settingsData,value)}};
const Jt=()=>store;
let timer=null,timers=0,polls=0,remotePolls=0;
const mailbox={startPolling(){if(timer===null){polls++;timer=++timers}},stopPolling(){timer=null}};
const uo=()=>mailbox;
const Lt=()=>({proRestoreFromApi(){remotePolls++},liteStartPoll(){remotePolls++}});
'''


def _patched(tmp_path: Path, source: str = _SOURCE) -> str:
    main = tmp_path / MAIN_JS_0627
    main.parent.mkdir(parents=True)
    main.write_text("/*" + MAILBOX_WRITE_ANCHOR_0627 + "*/\n" + source, encoding="utf-8")
    assert _repair_mailbox_write_access(tmp_path) == "PATCHED"
    result = main.read_text(encoding="utf-8")
    assert "function oliviaLocalDesktopMail" in result
    assert _repair_mailbox_write_access(tmp_path) == "ALREADY_PATCHED"
    assert main.read_text(encoding="utf-8") == result
    return result


def _run(source: str, assertions: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, "-e", _HARNESS + source + "\n(async()=>{" + assertions
         + "})().catch(error=>{console.error(error);process.exitCode=1})"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("legacy", [False, True])
def test_local_desktop_mail_preserves_user_widget_choices(tmp_path: Path, legacy: bool) -> None:
    source = _SOURCE.replace(_WIDGET_GATE, 'l.value.mailWidget=!0,l.value.musicWidget=!0') if legacy else _SOURCE
    _run(_patched(tmp_path, source), '''
store.settingsData={mailWidget:true,musicWidget:false};enforceWidgets();
assert.deepEqual(store.settingsData,{mailWidget:true,musicWidget:false});
store.settingsData.mailWidget=false;enforceWidgets();
assert.equal(store.settingsData.mailWidget,false);
assert.deepEqual(desktopItems(),["mail-widget","other"]);
''')


def test_login_restores_native_polling_once_and_migrates_only_mail(tmp_path: Path) -> None:
    _run(_patched(tmp_path), '''
login();await oliviaRestoreDesktopMail(state);
assert.deepEqual(writes,[{mailWidget:true}]);
assert.deepEqual(native,{mailWidget:true,musicWidget:false});
assert.deepEqual(updates,[{syncToClient:false}]);
assert.equal(store.settingsData.mailWidget,true);
assert.equal(remotePolls,0);
login();mailbox.startPolling();await oliviaRestoreDesktopMail(state);
assert.equal(timers,1);assert.equal(polls,1);assert.equal(writes.length,1);
native.mailWidget=false;store.settingsData.mailWidget=false;
login();await oliviaRestoreDesktopMail(state);enforceWidgets();
assert.equal(native.mailWidget,false);assert.equal(store.settingsData.mailWidget,false);
assert.equal(writes.length,1);
''')


def test_legacy_request_guard_still_checks_managed_desktop_origin(tmp_path: Path) -> None:
    source = _SOURCE.replace('if(t.isOfflineMode)throw new Ol(e);', 'if(!1)throw new Ol(e);')
    _run(_patched(tmp_path, source), '''
assert.equal(oliviaLocalDesktopMail(state),true);
Te.defaults.baseURL="https://example.invalid/toy";
assert.equal(oliviaLocalDesktopMail(state),false);
''')


def test_migration_waits_for_native_save_and_serializes_concurrent_logins(tmp_path: Path) -> None:
    _run(_patched(tmp_path), '''
let finish;waitForSave=new Promise(resolve=>{finish=resolve});
const first=oliviaRestoreDesktopMail(state),second=oliviaRestoreDesktopMail(state);
await Promise.resolve();
assert.equal(writes.length,1);assert.ok(![...values.values()].includes("1"));
assert.equal(store.settingsData.mailWidget,false);
finish();await Promise.all([first,second]);
assert.equal(writes.length,1);assert.ok([...values.values()].includes("1"));
''')


def test_already_enabled_native_mail_needs_no_write(tmp_path: Path) -> None:
    _run(_patched(tmp_path), '''
native.mailWidget=true;await oliviaRestoreDesktopMail(state);
assert.equal(writes.length,0);assert.equal(store.settingsData.mailWidget,true);
assert.ok([...values.values()].includes("1"));
''')


def test_saved_pro_profile_initializes_local_lite_before_native_settings(tmp_path: Path) -> None:
    _run(_patched(tmp_path), '''
await oliviaRestoreDesktopMail(state);
state.appMode=Se.PRO;state.isOfflineMode=false;Te.defaults.baseURL="";
applyClientConfig();enforceWidgets();
assert.equal(state.appMode,Se.LITE);assert.equal(store.settingsData.mailWidget,true);
login();await oliviaRestoreDesktopMail(state);
assert.equal(writes.length,1);assert.equal(native.mailWidget,true);
state.appMode=Se.PRO;applyClientConfig(false);assert.equal(state.appMode,Se.PRO);
managed=false;applyClientConfig(true);assert.equal(state.appMode,Se.PRO);
''')


def test_user_closing_mail_during_delayed_migration_wins(tmp_path: Path) -> None:
    _run(_patched(tmp_path), '''
let finish;waitForSave=new Promise(resolve=>{finish=resolve});
const migration=oliviaRestoreDesktopMail(state);await Promise.resolve();
assert.equal(writes.length,1);
waitForSave=null;userChoice(false);await Promise.resolve();
finish();await migration;
assert.equal(store.settingsData.mailWidget,false);assert.equal(native.mailWidget,false);
assert.ok([...values.values()].includes("1"));
await oliviaRestoreDesktopMail(state);assert.equal(native.mailWidget,false);
''')


@pytest.mark.parametrize("failure", ["saveFails", "readbackFails", "storageFails"])
def test_migration_failure_is_retryable_and_never_records_success(tmp_path: Path, failure: str) -> None:
    _run(_patched(tmp_path), f'''
{failure}=true;await oliviaRestoreDesktopMail(state);
assert.ok(![...values.values()].includes("1"));
assert.equal(store.settingsData.mailWidget,false);
{failure}=false;await oliviaRestoreDesktopMail(state);
assert.equal(native.mailWidget,true);assert.equal(store.settingsData.mailWidget,true);
assert.ok([...values.values()].includes("1"));
''')


@pytest.mark.parametrize("unmanaged", [
    "managed=false", "apiBase='http://127.0.0.1:19000/'",
    "Te.defaults.baseURL='https://example.invalid/toy'", "state.appMode=Se.PRO",
])
def test_unmanaged_and_pro_sessions_keep_original_offline_behavior(tmp_path: Path, unmanaged: str) -> None:
    _run(_patched(tmp_path), unmanaged + ''';
store.settingsData={mailWidget:true,musicWidget:true};enforceWidgets();
assert.deepEqual(store.settingsData,{mailWidget:false,musicWidget:false});
assert.deepEqual(desktopItems(),["other"]);
login();await oliviaRestoreDesktopMail(state);
assert.equal(timers,0);assert.equal(writes.length,0);assert.equal(values.size,0);
state.isOfflineMode=false;login();
assert.equal(remotePolls,1);
''')
