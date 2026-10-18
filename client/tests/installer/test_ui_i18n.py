"""English interface: the shared language helper, the dictionary and the wrapped strings of the injected scripts.

The injected scripts write their text as ``t("<Chinese source>")``. These tests check the dictionary against the
scripts (every wrapped string has an English entry, no Chinese in English text, placeholders match), the behaviour
of ``window.oliviaI18n`` (stored choice, system language, fallback, reload on change) and that the main panels render
without Chinese in English mode while Chinese mode keeps its text.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT
from runtime.call_avatar_ui import JAVASCRIPT as AVATAR_JAVASCRIPT
from runtime.personal_chat.setup_ui import PERSONAL_CHAT_SETUP_JAVASCRIPT
from runtime.ui_i18n import EN, I18N_JAVASCRIPT

HAN = re.compile(r"[一-鿿]")
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")

# The scripts that write their text through t(): the composed bootstrap (call page, piano dialog, update notice and
# the settings pages, without the dictionary at its head) and the QQ / WeChat binding panel. The avatar rig keeps
# internal tone keys and is not shown.
SOURCES = {
    "bootstrap": BOOTSTRAP_JAVASCRIPT.replace(AVATAR_JAVASCRIPT, "").replace(I18N_JAVASCRIPT, ""),
    "personal-chat setup": PERSONAL_CHAT_SETUP_JAVASCRIPT,
}

# Chinese text that is deliberately not translated, with the reason.
UNWRAPPED = {
    '"写下你的感受"': "native composer title, used to find the native dialog",
    '"寄出信件"': "native submit label, used to find the native button",
    '"用户在原版 Olivia 设置中明确纠正长期记忆。"': "audit reason sent to the service",
    '"用户在原版 Olivia 设置中明确删除长期记忆。"': "audit reason sent to the service",
    '"用户在原版 Olivia 设置中确认继续清空当前长期记忆。"': "audit reason sent to the service",
    '"用户在原版 Olivia 设置中明确清空当前长期记忆。"': "audit reason sent to the service",
    '"恢复"': "audit reason word, not shown",
    '"暂停"': "audit reason word, not shown",
    "'用户说：'": "prefix the service puts on stored sentences, compared as data",
    '"中文"': "language name, written in its own language",
    '"语言 / Language"': "bilingual by design",
    '"/连接测试"': "chat command the service recognises",
    "'开头'": "piano section value sent to the service (the label next to it is translated)",
    "'中段'": "piano section value sent to the service (the label next to it is translated)",
    "'结尾'": "piano section value sent to the service (the label next to it is translated)",
}
UNWRAPPED_TEMPLATE_PARTS = {"用户在原版 Olivia 设置中明确", " Mem0 长期记忆。"}


# --- a small JavaScript scanner: strings, template literals, regular expressions and comments -----------------------

_ID_END = re.compile(r"[A-Za-z0-9_$)\]}]")
_REGEX_KEYWORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else", "yield", "await"}


def _lex(src: str, i: int = 0, stop_at_brace: bool = False):
    """Return (tokens, index). Tokens are (kind, start, end, parts); parts are set for template literals."""
    n = len(src)
    toks = []
    brace = 0
    last_sig, last_word = "", ""
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            toks.append(("str", i, j + 1, None))
            i, last_sig, last_word = j + 1, '"', ""
        elif c == "`":
            parts, j, s0 = [], i + 1, i + 1
            while j < n and src[j] != "`":
                if src[j] == "\\":
                    j += 2
                elif src.startswith("${", j):
                    parts.append(("s", s0, j))
                    sub, k = _lex(src, j + 2, True)
                    parts.append(("e", j + 2, k, sub))
                    j = s0 = k + 1
                else:
                    j += 1
            parts.append(("s", s0, j))
            toks.append(("tpl", i, j + 1, parts))
            i, last_sig, last_word = j + 1, '"', ""
        elif c == "{":
            brace += 1
            i, last_sig, last_word = i + 1, "{", ""
        elif c == "}":
            if stop_at_brace and brace == 0:
                return toks, i
            brace -= 1
            i, last_sig, last_word = i + 1, "}", ""
        elif c == "/":
            is_regex = last_sig == "" or (not _ID_END.match(last_sig) and last_sig != '"') or last_word in _REGEX_KEYWORDS
            if last_sig == "}":
                is_regex = False
            if is_regex:
                j, in_class = i + 1, False
                while j < n and src[j] != "\n":
                    ch = src[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        break
                    j += 1
                k = j + 1
                while k < n and src[k].isalpha():
                    k += 1
                toks.append(("re", i, k, None))
                i, last_sig, last_word = k, '"', ""
            else:
                i, last_sig, last_word = i + 1, "/", ""
        else:
            m = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", src[i:i + 80])
            if m:
                last_word, last_sig = m.group(0), "a"
                i += len(last_word)
                continue
            m = re.match(r"[0-9][0-9A-Za-z_.]*", src[i:i + 40])
            if m:
                i, last_sig, last_word = i + len(m.group(0)), "0", ""
                continue
            i, last_sig, last_word = i + 1, c, ""
    return toks, i


def _walk(tokens):
    for token in tokens:
        yield token
        if token[0] == "tpl":
            for part in token[3]:
                if part[0] == "e":
                    yield from _walk(part[3])


def _decode(literal: str) -> str:
    body, out, i = literal[1:-1], [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r"}
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i])
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt == "u":
            out.append(chr(int(body[i + 2:i + 6], 16)))
            i += 6
        elif nxt == "x":
            out.append(chr(int(body[i + 2:i + 4], 16)))
            i += 4
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def _object_keys(source: str, start: int, spans) -> list[str] | None:
    """Names of the properties of the object literal that begins at ``start`` (after the comma of t(key, {...}))."""
    i = start
    while i < len(source) and source[i] in " \t\r\n":
        i += 1
    if i >= len(source) or source[i] != "{":
        return None
    depth, segment, segments = 0, [], []
    by_start = {s: e for s, e in spans}
    while i < len(source):
        if i in by_start:
            segment.append("0")
            i = by_start[i]
            continue
        ch = source[i]
        if ch in "{([":
            depth += 1
            if depth > 1:
                segment.append(ch)
        elif ch in "})]":
            depth -= 1
            if depth == 0:
                segments.append("".join(segment))
                break
            segment.append(ch)
        elif ch == "," and depth == 1:
            segments.append("".join(segment))
            segment = []
        else:
            segment.append(ch)
        i += 1
    names = []
    for part in segments:
        match = re.match(r"\s*([A-Za-z_$][\w$]*)\s*(?::|$)", part)
        if match:
            names.append(match.group(1))
    return names


def scan(source: str):
    """Return (wrapped keys, unwrapped Chinese string literals, Chinese static parts of template literals, calls).

    ``calls`` lists (key, names of the values passed or None) for every t() call.
    """
    tokens, _ = _lex(source)
    spans = [(start, end) for kind, start, end, _ in _walk(tokens) if kind in {"str", "tpl", "re"}]
    keys, bare, template_parts, calls = [], [], [], []
    for kind, start, end, parts in _walk(tokens):
        if kind == "str":
            literal = source[start:end]
            before = source[max(0, start - 40):start].rstrip()
            after = source[end:end + 20].lstrip()
            if re.search(r"(?<![\w$.])t\($", before) and after[:1] in {")", ","}:
                key = _decode(literal)
                keys.append(key)
                names = None
                if after[:1] == ",":
                    names = _object_keys(source, end + source[end:].index(",") + 1, spans)
                calls.append((key, names))
            elif HAN.search(literal):
                bare.append(literal)
        elif kind == "tpl":
            for part in parts:
                if part[0] == "s" and HAN.search(source[part[1]:part[2]]):
                    template_parts.append(source[part[1]:part[2]])
    return keys, bare, template_parts, calls


@pytest.fixture(scope="module")
def scanned():
    return {name: scan(source) for name, source in SOURCES.items()}


# --- dictionary against the scripts ---------------------------------------------------------------------------------

def test_every_wrapped_string_has_an_english_entry(scanned):
    keys = {key for found, _, _, _ in scanned.values() for key in found}
    assert len(keys) > 1300, "the scanner should find every t() call"
    missing = sorted(key for key in keys if key not in EN)
    assert missing == [], f"{len(missing)} wrapped strings have no English entry, e.g. {missing[:5]}"


def test_dictionary_has_no_unused_or_empty_entries(scanned):
    keys = {key for found, _, _, _ in scanned.values() for key in found}
    assert sorted(key for key in EN if key not in keys) == []
    assert [key for key, value in EN.items() if not value.strip()] == []


def test_every_placeholder_is_filled_by_its_call(scanned):
    wrong = []
    for name, (_, _, _, calls) in scanned.items():
        for key, names in calls:
            wanted = set(PLACEHOLDER.findall(key))
            if wanted and (names is None or not wanted <= set(names)):
                wrong.append((name, key[:60], sorted(wanted), names))
    assert wrong == []


def test_english_text_contains_no_chinese():
    assert [key for key, value in EN.items() if HAN.search(value)] == []


def test_placeholders_match_between_source_and_english():
    wrong = {key: value for key, value in EN.items() if sorted(PLACEHOLDER.findall(key)) != sorted(PLACEHOLDER.findall(value))}
    assert wrong == {}


def test_chinese_text_outside_t_is_a_known_exception(scanned):
    for name, (_, bare, template_parts, _) in scanned.items():
        unknown = [literal for literal in bare if literal not in UNWRAPPED]
        assert unknown == [], f"{name}: Chinese text that is not wrapped: {unknown[:5]}"
        assert set(template_parts) <= UNWRAPPED_TEMPLATE_PARTS, f"{name}: Chinese in a template literal: {template_parts[:3]}"


# Full-width marks and the Chinese double ellipsis are Chinese typography too: outside t() they would show in English.
FULL_WIDTH = re.compile(r"[　-〿＀-￯]|……")
FULL_WIDTH_OUTSIDE_T = {
    "'？?'": "call page: punctuation the spoken-text cue detector looks for",
    "'！!'": "call page: punctuation the spoken-text cue detector looks for",
    "'~～'": "call page: punctuation the spoken-text cue detector looks for",
    '"　"': "the Chinese branch of the gap between a time and a title (English uses a plain space)",
}


def test_full_width_punctuation_outside_t_is_a_known_exception():
    for name, source in SOURCES.items():
        tokens, _ = _lex(source)
        found = []
        for kind, start, end, parts in _walk(tokens):
            if kind == "str":
                before = source[max(0, start - 40):start].rstrip()
                if not re.search(r"(?<![\w$.])t\($", before) and FULL_WIDTH.search(source[start:end]):
                    found.append(source[start:end])
            elif kind == "tpl":
                found += [source[part[1]:part[2]] for part in parts if part[0] == "s" and FULL_WIDTH.search(source[part[1]:part[2]])]
        known = set(FULL_WIDTH_OUTSIDE_T) | set(UNWRAPPED) | UNWRAPPED_TEMPLATE_PARTS
        assert [literal for literal in found if literal not in known] == [], name


def test_audit_reasons_and_native_labels_stay_chinese():
    source = SOURCES["bootstrap"]
    assert 'const LETTER_COMPOSER_TITLE = "写下你的感受";' in source
    assert 'const LETTER_SUBMIT_LABEL = "寄出信件";' in source
    assert "reason: `用户在原版 Olivia 设置中明确${action} Mem0 长期记忆。`" in source
    assert "{command: \"/连接测试\"}" in SOURCES["personal-chat setup"]


def test_the_helper_comes_first_in_the_bootstrap():
    assert BOOTSTRAP_JAVASCRIPT.startswith("try {" + I18N_JAVASCRIPT)
    assert BOOTSTRAP_JAVASCRIPT.count("window.oliviaI18n = Object.freeze") == 1


# --- the helper in a bare JavaScript context ------------------------------------------------------------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js unavailable")

HELPER_HARNESS = r"""
const vm = require('node:vm');
const spec = JSON.parse(process.argv[1]);
const prelude = require('node:fs').readFileSync(process.argv[2], 'utf8');
const store = Object.assign({}, spec.stored || {});
const log = {reloads: 0, writes: []};
const win = {location: {reload() { log.reloads += 1; }}};
if (!spec.noStorage) {
  win.localStorage = spec.brokenStorage
    ? {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }}
    : {getItem: key => (key in store ? store[key] : null), setItem(key, value) { store[key] = String(value); log.writes.push([key, String(value)]); }};
}
if (spec.language !== undefined) win.navigator = {language: spec.language};
const context = vm.createContext({window: win});
vm.runInContext(prelude, context);
vm.runInContext(prelude, context);
const api = win.oliviaI18n;
const data = process.argv[3] ? JSON.parse(require('node:fs').readFileSync(process.argv[3], 'utf8')) : null;
const out = (new Function('api', 'log', 'store', 'data', spec.code))(api, log, store, data);
process.stdout.write(JSON.stringify({out, log, store}));
"""


def run_helper(code: str, tmp_path: Path, data=None, **spec) -> dict:
    prelude = tmp_path / "prelude.js"
    prelude.write_text(I18N_JAVASCRIPT, encoding="utf-8")
    arguments = [str(prelude)]
    if data is not None:
        payload = tmp_path / "data.json"
        payload.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        arguments.append(str(payload))
    result = subprocess.run(
        [NODE, "-e", HELPER_HARNESS, json.dumps({**spec, "code": code}), *arguments],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
@pytest.mark.parametrize("language", ["zh-CN", "zh-TW", "en-US", "en-GB", "de-DE", "ja", "", None])
def test_the_default_is_chinese_whatever_the_browser_language_is(tmp_path, language):
    # The embedded browser may report an English language on a Chinese system: only the switch can select English.
    spec = {} if language is None else {"language": language}
    assert run_helper("return api.lang()", tmp_path, **spec)["out"] == "zh-cn"


@needs_node
def test_a_stored_choice_wins_and_bad_values_are_ignored(tmp_path):
    stored = {"olivia.ui.language": "en"}
    assert run_helper("return api.lang()", tmp_path, language="zh-CN", stored=stored)["out"] == "en"
    stored = {"olivia.ui.language": "zh-cn"}
    assert run_helper("return api.lang()", tmp_path, language="en-US", stored=stored)["out"] == "zh-cn"
    stored = {"olivia.ui.language": "fr"}
    assert run_helper("return api.lang()", tmp_path, language="en-US", stored=stored)["out"] == "zh-cn"
    assert run_helper("return api.lang()", tmp_path, language="zh-CN", stored=stored)["out"] == "zh-cn"
    stored = {"olivia.ui.language": "EN"}
    assert run_helper("return api.lang()", tmp_path, language="zh-CN", stored=stored)["out"] == "zh-cn"


@needs_node
def test_blocked_or_missing_storage_and_navigator_never_throw(tmp_path):
    assert run_helper("return api.lang()", tmp_path, language="en-US", brokenStorage=True)["out"] == "zh-cn"
    assert run_helper("return api.lang()", tmp_path, language="en-US", noStorage=True)["out"] == "zh-cn"
    assert run_helper("return api.lang()", tmp_path)["out"] == "zh-cn"
    # a choice that cannot be stored cannot take effect either: the page reloads and stays Chinese
    done = run_helper("api.set('en'); return api.t('取消')", tmp_path, language="en-US", brokenStorage=True)
    assert done["out"] == "取消" and done["log"]["reloads"] == 1


@needs_node
def test_set_stores_the_choice_and_reloads_once(tmp_path):
    result = run_helper("api.set('en'); return api.lang()", tmp_path, language="zh-CN")
    assert result["out"] == "en"
    assert result["store"] == {"olivia.ui.language": "en"}
    assert result["log"]["reloads"] == 1
    result = run_helper("api.set('zh-CN'); return api.lang()", tmp_path, language="en-US", stored={"olivia.ui.language": "en"})
    assert result["out"] == "zh-cn" and result["store"] == {"olivia.ui.language": "zh-cn"}
    result = run_helper("api.set('fr'); api.set(undefined); return api.lang()", tmp_path, language="en-US")
    assert result["store"] == {} and result["log"]["reloads"] == 0


@needs_node
def test_t_translates_in_english_and_keeps_chinese_otherwise(tmp_path):
    english = {"olivia.ui.language": "en"}
    chinese = {"olivia.ui.language": "zh-cn"}
    code = "return [api.t('取消'), api.t('已选 {count} 封', {count: 3}), api.t('没有这一条'), api.t('{x} 与 {y}', {x: 1}), api.t(undefined), api.t('取消', null)]"
    assert run_helper(code, tmp_path, stored=english)["out"] == [
        "Cancel", "3 selected", "没有这一条", "1 与 {y}", None, "Cancel",
    ]
    assert run_helper(code, tmp_path, stored=chinese)["out"] == [
        "取消", "已选 3 封", "没有这一条", "1 与 {y}", None, "取消",
    ]
    # replacement text is inserted literally, and a value is never read as another placeholder
    out = run_helper("return [api.t('已选 {count} 封', {count: '$&{count}'}), api.t('constructor'), api.t('{a}', {a: '{b}', b: 'x'})]", tmp_path, stored=english)["out"]
    assert out == ["$&{count} selected", "constructor", "{b}"]


@needs_node
def test_helper_is_defined_once_and_is_not_replaceable(tmp_path):
    out = run_helper("const first = api; return [Object.isFrozen(first), typeof first.t, typeof first.lang, typeof first.set]", tmp_path, language="en-US")["out"]
    assert out == [True, "function", "function", "function"]


@needs_node
def test_chinese_mode_returns_every_source_string_unchanged(tmp_path):
    keys = [key for key in EN if not PLACEHOLDER.search(key)]
    out = run_helper("return data.filter(key => api.t(key) !== key)", tmp_path, data=keys, stored={"olivia.ui.language": "zh-cn"})["out"]
    assert out == []
    # with placeholders: the source text with the values filled in, nothing else
    keyed = [key for key in EN if PLACEHOLDER.search(key)]
    code = r"return data.filter(key => api.t(key, Object.fromEntries([...key.matchAll(/\{(\w+)\}/g)].map(m => [m[1], '<' + m[1] + '>']))) !== key.replace(/\{(\w+)\}/g, '<$1>'))"
    assert run_helper(code, tmp_path, data=keyed, stored={"olivia.ui.language": "zh-cn"})["out"] == []
    filled = run_helper("return api.t('第 {page} / {pages} 页', {page: 2, pages: 5})", tmp_path, stored={"olivia.ui.language": "zh-cn"})["out"]
    assert filled == "第 2 / 5 页"


@needs_node
def test_loading_the_bootstrap_twice_does_not_redefine_the_helper(tmp_path):
    script = tmp_path / "twice.js"
    script.write_text(
        "const vm=require('node:vm');const win={navigator:{language:'en-US'},location:{reload(){}}};"
        "const ctx=vm.createContext({window:win,document:{}});"
        f"const src=require('node:fs').readFileSync({json.dumps(str(tmp_path / 'prelude.js'))},'utf8');"
        "vm.runInContext(src,ctx);const first=win.oliviaI18n;vm.runInContext(src,ctx);"
        "process.stdout.write(String(first===win.oliviaI18n));",
        encoding="utf-8",
    )
    (tmp_path / "prelude.js").write_text(I18N_JAVASCRIPT, encoding="utf-8")
    assert subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30).stdout == "true"


# --- panels in a browser: English mode shows no Chinese, Chinese mode keeps its text ---------------------------------

ORIGIN = "http://127.0.0.1:27149"
TOKEN = "s" * 40
# Chinese that stays on screen in English mode: the bilingual language names and the chat command the service reads.
ALLOWED_IN_ENGLISH = {"语言 / Language", "中文"}
COMMAND_LINE = re.compile(r"/连接测试")

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#18191c;color:#ece9e2;font:15px/1.6 sans-serif}header{display:flex;justify-content:flex-end;padding:14px 24px}
main{max-width:760px;margin:0 auto;padding:24px}.tp-settings-item{padding:8px 0}</style>
<script src="%(origin)s/assets/olivia-companion-settings.js" data-olivia-companion-settings="p03.original-settings-shell.v1"
 data-ui-version="test" data-api-base="%(origin)s/"></script></head>
<body><script data-olivia-personal-chat-bindings="v1" data-api-base="%(origin)s/">%(chat)s</script>
<header><span>Resonance Edition</span></header>
<main><div class="tp-settings-item"><div>Settings</div></div></main></body></html>"""

