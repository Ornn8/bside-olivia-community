"""English interface language for the native frontend bundle."""
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer import patch_native_locale as locale
from installer.patch_native_locale import (
    LITERALS, LOCALE_MEMBER, MAIN, MARKER, PRELUDE, patch_native_locale, patch_source,
)

CLIENT = Path(__file__).resolve().parents[2]
EN_MODULE = CLIENT / 'runtime' / 'native_locale' / 'en.js'
KEYS = json.loads((CLIENT / 'tests' / 'fixtures' / 'native_zh_cn_keys.json').read_text(encoding='utf-8'))['keys']
CJK = re.compile('[　-〿㐀-鿿豈-﫿＀-￯]')
PLACEHOLDER = re.compile(r'\{\{[A-Za-z_]+\}\}|\{[A-Za-z_]+\}')


def _node():
    executable = shutil.which('node')
    if not executable:
        pytest.skip('node is required to execute the bundle code')
    return executable


# The official translator exactly as the 0.0.9.627 bundle has it, with its two dynamic-import helpers.
HELPERS = (
    'const Dl=function(t,s,i){if(!s||s.length===0)return t();throw new Error("preload")},'
    'Vl=(e,t)=>{const s=e[t];return s?typeof s=="function"?s():Promise.resolve(s):new Promise((i,l)=>{'
    '(typeof queueMicrotask=="function"?queueMicrotask:setTimeout)(l.bind(null,new Error("Unknown variable dynamic import: "+t)))})};'
)
TRANSLATOR = (
    'let Ao="zh-cn",rs={};const fe=()=>{const e=(i,l={})=>{if(typeof i=="object"&&"key"in i){let c=rs[i.key]??`i18n key: ${i.key} not found`;'
    'return Object.entries(i.data).forEach(([m,u])=>{const p=new RegExp(`{${m}}`,"g");c=c.replace(p,u.toString())}),'
    'Object.entries(l).forEach(([m,u])=>{const p=new RegExp(`{${m}}`,"g");c=c.replace(p,String(u))}),c}'
    'let a=rs[i]??`i18n key: ${i} not found`;return Object.entries(l).forEach(([c,m])=>{const u=new RegExp(`{${c}}`,"g");a=a.replace(u,String(m))}),a},'
    't=async i=>{Ao=i,rs=(await Vl(Object.assign({"./locales/zh-cn.json":()=>Dl(()=>import("./zh-cn-bd81633d.js"),[],import.meta.url)}),'
    '`./locales/${Ao}.json`)).default};return{t:e,setLang:t,init18n:async(i={lang:Ao})=>{await t(i.lang)},lang:Ao}};'
)
START = 'const Db=async()=>{const{init18n:e}=fe();await e({lang:"zh-cn"});const t=jo(nf);t.mount("#app")};'
MONTH = 'const la=e=>{const i=new Date(e);return["common_date_format",{month:i.getMonth()+1,day:i.getDate()}]};'
UPLOAD = 'const T=new Error(`上传失败: ${d.status}`);'

ORIGINAL_LITERALS = [
    'x="请选择文件";', 'y=new Error("上传过程中发生错误");', 'y=new Error("上传请求超时");', 'y=new Error("上传被取消");',
    'errorMessage:{default:"当前设备暂不支持播放该视频"},', 'd.value="当前设备暂不支持播放该视频",',
]
INJECTED_LITERALS = [
    'return"时间未知";',
    'title:o(l)===o(ot).Shuffle?"随机播放，点击切换为顺序播放":"顺序播放，点击切换为随机播放","aria-label":o(l)===o(ot).Shuffle?"随机播放":"顺序播放",',
    'l=j(()=>s.mail.letterStatus===bt.NO_REPLY?"本轮不回复 · "+s.mail.sent.subject:i.value);',
    'v(a.waiting?"收到回信后可以继续写信":o(s)("mailbox_write_mail_remaing")),v(a.waiting?"等待林离回信":o(s)("mailbox_write_mail"));',
    'n("div",{role:"status"},"本轮不回复");',
    'v(window.__oliviaReplyFailureMessage?.(A.errorCode,"title")||"这封回信暂时没能完成。你的信仍保留在信箱里。"),'
    'v(window.__oliviaReplyFailureMessage?.(A.errorCode,"hint")||"请保留诊断包，查看信件状态后再决定是否重寄。");',
    'v(A.replyWaitReason==="bathing"?"林离洗澡中":A.videoPending?"林离录视频中":["PENDING"].includes(A.audioStatus)?"林离正在录语音…":o(i)("mailbox_waiting_for_reply"));',
]
CONSOLE_ONLY = '"📡 上传请求完成, 状态码:"'


