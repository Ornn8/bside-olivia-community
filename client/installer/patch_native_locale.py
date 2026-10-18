"""Add an English interface language to the native 0627 frontend bundle atomically.

The native bundle ships a small translator that only knows the zh-cn module. This patch
  * adds assets/en-olivia.js (the English module, same keys as zh-cn) to the archive,
  * registers it in the bundle's locale map,
  * picks the start language from localStorage `olivia.ui.language` (zh-cn | en); without a stored
    choice it stays zh-cn (the embedded browser may report en-US on Chinese Windows),
  * wraps the few Chinese literals that live outside the locale module.

In zh-cn mode every changed expression returns exactly what it returned before.
Each rewrite is anchored on exact text and is idempotent, so it can run on the original
bundle and on top of every other frontend patch, in any number of passes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

MAIN = 'assets/main-31595bd3.js'
LOCALE_MEMBER = 'assets/en-olivia.js'
MARKER = '/*olivia-native-locale-v1*/'
SOURCE = Path(__file__).resolve().parents[1] / 'runtime' / 'native_locale' / 'en.js'

STORAGE_KEY = 'olivia.ui.language'

# Defined once at the very start of the bundle. Only an explicit stored choice selects English;
# anything else, including blocked storage, keeps today's zh-cn.
PRELUDE = (
    'const oliviaNativeLang=(()=>{try{const e=localStorage.getItem("' + STORAGE_KEY + '");'
    'if(e==="en"||e==="zh-cn")return e}catch{}'
    'return"zh-cn"})(),'
    'oliviaNativeText=(e,t)=>oliviaNativeLang==="en"?t:e,'
    'oliviaNativeMonth=e=>oliviaNativeLang==="en"?["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][e]:e+1;'
)

LOADER_BEFORE = (
    't=async i=>{Ao=i,rs=(await Vl(Object.assign({"./locales/zh-cn.json":()=>Dl(()=>import("./zh-cn-bd81633d.js"),[],import.meta.url)}),'
    '`./locales/${Ao}.json`)).default};'
)
# Chinese is always loaded: an English key that is missing, or an English module that fails to load,
# shows the Chinese text instead of a raw key or an error.
LOADER_AFTER = (
    't=async i=>{Ao=i;const oliviaLocales=Object.assign({"./locales/zh-cn.json":()=>Dl(()=>import("./zh-cn-bd81633d.js"),[],import.meta.url),'
    '"./locales/en.json":()=>Dl(()=>import("./en-olivia.js"),[],import.meta.url)}),'
    'oliviaBase=(await Vl(oliviaLocales,"./locales/zh-cn.json")).default;'
    'rs=Ao==="zh-cn"?oliviaBase:{...oliviaBase,...(await Vl(oliviaLocales,`./locales/${Ao}.json`).catch(()=>({default:{}}))).default}};'
)

# Text rewrites that are not plain literals: (before, after).
RAW_REWRITES = (
    ('await e({lang:"zh-cn"});', 'await e({lang:oliviaNativeLang});'),
    ('["common_date_format",{month:i.getMonth()+1,day:i.getDate()}]',
     '["common_date_format",{month:oliviaNativeMonth(i.getMonth()),day:i.getDate()}]'),
    ('`上传失败: ${d.status}`',
     '`${oliviaNativeText("上传失败: ","Upload failed: ")}${d.status}`'),
)

# Chinese string literals outside the locale module: (chinese, english, occurrences, added_by_patch).
# The first group is in the base bundle. The second group is written into the bundle by our own
# mailbox/music patches, so an unpatched bundle has none of them and a patched one has exactly `occurrences`.
LITERALS = (
    ('请选择文件', 'Please choose a file', 1, False),
    ('上传过程中发生错误', 'An error occurred during the upload', 1, False),
    ('上传请求超时', 'The upload timed out', 1, False),
    ('上传被取消', 'Upload canceled', 1, False),
    ('当前设备暂不支持播放该视频', "This device can't play this video", 2, False),
    ('时间未知', 'Unknown time', 1, True),
    ('随机播放，点击切换为顺序播放', 'Shuffle on. Click to play in order', 1, True),
    ('顺序播放，点击切换为随机播放', 'Playing in order. Click to shuffle', 1, True),
    ('随机播放', 'Shuffle', 1, True),
    ('顺序播放', 'Play in order', 1, True),
    ('本轮不回复 · ', 'No reply · ', 1, True),
    ('本轮不回复', 'No reply this time', 1, True),
    ('收到回信后可以继续写信', 'After her reply', 1, True),
    ('等待林离回信', 'Waiting…', 1, True),
    ('这封回信暂时没能完成。你的信仍保留在信箱里。',
     "This reply couldn't be completed right now. Your letter is still in your mailbox.", 1, True),
    ('请保留诊断包，查看信件状态后再决定是否重寄。',
     "Please keep the diagnostic bundle, check the letter's status, then decide whether to send it again.", 1, True),
    ('林离洗澡中', 'Linli is taking a bath', 1, True),
    ('林离录视频中', 'Linli is recording a video', 1, True),
    ('林离正在录语音…', 'Linli is recording a voice message…', 1, True),
)

WRAP = 'oliviaNativeText('


def _js(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _rewrite_pair(source: str, before: str, after: str) -> str:
    """Replace `before` by `after` exactly once; accept a bundle that already has `after`."""
    old, new = source.count(before), source.count(after)
    if old == 1 and new == 0:
        return source.replace(before, after, 1)
    if old == 0 and new == 1:
        return source
    raise ValueError('NATIVE_LOCALE_ANCHOR_INVALID')


def _wrap_literal(source: str, chinese: str, english: str, expected: int, added_by_patch: bool) -> str:
    """Turn every bare "chinese" into oliviaNativeText("chinese","english")."""
    quoted = _js(chinese)
    bare = re.compile(r'(?<!' + re.escape(WRAP) + r')' + re.escape(quoted))
    wrapped = WRAP + quoted + ',' + _js(english) + ')'
    found = len(bare.findall(source)) + source.count(wrapped)
    if found != expected and not (added_by_patch and found == 0):
        raise ValueError('NATIVE_LOCALE_LITERAL_INVALID')
    return bare.sub(lambda _match: wrapped, source)


def patch_source(source: str) -> str:
    if source.count(MARKER) > 1:
        raise ValueError('NATIVE_LOCALE_MARKER_INVALID')
    source = _rewrite_pair(source, LOADER_BEFORE, LOADER_AFTER)
    for before, after in RAW_REWRITES:
        source = _rewrite_pair(source, before, after)
    for chinese, english, expected, added_by_patch in LITERALS:
        source = _wrap_literal(source, chinese, english, expected, added_by_patch)
    if MARKER not in source:
        source = MARKER + PRELUDE + source
    elif source.count(PRELUDE) != 1:
        raise ValueError('NATIVE_LOCALE_PRELUDE_INVALID')
    return source


def english_module() -> bytes:
    return SOURCE.read_bytes()


def patch_native_locale(path: Path | str) -> str:
    path = Path(path)
    module = english_module()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if MAIN not in names:
            return 'UNSUPPORTED_CLIENT'
        source = archive.read(MAIN).decode('utf-8')
        changed = patch_source(source).encode('utf-8')
        if changed == source.encode('utf-8') and LOCALE_MEMBER in names and archive.read(LOCALE_MEMBER) == module:
            return 'ALREADY_PATCHED'
        with tempfile.TemporaryDirectory(prefix='.native-locale-', dir=path.parent) as folder:
            staged = Path(folder) / 'feapp.dat'
            with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED) as target:
                for info in archive.infolist():
                    if info.filename == LOCALE_MEMBER:
                        continue
                    target.writestr(info, changed if info.filename == MAIN else archive.read(info))
                target.writestr(LOCALE_MEMBER, module)
            with zipfile.ZipFile(staged) as verify:
                if verify.testzip() is not None:
                    raise ValueError('NATIVE_LOCALE_ARCHIVE_INVALID')
            backup = path.with_name(path.name + '.locale.orig')
            if not backup.exists():
                shutil.copy2(path, backup)
            # Windows requires releasing the source ZIP before replacement.
            archive.close()
            os.replace(staged, path)
    return 'PATCHED'