MODELS = [
    {"id": "model-a", "display_name": "Model A", "input_multiplier": "0.575", "output_multiplier": "0.575",
     "pricing": {"input_per_million_yuan": "9.28", "output_per_million_yuan": "46.4"}, "description": "Warm and steady."},
    {"id": "model-b", "display_name": "Model B", "input_multiplier": "0.14", "output_multiplier": "0.18",
     "pricing": {"input_per_million_yuan": "2.3", "output_per_million_yuan": "14.3"}, "description": "Quick and light."},
]
CHAT_STATES = {
    "choice": {"selected_channels": [], "configured": {}, "listeners": {}},
    "login": {"selected_channels": ["qq"], "configured": {"qq": False}, "listeners": {}, "qq": {"state": "SETUP_REQUIRED"},
              "napcat": {"state": "AWAITING_QQ_LOGIN", "installed": True}},
    "ready": {"selected_channels": ["qq"], "configured": {"qq": True}, "listeners": {"qq": "CONNECTED"},
              "qq": {"state": "CONNECTED", "owner_masked": "12****90", "can_reuse_owner": True},
              "napcat": {"state": "RUNNING", "installed": True}, "connection_test_pending": []},
}
PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _reply(path: str, body, chat: dict) -> dict:
    """Synthetic answers of the local service; nothing here is a real account."""
    action = (body or {}).get("action")
    if path == "/toy/setup/status":
        return {"status": "READY", "session_token": TOKEN, "setup_completed": True, "show_initial_setup": False,
                "llm": {"key_configured": True}}
    if path == "/toy/relay/action":
        return {"account": {"configured": True, "key_prefix": "olivia-test"},
                "models": {"selected_model": "model-a", "models": MODELS},
                "balance": {"billing_mode": "money", "remaining_yuan": "42.1", "used_yuan": "7.9",
                            "usage": {"calls": 12, "total_tokens": 1234, "input_tokens": 1000, "output_tokens": 234}},
                "statement": {"items": [{"label": "Reply", "created_at": "2026-10-17T10:00:00Z", "status": "settled",
                                         "charged_yuan": "0.0123", "reserved_yuan": "0"}],
                              "reserved_yuan": "0", "minimum_charge_yuan": "0.01"},
                "order_status": {"order": None}}.get(action, {})
    if path == "/toy/generation/action":
        return {"status": "READY", **({"has_key": True} if action == "settings_status" else {"remaining_yuan": "42.1"})}
    if path == "/toy/settings/reply-routes":
        return {"code": 0, "data": {"tier": "audio", "tier_configured": True, "image": {"enabled": True, "resolution": "1K"},
                                    "image_capability": {"default_model": "img", "models": [
                                        {"id": "img", "display_name": "Image A", "resolutions": ["1K"],
                                         "price_ranges_cents": {"1K": [20, 30]}}]}}}
    if path == "/toy/sticker-packs":
        return {"code": 0, "data": {"folder": "C:\\Olivia\\stickers", "packs": []}}
    if path == "/toy/updates/local/status":
        return {"version": "2.4.5"}
    if path == "/toy/call/config":
        return {"code": 200, "data": {"available": True, "models": [{"id": "flash", "cents_per_minute": 40}], "cents_per_minute": 40}}
    if path == "/toy/autoupdate/state":
        return {"code": 200, "data": {"state": "ready", "version": "2.4.6",
                                      "notes": {"title": "Smoother letters", "items": [{"title": "Letters", "text": "Calmer."}]}}}
    if path == "/toy/personal-chat/setup/status":
        return chat
    if path == "/toy/personal-chat/setup/qq/napcat/login":
        return {"qr_data": PIXEL, "logged_in": False, "scanned": False}
    return {"code": 200, "data": {}}