def bundle(injected=True):
    parts = [HELPERS, TRANSLATOR, START, MONTH, UPLOAD, f'console.log({CONSOLE_ONLY},d.status);', *ORIGINAL_LITERALS]
    if injected:
        parts += INJECTED_LITERALS
    return '/*olivia-letter-scroll-v1*/import{x}from"./letter-stickers/select.js";\n' + '\n'.join(parts) + '\n'


def cjk_literals(source):
    """Every quoted or template string token that contains Chinese, by position."""
    found, i, n = [], 0, len(source)
    while i < n:
        c = source[i]
        if c in '"\'`':
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == '\\' else 1
            if CJK.search(source[i:j + 1]):
                found.append(source[i:j + 1])
            i = j + 1
        else:
            i += 1
    return found


def wrapped_calls_removed(source):
    string = r'"(?:[^"\\]|\\.)*"'
    source = re.sub(r'oliviaNativeText\(' + string + ',' + string + r'\)', '""', source)
    return source


def make_archive(path, main, extra=None):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(MAIN, main)
        z.writestr('index.html', '<html></html>')
        z.writestr('assets/zh-cn-bd81633d.js', 'export default {}')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


# ---------- source patch ----------

def test_patch_covers_every_known_literal_and_leaves_only_the_console_message():
    patched = patch_source(bundle())
    assert patched.startswith(MARKER + PRELUDE)
    assert 'await e({lang:oliviaNativeLang});' in patched and 'await e({lang:"zh-cn"});' not in patched
    assert '{month:oliviaNativeMonth(i.getMonth()),day:i.getDate()}' in patched
    assert '`${oliviaNativeText("上传失败: ","Upload failed: ")}${d.status}`' in patched
    assert '"./locales/en.json":()=>Dl(()=>import("./en-olivia.js"),[],import.meta.url)' in patched
    for chinese, english, expected, _ in LITERALS:
        assert patched.count(f'oliviaNativeText({json.dumps(chinese, ensure_ascii=False)},{json.dumps(english, ensure_ascii=False)})') == expected
    assert cjk_literals(wrapped_calls_removed(patched)) == [CONSOLE_ONLY]


def test_patch_is_idempotent_and_marker_is_single():
    once = patch_source(bundle())
    assert patch_source(once) == once
    assert once.count(MARKER) == 1 and once.count(PRELUDE) == 1


def test_literals_added_by_other_patches_are_picked_up_on_a_later_pass():
    first = patch_source(bundle(injected=False))
    assert 'oliviaNativeText("本轮不回复"' not in first and '本轮不回复' not in first
    later = patch_source(first + '\n' + '\n'.join(INJECTED_LITERALS))
    assert cjk_literals(wrapped_calls_removed(later)) == [CONSOLE_ONLY]
    assert patch_source(later) == later


@pytest.mark.parametrize('damage', [
    lambda s: s.replace('t=async i=>{Ao=i,rs=', 't=async i=>{Ao=i,rsx='),
    lambda s: s.replace('await e({lang:"zh-cn"});', ''),
    lambda s: s + START,
    lambda s: s.replace('month:i.getMonth()+1', 'month:i.getMonth()'),
    lambda s: s.replace('x="请选择文件";', ''),
    lambda s: s + 'z="请选择文件";',
    lambda s: s.replace('d.value="当前设备暂不支持播放该视频",', ''),
    lambda s: s + 'z="本轮不回复";',
])
def test_unexpected_bundle_text_is_rejected(damage):
    with pytest.raises(ValueError, match='NATIVE_LOCALE'):
        patch_source(damage(bundle()))


