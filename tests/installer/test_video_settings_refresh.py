import subprocess

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT


def test_installed_video_refresh_enables_existing_settings_row(tmp_path):
    source = BOOTSTRAP_JAVASCRIPT
    mount = source[source.index("  const mountVideoReplySetting ="):source.index("  const mountDiagnosticExport =")]
    harness = r'''
const assert = require("node:assert/strict");
let refreshVideoReplySetting = async () => {};
let ready = false, calls = 0;
const buttons=[];
const node=()=>({isConnected:true,style:{},append(){},setAttribute(){}});
const document={createElement:node};
const text=node, actions=node;
const button=(label,fn)=>{const n=node();n.textContent=label;n.click=fn;buttons.push(n);return n;};
const VIDEO_REPLY_SETTINGS_PATH="/toy/settings/video-reply";
const VIDEO_REPLY_DEPENDENCY_LABELS=new Map();
const requestJson=async()=>{calls++;return {state:"available",enabled:false,ready,dependencies:[]};};
'''
    harness += mount + r'''
(async()=>{
 mountVideoReplySetting(node());
 await new Promise(setImmediate);
 assert.equal(buttons[0].disabled,true);
 ready=true;
 await refreshVideoReplySetting();
 assert.equal(buttons[0].disabled,false);
 assert.equal(calls,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    script = tmp_path / "refresh.cjs"
    script.write_text(harness, encoding="utf-8")
    subprocess.run(["node", str(script)], check=True, capture_output=True, text=True)


def test_capability_completion_and_all_dialog_dismissals_refresh_setting():
    source = BOOTSTRAP_JAVASCRIPT
    dialog = source[source.index("  const openDialog ="):source.index("  const findSettingsContainer =")]
    assert "const dismiss = () => {" in dialog
    assert "void refreshVideoReplySetting();" in dialog
    assert dialog.count("dismiss();") == 3
    capability = source[source.index("  const renderVideoCapabilityPanel ="):source.index("  const openDialog =")]
    assert 'if (payload && payload.status === "READY") void refreshVideoReplySetting();' in capability