def _launch(playwright):
    for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch(headless=True, **options)
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    pytest.skip("No Chromium-based browser available")


def _boot(browser, language=None, chat="choice", width=1100, height=1000, locale="en-US"):
    """The whole injected script in a page, answered by synthetic data."""
    page = browser.new_page(viewport={"width": width, "height": height}, locale=locale)
    if language:
        page.add_init_script(f"try{{localStorage.setItem('olivia.ui.language','{language}')}}catch(_){{}}")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.errors = errors
    state = CHAT_STATES[chat]

    def handle(route):
        request = route.request
        path = request.url[len(ORIGIN):].split("?")[0]
        if path == "/assets/olivia-companion-settings.js":
            return route.fulfill(content_type="application/javascript", body=BOOTSTRAP_JAVASCRIPT)
        if path == "/":
            return route.fulfill(content_type="text/html", body=PAGE % {"origin": ORIGIN, "chat": PERSONAL_CHAT_SETUP_JAVASCRIPT})
        try:
            body = request.post_data_json if request.method == "POST" else None
        except Exception:  # noqa: BLE001
            body = None
        route.fulfill(content_type="application/json", body=json.dumps(_reply(path, body, state)))

    page.route(ORIGIN + "/**", handle)
    page.goto(ORIGIN + "/#/settings")
    page.wait_for_selector("[data-olivia-group=language]")
    page.wait_for_timeout(1200)
    return page