def test_a_second_marker_is_rejected():
    with pytest.raises(ValueError, match='NATIVE_LOCALE_MARKER_INVALID'):
        patch_source(MARKER + patch_source(bundle()))


# ---------- archive patch ----------

def test_archive_gets_module_and_patched_bundle_and_keeps_other_members(tmp_path):
    path = make_archive(tmp_path / 'feapp.dat', bundle(), {'assets/keep.bin': b'keep me'})
    assert patch_native_locale(path) == 'PATCHED'
    with zipfile.ZipFile(path) as z:
        assert z.read(LOCALE_MEMBER) == EN_MODULE.read_bytes()
        assert z.read('assets/keep.bin') == b'keep me' and z.read('index.html') == b'<html></html>'
        assert z.read(MAIN).decode('utf-8') == patch_source(bundle())
        assert z.namelist().count(LOCALE_MEMBER) == 1 and z.testzip() is None
    assert path.with_name('feapp.dat.locale.orig').is_file()
    before = path.read_bytes()
    assert patch_native_locale(path) == 'ALREADY_PATCHED'
    assert path.read_bytes() == before


def test_changed_english_module_is_replaced_in_place(tmp_path, monkeypatch):
    path = make_archive(tmp_path / 'feapp.dat', bundle())
    assert patch_native_locale(path) == 'PATCHED'
    newer = tmp_path / 'en.js'
    newer.write_bytes(b'export default {studio_title:"Music!"}')
    monkeypatch.setattr(locale, 'SOURCE', newer)
    assert patch_native_locale(path) == 'PATCHED'
    with zipfile.ZipFile(path) as z:
        assert z.read(LOCALE_MEMBER) == newer.read_bytes() and z.namelist().count(LOCALE_MEMBER) == 1
    assert patch_native_locale(path) == 'ALREADY_PATCHED'


def test_unsupported_and_mismatching_archives_are_not_touched(tmp_path):
    other = tmp_path / 'other.dat'
    with zipfile.ZipFile(other, 'w') as z:
        z.writestr('index.html', 'x')
    before = other.read_bytes()
    assert patch_native_locale(other) == 'UNSUPPORTED_CLIENT' and other.read_bytes() == before
    broken = make_archive(tmp_path / 'broken.dat', 'unknown version of the bundle')
    before = broken.read_bytes()
    with pytest.raises(ValueError, match='NATIVE_LOCALE_ANCHOR_INVALID'):
        patch_native_locale(broken)
    assert broken.read_bytes() == before
    assert not list(tmp_path.glob('*.locale.orig')) and not [p for p in tmp_path.iterdir() if p.name.startswith('.native-locale-')]


def test_patch_runs_on_top_of_the_sticker_patch(tmp_path):
    from installer.patch_letter_stickers import patch_letter_stickers
    source = ('content:e.replyText??"",;__name:"MailBoxReplyContent",props:{;'
              'class:ae(["mail-box-reply-content faux-bold",o(E)]);'
              '_(se,{key:2},[A.type==="text"?(r(),_("textarea",{key:0,ref:p,value:l.modelValue,readonly:A.readonly,;'
              'n("div",mw,v(o(I)),1);null,42,uw);],2)}}});const $s=;'
              '__name:"MailBoxContentBody";F(ks,{onVideoError:u},null,8,[]);F(ks,{onVideoError:u},null,8,[])]}),_:1},8,["disabled"])\n')
    path = make_archive(tmp_path / 'feapp.dat', source + bundle())
    assert patch_letter_stickers(path) == 'PATCHED'
    assert patch_native_locale(path) == 'PATCHED'
    assert patch_letter_stickers(path) == 'ALREADY_PATCHED'
    assert patch_native_locale(path) == 'ALREADY_PATCHED'
    with zipfile.ZipFile(path) as z:
        text = z.read(MAIN).decode('utf-8')
        assert MARKER in text and 'olivia-letter-stickers-v5' in text
        assert LOCALE_MEMBER in z.namelist() and 'assets/letter-stickers/select.js' in z.namelist()


