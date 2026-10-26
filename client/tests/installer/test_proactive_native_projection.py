import pytest
import shutil
import subprocess

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

from patch_companion_settings import _repair_native_proactive_collection, CompanionSettingsPatchError


def test_composer_presence_rate_limit_clear_and_no_draft_content(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    begin = BOOTSTRAP_JAVASCRIPT.index('  const composingInputs =')
    end = BOOTSTRAP_JAVASCRIPT.index('  const constrainLetterInputs =', begin)
    script = tmp_path / 'presence.cjs'
    script.write_text('''
const sent=[]; let now=0; Date.now=()=>now;
const requestMutation=async(path, body)=>{sent.push({path,body});};
''' + BOOTSTRAP_JAVASCRIPT[begin:end] + '''
(async()=>{
  letterComposing(true);
  for(let i=0;i<29;i++){now+=1000;letterComposing(true);}
  await composingRequest;
  if(sent.length!==1)throw Error('heartbeat spam');
  now=30000;letterComposing(true); await composingRequest;
  if(sent.length!==2)throw Error('renewal missing');
  letterComposing(false); await composingRequest;
  if(sent.length!==3 || sent[2].body.active!==false)throw Error('clear missing');
  letterComposing(false);await composingRequest;
  if(sent.length!==3)throw Error('duplicate clear');
  for(const item of sent){
    if(item.path!=='/toy/letter/composing' || Object.keys(item.body).join()!=='active')throw Error('draft leak');
  }
})();
''', encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_proactive_selection_skips_paired_letter_and_updates_native_audio_props():
    # Synthetic fragments of the supported adapter contract; no original bundle.
    model = 'return{id:e.letterId,isUnread:e.isRead===0,coverId:e.coverId||"",'
    source = model + model + (
        'h=j({get:()=>i("mailbox_welcome_content"),set:()=>{}})'
        ':x.selectedMail&&!o(p)?paired:o(p)&&o(l).welcomeMailRead?welcome:empty;'
        'k(ks,{modelValue:o(h),"onUpdate:modelValue":I[1]||(I[1]=A=>be(h)?h.value=A:null),'
        'class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",'
        'timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??0,'
        'type:"text"},null,8,["modelValue","timestamp"])'
    )
    patched = _repair_native_proactive_collection(source)
    assert 'x.selectedMail&&!o(p)&&x.selectedMail.origin!=="proactive"?' in patched
    assert 'x.selectedMail.origin==="proactive")?welcome' in patched
    assert 'a.selectedMail.received' in patched
    assert '["modelValue","timestamp","coverId","audioUrl","audioStatus","songUrl"]' in patched
    assert _repair_native_proactive_collection(patched) == patched


@pytest.mark.parametrize('unread,empty', [
    ('e.isRead===0,replyKind:e.replyKind||"text_letter",', '0'),
    ('e.letterStatus!==bt.NO_REPLY&&e.isRead===0,replyKind:e.replyKind||"text_letter",', 'null'),
])
def test_fresh_install_shapes_get_the_projection(unread, empty):
    # A fresh install runs every step in one pass: replyKind sits between isUnread and coverId, and a
    # later start has the NO_REPLY unread check and a ??null paper timestamp. 2.4.0 skipped both.
    model = 'return{id:e.letterId,isUnread:' + unread + 'coverId:e.coverId||"",'
    source = model + model + (
        'h=j({get:()=>i("mailbox_welcome_content"),set:()=>{}})'
        ':x.selectedMail&&!o(p)?paired:o(p)&&o(l).welcomeMailRead?welcome:empty;'
        'k(ks,{modelValue:o(h),"onUpdate:modelValue":I[1]||(I[1]=A=>be(h)?h.value=A:null),'
        'class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",'
        'timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??' + empty + ','
        'type:"text"},null,8,["modelValue","timestamp"])'
    )
    patched = _repair_native_proactive_collection(source)
    assert patched.startswith('/*olivia-proactive-native-v1*/')
    assert patched.count('isUnread:' + unread.split(',')[0] + ',origin:e.origin||"",title:e.title||e.summary||"",'
                         'replyAllowed:e.replyAllowed!==false,') == 2
    assert '["modelValue","timestamp","coverId","audioUrl","audioStatus","songUrl"]' in patched
    assert _repair_native_proactive_collection(patched) == patched


def test_changed_supported_native_shape_fails_before_partial_patch():
    source = 'return{id:e.letterId,isUnread:e.isRead===0,coverId:e.coverId||"",' * 2
    with pytest.raises(CompanionSettingsPatchError, match='COMPANION_PROACTIVE_ANCHOR_INVALID'):
        _repair_native_proactive_collection(source)


def test_busy_send_gate_keeps_existing_mailbox_readable(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    begin = BOOTSTRAP_JAVASCRIPT.index('window.__oliviaPrepareLetterRoute = async (config) => {')
    end = BOOTSTRAP_JAVASCRIPT.index('    const body =', begin)
    gate = BOOTSTRAP_JAVASCRIPT[begin:end] + 'return config; };'
    script = tmp_path / 'gate.cjs'
    script.write_text('const window={}; const apiBase="http://127.0.0.1:4000"; const proactiveState={busy:true};\n'
                      + gate + '''
(async()=>{
  const detail={url:'/toy/letter/detail'};
  if(await window.__oliviaPrepareLetterRoute(detail)!==detail) throw Error('read blocked');
  const sending={url:'/toy/letter/send'};
  if(await window.__oliviaPrepareLetterRoute(sending)!==sending) throw Error('user send blocked');
  try { await window.__oliviaPrepareLetterRoute({url:'/toy/letter/resend'}); }
  catch(error) { if(error.code==='PROACTIVE_LETTER_BUSY') return; throw error; }
  throw Error('send was allowed');
})();
''', encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_native_save_supplies_existing_explicit_action_header(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    begin = BOOTSTRAP_JAVASCRIPT.index('  const saveProactiveSettings = async')
    end = BOOTSTRAP_JAVASCRIPT.index('  const requestDiagnosticExport', begin)
    script = tmp_path / 'save.cjs'
    script.write_text('''
const apiBase='http://127.0.0.1:4000', PROACTIVE_SETTINGS_PATH='/toy/proactive/settings';
const CONFIRM_HEADER='X-Olivia-Companion-Action', CONFIRM_VALUE='confirmed';
const publishProactiveState=()=>{};
global.fetch=async(url, options)=>{
  if(options.headers[CONFIRM_HEADER]!==CONFIRM_VALUE) throw Error('confirmation missing');
  return {ok:true, json:async()=>({code:0,data:JSON.parse(options.body)})};
};
''' + BOOTSTRAP_JAVASCRIPT[begin:end] + '\nsaveProactiveSettings({enabled:true,allow_voice:true,login_check_enabled:false});', encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_her_own_letter_shows_the_video_it_carries():
    # 2026-10-26: a contact letter carried a finished song video the paper was never given.
    from patch_companion_settings import _repair_native_proactive_video
    paper = ('k(ks,{coverId:x.selectedMail?.coverId,audioUrl:x.selectedMail?.received?.audioUrl||"",'
             'modelValue:o(h),class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",timestamp:t??null,'
             'type:"text"},null,8,["modelValue","timestamp","coverId","audioUrl","audioStatus","songUrl"]')
    patched = _repair_native_proactive_video('before;' + paper + ';after')
    assert patched.startswith('/*olivia-proactive-video-v1*/')
    assert 'videoUrl:x.selectedMail?.received?.videoUrl,bodyText:x.selectedMail?.received?.bodyText,' in patched
    assert 'type:x.selectedMail?.received?.type==="video"?"video":"text"},null,8,' in patched
    assert '"songUrl","videoUrl","bodyText","signature","type"]' in patched
    assert _repair_native_proactive_video(patched) == patched
    assert _repair_native_proactive_video('a bundle without the paper') == 'a bundle without the paper'


# Her letter's paper as the native mailbox renders it (assets/main-*.js, welcome-paper path), its slot wrapper included.
NATIVE_PROACTIVE_PAPER = (
    'n("div",Hw,[k($s,null,{default:V(()=>{var E,M;return[k(ks,{coverId:x.selectedMail?.coverId,'
    'audioUrl:x.selectedMail?.received?.audioUrl||"",audioStatus:x.selectedMail?.audioStatus||"",'
    'songUrl:x.selectedMail?.received?.songUrl||"",modelValue:o(h),"onUpdate:modelValue":I[1]||(I[1]=A=>be(h)?h.value=A:null),'
    'class:"w-[516px] aspect-[16/9]","is-visible":!0,readonly:"",'
    'timestamp:((M=(E=x.selectedMail)==null?void 0:E.received)==null?void 0:M.timestamp)??null,'
    'type:"text"},null,8,["modelValue","timestamp","coverId","audioUrl","audioStatus","songUrl"])]}),_:1})])')


def test_her_video_letter_keeps_its_words_under_the_video(tmp_path):
    # 2026-10-26 「主动信件视频把文字挡住了」: the paper shows text OR video, so the video took her words away.
    from patch_companion_settings import _repair_native_proactive_video, _repair_native_proactive_video_text
    patched = _repair_native_proactive_video_text(_repair_native_proactive_video(NATIVE_PROACTIVE_PAPER))
    assert patched.startswith('/*olivia-proactive-video-text-v1*/')
    assert _repair_native_proactive_video_text(patched) == patched
    assert _repair_native_proactive_video_text(NATIVE_PROACTIVE_PAPER) == NATIVE_PROACTIVE_PAPER  # needs the video step
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js unavailable')
    expression = patched[patched.index('*/') + 2:]
    script = tmp_path / 'paper.cjs'
    script.write_text('''
const n=(tag,props,children)=>({tag,props,children}), k=(c,props,slots)=>({c,props,slots});
const V=f=>f, r=()=>{}, _=(tag,props,children)=>({tag,props,children}), Y=()=>null, o=v=>v.value, be=()=>false;
const Hw={}, $s='zoomable', ks='paper', I=[];
const papers=node=>{if(!node||typeof node!=='object')return[];if(Array.isArray(node))return node.flatMap(papers);
  if(node.c==='paper')return[node.props];
  return [...papers(node.children),...papers(node.slots&&node.slots.default?node.slots.default():null)];};
const render=(type)=>{const x={selectedMail:{coverId:1,audioStatus:'',received:{type,bodyText:'她的信',signature:'林',
  timestamp:1,videoUrl:type==='video'?'v.mp4':''}}};const h={value:'她的信'};return papers(%s);};
const video=render('video'), text=render('text');
if(video.length!==2||video[0].type!=='video'||video[1].type!=='text'||video[1].modelValue!=='她的信')throw Error('video '+JSON.stringify(video));
if(text.length!==1||text[0].type!=='text')throw Error('text '+JSON.stringify(text));
''' % expression, encoding='utf-8')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