def _chinese(page, selector="body"):
    return [line for line in page.inner_text(selector).split("\n") if HAN.search(line)]


def _unexpected(lines):
    return [line for line in lines if line not in ALLOWED_IN_ENGLISH and not COMMAND_LINE.search(line)]


def test_language_switch_follows_the_choice_and_survives_a_reload():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        # no stored choice yet: Chinese
        page = _boot(browser, locale="en-US")
        assert page.get_by_text("林离怎么回复").is_visible()
        page.evaluate("document.querySelector('[data-olivia-group=language]').open = true")
        group = page.locator("[data-olivia-group=language]")
        assert group.get_by_role("radio", name="中文").get_attribute("aria-checked") == "true"
        group.get_by_role("radio", name="English").click()
        page.get_by_text("How she replies").wait_for()
        assert page.evaluate("localStorage.getItem('olivia.ui.language')") == "en"
        assert page.get_by_text("林离怎么回复").count() == 0
        # the choice survives another reload, and the switch back works too
        page.reload()
        page.get_by_text("How she replies").wait_for()
        page.evaluate("document.querySelector('[data-olivia-group=language]').open = true")
        group = page.locator("[data-olivia-group=language]")
        assert group.get_by_role("radio", name="English").get_attribute("aria-checked") == "true"
        group.get_by_role("radio", name="中文").click()
        page.get_by_text("林离怎么回复").wait_for()
        assert page.evaluate("localStorage.getItem('olivia.ui.language')") == "zh-cn"
        assert page.errors == []
        browser.close()