# ---------- the English module ----------

def english_keys():
    program = ("import {pathToFileURL} from 'url';const m=await import(pathToFileURL(process.argv[1]).href);"
               "console.log(JSON.stringify({d:m.default,names:Object.keys(m).filter(k=>k!=='default')}))")
    out = subprocess.check_output([_node(), '--input-type=module', '-e', program, str(EN_MODULE)], text=True, encoding='utf-8')
    return json.loads(out)


def test_english_module_has_exactly_the_zh_cn_keys_and_export_shape():
    loaded = english_keys()
    assert set(loaded['d']) == set(KEYS) and len(KEYS) == 398
    assert sorted(loaded['names']) == sorted(KEYS)       # same named exports as the zh-cn module
    assert all(isinstance(value, str) and value.strip() for value in loaded['d'].values())


def test_english_values_keep_placeholders_and_contain_no_chinese():
    values = english_keys()['d']
    for key, placeholders in KEYS.items():
        assert sorted(PLACEHOLDER.findall(values[key])) == placeholders, key
        assert not CJK.search(values[key]), key
    assert not CJK.search(EN_MODULE.read_text(encoding='utf-8'))


def test_english_module_is_utf8_without_bom_and_has_no_crlf():
    raw = EN_MODULE.read_bytes()
    assert not raw.startswith(b'\xef\xbb\xbf') and b'\r' not in raw
    raw.decode('utf-8')


def test_glossary_terms_are_used():
    values = english_keys()['d']
    assert values['mailbox_title'] == 'Mailbox' and values['mailbox_my_mailbox'] == 'My mailbox'
    assert values['mailbox_write_mail'] == 'Write a letter' and values['mailbox_write_mail_title'] == 'Write to Linli'
    assert values['studio_title'] == 'Music' and values['settings_title'] == 'Settings'
    assert values['common_date_format'] == '{month} {day}'
    assert not [k for k, v in values.items() if '林' in v or 'BSide' in v]


# ---------- language selection and loading, executed by node ----------

DRIVER = r"""
import fs from 'fs';
import path from 'path';
import {pathToFileURL} from 'url';
const [dir, scenarios] = [process.argv[2], JSON.parse(process.argv[3])];
const results = [];
let n = 0;
for (const s of scenarios) {
  const store = s.stored;
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {getItem(k){
    if (store === 'throw') throw new Error('blocked');
    if (k !== 'olivia.ui.language') throw new Error('wrong key ' + k);
    return store ?? null;
  }}});
  Object.defineProperty(globalThis, 'navigator', {configurable: true, get(){
    if (s.nav === 'throw') throw new Error('no navigator');
    return {language: s.nav};
  }});
  for (const name of s.remove || []) fs.rmSync(path.join(dir, name), {force: true});
  const mod = await import(pathToFileURL(path.join(dir, 'main.mjs')).href + '?case=' + (n++));
  const out = await mod.run(s);
  results.push(out);
}
console.log(JSON.stringify(results));
"""

ZH_MODULE = 'const a="曲库",b="只有中文",c="{n}分钟前";export default {studio_title:a,only_zh:b,common_minutes_ago:c};'


RUN = (
    '\nexport const run=async()=>{const{t,init18n}=fe();await init18n({lang:oliviaNativeLang});'
    'return{lang:oliviaNativeLang,text:oliviaNativeText("中","en"),month:oliviaNativeMonth(9),'
    'title:t("studio_title"),fallback:t("only_zh"),minutes:t("common_minutes_ago",{n:5}),'
    'raw:t("missing_key")}};\n'
)


def write_harness(folder, main, zh, english):
    folder.mkdir(exist_ok=True)
    (folder / 'main.mjs').write_text(main, encoding='utf-8')
    (folder / 'zh-cn-bd81633d.js').write_bytes(zh)
    (folder / 'en-olivia.js').write_bytes(english)
    (folder / 'driver.mjs').write_text(DRIVER, encoding='utf-8')