def test_english_needs_a_choice_and_chinese_is_the_default_in_any_browser_language():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        # an English browser without a stored choice still shows Chinese
        default = _boot(browser, locale="en-US")
        assert default.get_by_text("林离怎么回复").is_visible()
        assert default.get_by_text("How she replies").count() == 0
        english = _boot(browser, language="en", locale="zh-CN")
        assert english.get_by_text("How she replies").is_visible()
        assert english.get_by_text("Update ready").is_visible()
        assert english.get_by_role("button", name="Call").is_visible()
        chinese = _boot(browser, language="zh-cn", locale="en-US")
        assert chinese.get_by_text("林离怎么回复").is_visible()
        assert chinese.get_by_text("新版本已准备好").is_visible()
        assert chinese.get_by_role("button", name="打电话").is_visible()
        assert chinese.get_by_role("button", name="账户 ¥42.10").is_visible()
        browser.close()


@pytest.mark.parametrize("group", ["language", "reply", "chat", "letters", "help"])
def test_english_settings_groups_show_no_chinese(group):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        page = _boot(browser, language="en")
        page.evaluate("(group) => { document.querySelector(`[data-olivia-group=${group}]`).open = true }", group)
        page.wait_for_timeout(800)
        assert _unexpected(_chinese(page)) == []
        assert page.errors == []
        browser.close()


@pytest.mark.parametrize("chat", ["choice", "login", "ready"])
def test_english_chat_binding_panel_shows_no_chinese(chat):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        page = _boot(browser, language="en", chat=chat)
        page.evaluate("document.querySelector('[data-olivia-group=chat]').open = true")
        page.wait_for_timeout(1000)
        assert _unexpected(_chinese(page, "[data-olivia-group-body=chat]")) == []
        assert page.locator("[data-olivia-group-body=chat] button").count() > 0
        browser.close()


def test_english_header_account_update_and_call_show_no_chinese():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        page = _boot(browser, language="en")
        assert _unexpected(_chinese(page, "header")) == []
        page.click(".olivia-update-entry")
        assert _unexpected(_chinese(page, ".olivia-update")) == []
        assert page.get_by_role("button", name="Restart and update now").is_visible()
        page.keyboard.press("Escape")
        page.click(".olivia-call-entry")
        assert _unexpected(_chinese(page, ".olivia-call")) == []
        assert page.get_by_role("button", name="Dial").is_visible()
        page.evaluate("document.querySelectorAll('.olivia-call').forEach(node => node.remove())")
        page.click("[data-olivia-account-entry]")
        page.wait_for_timeout(1800)
        dialog = "[data-olivia-companion-settings-dialog]"
        assert page.get_by_role("heading", name="Reply model").is_visible()
        assert _unexpected(_chinese(page, dialog)) == []
        for tab in ("Usage history", "Key"):
            page.get_by_role("button", name=tab, exact=True).click()
            page.wait_for_timeout(500)
            assert _unexpected(_chinese(page, dialog)) == []
        assert page.errors == []
        browser.close()


def test_english_memory_dialog_shows_no_chinese():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        page = _boot(browser, language="en")
        page.evaluate("document.querySelector('[data-olivia-group=letters]').open = true")
        page.get_by_role("button", name="Open", exact=True).first.click()
        page.wait_for_timeout(2000)
        assert page.get_by_text("Long-term memory").first.is_visible()
        assert _unexpected(_chinese(page, "[data-olivia-companion-settings-dialog]")) == []
        browser.close()