def run_driver(folder, scenarios):
    out = subprocess.check_output([_node(), str(folder / 'driver.mjs'), str(folder), json.dumps(scenarios)],
                                  text=True, encoding='utf-8')
    return json.loads(out)


def run_node(tmp_path, scenarios, english=None):
    # The translator, start-up call and date helper of the real bundle; the literal snippets stay in a comment
    # so that the patch sees every anchor while node only runs the translator.
    source = (HELPERS + TRANSLATOR + 'const jo=()=>({mount(){}}),nf={};' + START + MONTH
              + 'function upload(d){' + UPLOAD + '}\n/*' + '\n'.join(ORIGINAL_LITERALS) + '*/\n')
    write_harness(tmp_path / 'web', patch_source(source) + RUN, ZH_MODULE.encode('utf-8'),
                  english if english is not None else EN_MODULE.read_bytes())
    return run_driver(tmp_path / 'web', scenarios)


@pytest.mark.parametrize('stored, nav, lang', [
    ('en', 'zh-CN', 'en'),            # only an explicit stored choice selects English
    ('en', 'en-US', 'en'),
    ('zh-cn', 'en-US', 'zh-cn'),
    (None, 'zh-CN', 'zh-cn'),
    (None, 'en-US', 'zh-cn'),         # the browser language never switches existing users
    (None, 'ja-JP', 'zh-cn'),
    ('fr', 'en-US', 'zh-cn'),         # an unknown stored value is ignored
    ('', 'en-US', 'zh-cn'),
    ('throw', 'en-US', 'zh-cn'),      # blocked storage keeps today's Chinese
    (None, 'throw', 'zh-cn'),
    ('en', 'throw', 'en'),
])
def test_start_language_selection(tmp_path, stored, nav, lang):
    result = run_node(tmp_path, [{'stored': stored, 'nav': nav}])[0]
    assert result['lang'] == lang


def test_zh_cn_mode_returns_exactly_the_original_values(tmp_path):
    result = run_node(tmp_path, [{'stored': 'zh-cn', 'nav': 'en-US'}])[0]
    assert result['lang'] == 'zh-cn'
    assert result['text'] == '中' and result['month'] == 10
    assert result['title'] == '曲库' and result['minutes'] == '5分钟前'
    assert result['raw'] == 'i18n key: missing_key not found'   # the official translator, untouched


def test_english_mode_loads_english_with_chinese_fallback(tmp_path):
    english = b'const a="Music",b="{n}m ago";export default {studio_title:a,common_minutes_ago:b};'
    result = run_node(tmp_path, [{'stored': 'en', 'nav': 'zh-CN'}], english=english)[0]
    assert result['lang'] == 'en' and result['text'] == 'en' and result['month'] == 'Oct'
    assert result['title'] == 'Music' and result['minutes'] == '5m ago'
    assert result['fallback'] == '只有中文'            # a key missing in English shows Chinese, never a raw key
    assert result['raw'] == 'i18n key: missing_key not found'


def test_missing_or_broken_english_module_shows_chinese_instead_of_failing(tmp_path):
    result = run_node(tmp_path, [{'stored': 'en', 'nav': 'en-US', 'remove': ['en-olivia.js']}])[0]
    assert result['lang'] == 'en' and result['title'] == '曲库' and result['fallback'] == '只有中文'
    result = run_node(tmp_path, [{'stored': 'en', 'nav': 'en-US'}], english=b'this is not javascript (')[0]
    assert result['title'] == '曲库'


def test_months_are_names_in_english_and_numbers_in_chinese(tmp_path):
    results = run_node(tmp_path, [{'stored': 'en', 'nav': 'en-US'}, {'stored': 'zh-cn', 'nav': 'en-US'}])
    assert [r['month'] for r in results] == ['Oct', 10]


# ---------- wiring ----------

def test_patcher_and_module_are_part_of_every_delivery_path():
    from installer import build_windows_setup, full_patch, start_local
    assert 'installer/patch_native_locale.py' in full_patch.PAYLOAD_REQUIRED_RELATIVE_FILES
    assert 'runtime/native_locale/en.js' in full_patch.PAYLOAD_REQUIRED_RELATIVE_FILES
    assert 'runtime/native_locale' in full_patch.PAYLOAD_EXTRA_DIRS
    assert 'installer/patch_native_locale.py' in build_windows_setup.RELEASE_INSTALLER_FILES
    assert build_windows_setup._is_release_file('runtime/native_locale/en.js')
    names = {path.relative_to(CLIENT).as_posix() for path in start_local._frontend_patcher_files(CLIENT)}
    assert {'installer/patch_native_locale.py', 'runtime/native_locale/en.js'} <= names


def test_module_is_copied_into_the_installed_backend_payload(tmp_path):
    from installer.full_patch import PAYLOAD_EXTRA_DIRS, _ignore_payload_copy
    assert 'en.js' not in _ignore_payload_copy(str(EN_MODULE.parent), ['en.js'])
    target = tmp_path / 'payload'
    shutil.copytree(EN_MODULE.parent, target, ignore=lambda d, names: _ignore_payload_copy(d, names))
    assert (target / 'en.js').read_bytes() == EN_MODULE.read_bytes()


def _repair_environment(tmp_path, monkeypatch, locale_result):
    import installer.start_local as launcher
    client = tmp_path / 'app/Olivia.exe'
    resources = client.parent / 'resources'
    resources.mkdir(parents=True)
    (resources / 'feapp.dat').write_bytes(b'archive')
    monkeypatch.setattr(launcher, '_client_executable', lambda _: client)
    monkeypatch.setattr(launcher, 'repair_web_player_event_ids', lambda *a, **k: None)
    monkeypatch.setattr(launcher, 'patch_companion_settings', lambda *a, **k: {'status': 'ALREADY_PATCHED'})
    monkeypatch.setattr('installer.patch_letter_stickers.patch_letter_stickers', lambda _: 'ALREADY_PATCHED')
    monkeypatch.setattr(launcher, '_frontend_patcher_digest', lambda: 'digest')
    monkeypatch.setattr(launcher, '_append_launcher_event', lambda root, event, **fields: events.append((event, fields)))
    events = []
    calls = []

    def fake(path):
        calls.append(path)
        if isinstance(locale_result, Exception):
            raise locale_result
        return locale_result
    monkeypatch.setattr('installer.patch_native_locale.patch_native_locale', fake)
    return launcher, events, calls, resources


def test_frontend_repair_applies_the_language_patch_after_the_sticker_patch(tmp_path, monkeypatch):
    launcher, events, calls, resources = _repair_environment(tmp_path, monkeypatch, 'PATCHED')
    assert launcher._repair_client_frontend(tmp_path, 8899) == 'PATCHED'
    assert calls == [resources / 'feapp.dat'] and events == []
    assert launcher._repair_client_frontend(tmp_path, 8899) == 'ALREADY_PATCHED'    # receipt written
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [ValueError('NATIVE_LOCALE_ANCHOR_INVALID'), OSError('locked'), zipfile.BadZipFile('bad')])
def test_a_failing_language_patch_never_blocks_startup(tmp_path, monkeypatch, failure):
    launcher, events, calls, _ = _repair_environment(tmp_path, monkeypatch, failure)
    assert launcher._repair_client_frontend(tmp_path, 8899) == 'ALREADY_PATCHED'
    assert events and events[0][0] == 'native_locale_skipped'
    assert launcher._repair_client_frontend(tmp_path, 8899) == 'ALREADY_PATCHED'
    assert len(calls) == 2                      # no receipt, so the next start tries again


# ---------- real bundle (needs the official archive; set OLIVIA_OFFICIAL_FEAPP_ORIG) ----------

OFFICIAL = os.environ.get('OLIVIA_OFFICIAL_FEAPP_ORIG')
needs_official = pytest.mark.skipif(not OFFICIAL or not Path(OFFICIAL).is_file(),
                                    reason='set OLIVIA_OFFICIAL_FEAPP_ORIG to the official feapp.dat to run')


def _main(path):
    with zipfile.ZipFile(path) as z:
        return z.read(MAIN).decode('utf-8')