def test_english_memory_dialog_opens_the_letters_and_memories_window():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    from tests.installer.letters_memories_fixture import Service, boot
    with sync_playwright() as p:
        browser = _launch(p)
        page = boot(browser, Service("en"), language="en", hash="#/settings")
        page.wait_for_selector("[data-olivia-group=letters]")
        page.evaluate("document.querySelector('[data-olivia-group=letters]').open = true")
        page.get_by_role("button", name="Open", exact=True).first.click()
        page.get_by_role("button", name="Open letters and memories").click()
        window = page.locator("[data-letter-deletion]")
        window.locator(".om-row").first.wait_for()
        assert window.get_by_role("tab", name="Memories").get_attribute("aria-selected") == "true"
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        assert page.errors == []
        browser.close()


@pytest.mark.parametrize("language,title,submit,limited", [
    ("zh-cn", "写下你的感受", "寄出信件", True),
    ("en", "Write down how you feel", "Send letter", True),
    ("en", "Share how you feel", "Send the letter", True),
    ("en", "Something else entirely", "Send letter", False),
    ("zh-cn", "Write down how you feel", "Send letter", False),
])
def test_native_composer_is_found_by_its_chinese_or_english_labels(language, title, submit, limited):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = f'<div role="dialog"><h2>{title}</h2><textarea id="letter"></textarea><button>{submit}</button></div>'
    with sync_playwright() as p:
        browser = _launch(p)
        page = _boot(browser, language=language)
        page.evaluate("(html) => document.body.insertAdjacentHTML('beforeend', html)", html)
        page.wait_for_timeout(600)
        assert page.evaluate("document.querySelector('#letter').maxLength") == (1200 if limited else -1)
        browser.close()


@pytest.mark.parametrize("language,title", [("zh-cn", "信箱"), ("en", "Mailbox"), ("en", "Music Desktop")])
def test_main_navigation_clears_the_native_page_title(language, title):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = ('<h1 id="native-title" style="position:absolute;left:20px;top:56px;margin:0;font:700 30px/44px sans-serif">'
            + title + "</h1>")
    with sync_playwright() as p:
        browser = _launch(p)
        page = _boot(browser, language=language)
        page.evaluate("(html) => { document.body.insertAdjacentHTML('afterbegin', html); location.hash = '#/collection'; }", html)
        page.wait_for_selector("[data-olivia-main-navigation] a")
        page.wait_for_timeout(500)
        title_right = page.evaluate("document.querySelector('#native-title').getBoundingClientRect().width") + 20
        first_pill = page.evaluate("document.querySelector('[data-olivia-main-navigation] a').getBoundingClientRect().left")
        assert first_pill >= title_right, (first_pill, title_right)
        if language == "zh-cn":
            assert first_pill == 120  # unchanged: the Chinese title is narrower than the fixed position
        browser.close()


def _sliced_page(browser, language: str, body: str, html: str, viewport=None):
    page = browser.new_page(viewport=viewport or {"width": 1000, "height": 850})
    # a real origin, so that the chosen language can be stored the way the switch stores it
    page.add_init_script(f"try{{localStorage.setItem('olivia.ui.language','{language}')}}catch(_){{}}")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.errors = errors
    page.route("http://slice.test/", lambda route: route.fulfill(content_type="text/html", body=html))
    page.goto("http://slice.test/")
    page.add_script_tag(content=I18N_JAVASCRIPT)
    page.add_script_tag(content="window.t = window.oliviaI18n.t;" + body)
    return page


def _source_between(start: str, end: str) -> str:
    source = BOOTSTRAP_JAVASCRIPT
    return source[source.index(start):source.index(end)]


HELPERS = _source_between("  const text = (tag, value, className)", "  const setButtonsBusy =")


def test_english_letters_and_memories_window_shows_no_chinese_on_any_tab():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    from tests.installer.letters_memories_fixture import Service, boot
    with sync_playwright() as p:
        browser = _launch(p)
        page = boot(browser, Service("en"), language="en")
        page.wait_for_selector("[data-letter-deletion-entry]")
        assert page.locator("[data-letter-deletion-entry]").inner_text() == "Letters and memories"
        assert _unexpected(_chinese(page, "[data-olivia-main-navigation]")) == []
        page.click("[data-letter-deletion-entry]")
        window = page.locator("[data-letter-deletion]")
        window.locator(".lm-title").first.wait_for()
        assert window.get_by_role("tab").all_inner_texts() == ["Letters", "Memories", "Recently deleted"]
        # letters: the list, an expanded exchange, the search, a long result with its full text, the linked-letter strip
        window.locator(".lm-list > div").nth(0).get_by_role("button").click()
        page.wait_for_timeout(300)
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        window.locator(".lm-search input").fill("rain")
        window.get_by_text("matches found").wait_for()
        assert window.get_by_role("button", name="Load full text").is_visible()
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        window.get_by_role("button", name="Load full text").click()
        page.wait_for_timeout(300)
        window.locator(".lm-search input").fill("zzzz")
        window.get_by_text("No matching letters. Try other keywords.").wait_for()
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        window.locator(".lm-search input").fill("")
        page.wait_for_timeout(400)
        # memories: the browser, its management and a confirmation, then the hand-over to the letter tab
        window.get_by_role("tab", name="Memories").click()
        window.locator(".om-row").first.wait_for()
        window.locator(".om-heading button").click()
        page.wait_for_timeout(300)
        assert window.locator(".om-heading button").inner_text() == "Memory settings"
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        window.get_by_role("button", name="Clear this user's memory").click()
        page.locator("[data-letter-confirm]").wait_for()
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        window.get_by_role("button", name="View linked original").click()
        window.get_by_text("Viewing linked original text").wait_for()
        window.get_by_role("button", name="Show all letters").wait_for()
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        # recently deleted
        window.get_by_role("tab", name="Recently deleted").click()
        page.wait_for_timeout(400)
        assert _unexpected(_chinese(page, "[data-letter-deletion]")) == []
        assert page.errors == []
        browser.close()