@needs_official
def test_real_bundle_original_and_fully_patched(tmp_path):
    import sys
    sys.path.insert(0, str(CLIENT))
    from patch_companion_settings import patch_companion_settings
    from patch_feapp import patch_feapp
    from installer.patch_letter_stickers import patch_letter_stickers
    official = Path(OFFICIAL)
    original = _main(official)
    patched_original = patch_source(original)
    assert patch_source(patched_original) == patched_original
    assert [lit for lit in cjk_literals(wrapped_calls_removed(patched_original))] == [CONSOLE_ONLY]

    work = tmp_path / 'full'
    work.mkdir()
    path = work / 'feapp.dat'
    shutil.copy2(official, path)
    patch_feapp(path, 'ws://127.0.0.1:8899/ws', work_root=work)
    for _ in range(2):          # the launcher repeats these on every repair
        patch_companion_settings(path, 'http://127.0.0.1:8899/', work_root=work)
        patch_letter_stickers(path)
        assert patch_native_locale(path) in {'PATCHED', 'ALREADY_PATCHED'}
    settled = path.read_bytes()
    assert patch_native_locale(path) == 'ALREADY_PATCHED' and path.read_bytes() == settled
    full = _main(path)
    assert full.count(MARKER) == 1
    assert cjk_literals(wrapped_calls_removed(full)) == [CONSOLE_ONLY]
    with zipfile.ZipFile(path) as z:
        assert LOCALE_MEMBER in z.namelist()
        zh_text = z.read('assets/zh-cn-bd81633d.js').decode('utf-8')
    zh_names = set(re.findall(r'\bas ([A-Za-z_$][\w$]*)[,}]', zh_text))
    assert zh_names - {'default'} == set(KEYS)       # the real zh-cn module and the English module share their keys


@needs_official
def test_real_bundle_loader_runs_with_the_real_zh_module(tmp_path):
    with zipfile.ZipFile(OFFICIAL) as z:
        zh = z.read('assets/zh-cn-bd81633d.js')
        original = z.read(MAIN).decode('utf-8')
    patched = patch_source(original)
    start = patched.index('let Ao="zh-cn",rs={};')
    block = patched[start:patched.index('Rs=e=>', start)].rstrip(',')
    run = ('\nexport const run=async()=>{const{t,init18n}=fe();await init18n({lang:oliviaNativeLang});'
           'return{lang:oliviaNativeLang,title:t("studio_title"),write:t("mailbox_write_mail"),'
           'ago:t("common_minutes_ago",{n:7}),date:t({key:"common_date_format",data:{month:"Oct",day:3}})}};\n')
    write_harness(tmp_path / 'real', MARKER + PRELUDE + HELPERS + block + ';' + run, zh, EN_MODULE.read_bytes())
    out = run_driver(tmp_path / 'real', [{'stored': 'zh-cn', 'nav': 'en-US'}, {'stored': 'en', 'nav': 'zh-CN'}])
    assert out[0]['title'] == '曲库' and out[0]['write'] == '写信' and out[0]['ago'] == '7分钟前'
    assert out[0]['date'] == 'Oct月3日'
    assert out[1] == {'lang': 'en', 'title': 'Music', 'write': 'Write a letter', 'ago': '7m ago', 'date': 'Oct 3'}


def test_a_wrapped_literal_after_a_keyword_keeps_the_keyword_apart():
    from installer.patch_native_locale import _GLUED, WRAP, _wrap_literal
    source = 'Ws=e=>{if(e==null)return"时间未知";return x}'
    wrapped = _wrap_literal(source, '时间未知', 'Unknown time', 1, True)
    assert 'return oliviaNativeText("时间未知","Unknown time")' in wrapped
    # Bundles patched before the fix are repaired the next time the patch runs.
    broken = 'if(e==null)returnoliviaNativeText("时间未知","Unknown time");x=foo(oliviaNativeText("a","b"))'
    repaired = _GLUED.sub(lambda match: match.group(1) + ' ' + WRAP, broken)
    assert 'return oliviaNativeText(' in repaired and 'foo(oliviaNativeText(' in repaired