def test_english_diary_pages_show_no_chinese():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    source = BOOTSTRAP_JAVASCRIPT
    diary = (source[source.index("  const DIARY_ROUTE ="):source.index("  const mountWorldPage =")]
             + source[source.index("  const goWorld ="):source.index("  const openWardrobe =")]
             + source[source.index("  const diaryStyle ="):source.index("  const mountCamerasPage =")])
    body = HELPERS + """
      const apiBase = 'http://127.0.0.1:27149';
      const entries = [{day: '2026-10-07', title: 'Exam week', mood: 'Worried', excerpt: 'Today you said...', short: false, seen: false,
                        commented: false, written_at: '2026-10-07T19:00:00Z'}];
      const routeRequest = async (path) => {
        if (path.startsWith('/toy/diary?')) return {entries, total: 1, unseen: 1, enabled: true};
        if (path.startsWith('/toy/diary/entry')) return {...entries[0], body: 'Today you said you have an exam.', comments: [{text: 'Thank you', written_at: '2026-10-08T01:00:00Z'}]};
        throw Error('unexpected ' + path);
      };
    """ + diary + "\nmountDiaryPage(document.querySelector('#page'));"
    with sync_playwright() as p:
        browser = _launch(p)
        page = _sliced_page(browser, "en", body, '<div id="app"><main id="page"></main></div>')
        page.get_by_text("Exam week").wait_for()
        assert page.get_by_text("Wed, Oct 7").is_visible()
        assert page.get_by_role("button", name="Gather earlier memories").is_visible()
        assert _unexpected(_chinese(page, "#page")) == []
        page.get_by_text("Exam week").click()
        page.get_by_text("Your comments").wait_for()
        assert _unexpected(_chinese(page, "#page")) == []
        assert page.errors == []
        browser.close()


def test_english_relationship_panel_fits_its_labels():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    reader = BOOTSTRAP_JAVASCRIPT.split("  const requestJson = ", 1)[1].split("  const publishProactiveState", 1)[0]
    renderer = BOOTSTRAP_JAVASCRIPT.split("    const relationshipPanel = ", 1)[1].split("    const relationship = relationshipPanel();", 1)[0]
    data = {"status": "READY", "version": 7, "route": "romantic", "routes": {"friend": "Friend", "romantic": "Romantic"}, "volume": 4,
            "title": "Trust", "description": "Easy rhythm.", "dynamic": "More trust lately.",
            "dimensions": {key: {"text": "Steady.", "rising": key == "trust"} for key in ("familiarity", "trust", "comfort", "closeness")},
            "moments": [{"date": "2026-10-16", "text": "You finished something", "quote": "Sunny day"}], "plans": [],
            "addressing": {"linli_calls_user": "", "user_calls_linli": ""}, "hint": "Maybe talk."}
    body = """
      const alive = () => true;
      const text = (tag, value, cls = '') => { const e = document.createElement(tag); e.textContent = value; e.className = cls; return e };
      const button = (label, handler) => { const e = text('button', label); e.onclick = handler; return e };
      const apiBase = 'http://relationship.test', CONFIRM_HEADER = 'x-c', CONFIRM_VALUE = '1';
      const VIDEO_CAPABILITY_PATH = 'vc', VIDEO_REPLY_SETTINGS_PATH = 'vs', MEMORY_PATH = 'm', DAILY_LIFE_PATH = 'd', STATUS_PATH = 's', PROACTIVE_STATUS_PATH = 'p', LOCAL_LETTER_IMPORT_PATH = 'l';
      const requestJson = """ + reader + "\nwindow.makePanel = " + renderer
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 860, "height": 1000})
        page.add_init_script("try{localStorage.setItem('olivia.ui.language','en')}catch(_){}")
        page.route("http://relationship.test/", lambda r: r.fulfill(content_type="text/html", body="<main id='panel'></main>"))
        page.route("http://relationship.test/toy/relationship**", lambda r: r.fulfill(content_type="application/json", body=json.dumps({"code": 0, "data": data})))
        page.goto("http://relationship.test/")
        page.add_script_tag(content=I18N_JAVASCRIPT)
        page.add_script_tag(content="window.t = window.oliviaI18n.t;" + body)
        page.evaluate("async () => { const panel = window.makePanel(); document.querySelector('#panel').replaceChildren(panel); panel.open = true; await panel.refresh() }")
        page.get_by_text("Romantic · Volume Four: “Trust”").wait_for()
        assert page.locator("dt").all_text_contents() == ["Familiarity", "Trust", "Comfort", "Closeness"]
        # a label is never broken across lines
        assert page.evaluate("[...document.querySelectorAll('dt')].every(el => el.getBoundingClientRect().height < 30)")
        assert _unexpected(_chinese(page, "#panel")) == []
        browser.close()
