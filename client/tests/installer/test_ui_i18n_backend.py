"""English wording for the text the backend writes: the dictionary, Chinese mode unchanged, English mode in Chinese-free text."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from runtime import ui_language
from runtime.ui_i18n import EN as PAGE_EN
from runtime.ui_i18n_backend import EN, tr, tr_in

ROOT = Path(__file__).parents[2]
HAN = re.compile(r"[一-鿿]")
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")

# Chinese that may stay inside English text: the chat command the services recognise.
ALLOWED_CHINESE_IN_ENGLISH = {"/连接测试"}
# The same Chinese word with a different job in the pages (a label) and in a sentence here.
DIFFERENT_ON_PURPOSE = {"开头", "中段", "结尾", "熟悉"}


def _sources():
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("tests/", "tools/", "scripts/", "control_center/", ".evidence/")):
            continue
        yield relative, path


def _calls(name: str):
    """Every ``tr(...)`` / ``tr_in(language, ...)`` call in the client sources: (file, line, literal, keywords)."""
    found = []
    for relative, path in _sources():
        if relative == "runtime/ui_i18n_backend.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
                index = 1 if name == "tr_in" else 0
                argument = node.args[index] if len(node.args) > index else None
                literal = argument.value if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else None
                found.append((relative, node.lineno, literal, {kw.arg for kw in node.keywords}, argument))
    return found


def _string_constants() -> set[str]:
    values = set()
    for relative, path in _sources():
        if relative == "runtime/ui_i18n_backend.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.add(node.value)
    return values


# --- the dictionary ----------------------------------------------------------------------------------------------

def test_english_text_contains_no_chinese_except_the_chat_command():
    for source, english in EN.items():
        assert english.strip(), source
        assert not HAN.search(re.sub("|".join(map(re.escape, ALLOWED_CHINESE_IN_ENGLISH)), "", english)), (source, english)


def test_placeholders_match_between_source_and_english():
    for source, english in EN.items():
        assert set(PLACEHOLDER.findall(source)) == set(PLACEHOLDER.findall(english)), source


def test_every_source_is_chinese():
    for source in EN:
        assert HAN.search(source) or source == "{date} ", source  # the date is wrapped so English can say "on"


def test_every_translated_call_has_an_entry_and_fills_its_placeholders():
    calls = _calls("tr") + _calls("tr_in")
    assert len(calls) > 40
    for relative, line, literal, keywords, argument in calls:
        assert not isinstance(argument, ast.JoinedStr), f"{relative}:{line} passes an f-string; use placeholders"
        if literal is None:
            continue  # a value looked up in a table that is itself translated at its source (checked below)
        if HAN.search(literal):
            assert literal in EN, f"{relative}:{line} has no English entry for {literal!r}"
        assert set(PLACEHOLDER.findall(literal)) <= keywords, f"{relative}:{line} does not pass {PLACEHOLDER.findall(literal)}"


# Sentences that sit inside a longer script text rather than being a string of their own.
EMBEDDED = {"本地视频暂时无法播放。": "patch_webplayer.py (the web player script)"}


def test_no_entry_is_unused():
    constants = _string_constants()
    unused = [source for source in EN
              if source not in constants and not (source in EMBEDDED and any(source in text for text in constants))]
    assert unused == []


def test_words_shared_with_the_pages_read_the_same():
    for source, english in EN.items():
        if source in PAGE_EN and source not in DIFFERENT_ON_PURPOSE:
            assert PAGE_EN[source] == english, source


def test_chinese_mode_returns_every_source_unchanged_and_fills_placeholders():
    assert ui_language.current() == "zh-cn"
    for source in EN:
        values = {name: f"<{name}>" for name in PLACEHOLDER.findall(source)}
        assert tr(source, **values) == PLACEHOLDER.sub(lambda m: f"<{m.group(1)}>", source), source
        assert tr_in("zh-cn", source, **values) == tr(source, **values)
        assert tr_in(None, source, **values) == tr(source, **values)


def test_english_mode_returns_the_entry_and_falls_back_to_the_source_when_there_is_none():
    ui_language.set_language("en")
    for source, english in EN.items():
        values = {name: f"<{name}>" for name in PLACEHOLDER.findall(source)}
        assert tr(source, **values) == PLACEHOLDER.sub(lambda m: f"<{m.group(1)}>", english), source
    assert tr("没有这个词条") == "没有这个词条"
    assert tr("没有这个词条：{name}", name="x") == "没有这个词条：x"
    assert tr("{unknown} stays") == "{unknown} stays"


def test_a_missing_value_leaves_the_placeholder_and_never_raises():
    ui_language.set_language("en")
    assert tr("连接测试验证码：{code}\n请原样回复这 4 位数字。回复后设置页会显示端到端已验证。").startswith("Connection test code: {code}")


# --- Chinese stays as it was, English has no Chinese ---------------------------------------------------------------

def _no_chinese(value, allowed=ALLOWED_CHINESE_IN_ENGLISH):
    text = value if isinstance(value, str) else repr(value)
    for item in allowed:
        text = text.replace(item, "")
    return not HAN.search(text)


def test_photo_failure_notices():
    from runtime.personal_chat.service import photo_failure_text

    balance, other = {"image_error_code": "GPU_INSUFFICIENT_BALANCE"}, {}
    assert photo_failure_text(balance) == "照片这次没生成成功，没能发给你。Olivia 可用余额不足，请充值后重试。"
    assert photo_failure_text(other) == "照片这次没生成成功，没能发给你。稍后再试一下。"
    ui_language.set_language("en")
    assert photo_failure_text(balance).startswith("The photo didn't come out") and "top up" in photo_failure_text(balance)
    assert _no_chinese(photo_failure_text(balance)) and _no_chinese(photo_failure_text(other))


def test_policy_notices_are_translated_when_they_are_the_built_in_ones():
    from runtime.cloud_policy import DEFAULTS

    for code, text in DEFAULTS["notices"].items():
        assert tr(text) == text
    ui_language.set_language("en")
    for code, text in DEFAULTS["notices"].items():
        assert _no_chinese(tr(text)), code
        assert tr(text).startswith("[System notice]")
    # wording the service sent that this client does not know passes through untouched
    assert tr("服务端新写的一句话。") == "服务端新写的一句话。"
    assert tr("Wording the service already wrote in English.") == "Wording the service already wrote in English."


def test_piano_completion_text_keeps_its_chinese_wording_and_has_an_english_one():
    from runtime.piano import completion_text

    meta = dict(title="雨夜", composer=None, key=None, tempo_bpm=72, seconds=156, section="完整一曲")
    assert completion_text(meta, source="original") == "给你弹了一整首我自己写的《雨夜》，完整的，录了 156 秒。"
    part = dict(meta, seconds=45.5, section="开头")
    assert completion_text(part) == "给你弹了《雨夜》的开头，录了 45.5 秒。"
    ui_language.set_language("en")
    assert completion_text(meta, source="original") == "I played you a whole piece I wrote myself, “雨夜”, start to finish. It runs 156 seconds."
    assert completion_text(part) == "I played you the beginning of “雨夜”, 45.5 seconds."
    english = completion_text(dict(part, title="Rain", section="结尾"))
    assert english == "I played you the ending of “Rain”, 45.5 seconds."


def test_daily_video_captions():
    from runtime.personal_chat.daily_video import capture_caption

    local = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 18, 22, 0, tzinfo=local)
    today = {"event_at": datetime(2026, 10, 18, 21, 30, tzinfo=local).isoformat(), "event_kind": "bath_finished"}
    earlier = {"event_at": datetime(2026, 10, 16, 8, 5, tzinfo=local).isoformat(), "event_kind": "walk", "share_text": "出门啦"}
    assert capture_caption(today, now) == "这是今天 21:30那次洗澡后的样子。"
    assert capture_caption(earlier, now) == "出门啦\n这是2026-10-16 08:05拍的。"
    assert capture_caption(dict(today, event_kind="walk"), now) == "这是今天 21:30拍的。"
    ui_language.set_language("en")
    assert capture_caption(today, now) == "This is how I looked after my bath today at 21:30."
    assert capture_caption(dict(earlier, share_text=""), now) == "This was taken on 2026-10-16 at 08:05."
    assert capture_caption(dict(today, event_kind="walk"), now) == "This was taken today at 21:30."


def test_mailbox_notice_follows_the_language():
    from runtime.personal_chat import mailbox_notice

    letter = {"letter_id": "l1", "origin": "proactive", "letter_status": "COMPLETED", "published_at": 1, "created_at": 1}
    reply = mailbox_notice.attach_notice([letter], {"origin": "user"}, "好的。")
    assert reply == "好的。\n\n对了，我之前往信箱里放了封信，你有空的时候再看看就好。"
    ui_language.set_language("en")
    reply = mailbox_notice.attach_notice([letter], {"origin": "user"}, "Okay.")
    assert reply == "Okay.\n\nOh, by the way, I left a letter in your mailbox earlier. Have a look whenever you have time."


def test_default_titles_follow_the_language():
    from runtime.personal_chat.speech import filename

    assert filename("") == "睡前音频.mp3" and filename("CON") == "音频_CON.mp3"
    ui_language.set_language("en")
    assert filename("") == "Bedtime audio.mp3" and filename("CON") == "Audio_CON.mp3"
    assert filename("雨夜故事") == "雨夜故事.mp3"  # a title she wrote is never changed


def test_component_panel_labels(tmp_path):
    from runtime.media.component_packages import COMPONENTS, MediaComponents

    chinese = MediaComponents(tmp_path / "data").status()["items"]
    assert [(item["label"], item["description"]) for item in chinese] == [(label, description) for label, description, _ in COMPONENTS.values()]
    ui_language.set_language("en")
    english = MediaComponents(tmp_path / "data").status()["items"]
    assert [item["id"] for item in english] == [item["id"] for item in chinese]
    for item in english:
        assert item["label"] and item["description"] and _no_chinese(item["label"] + item["description"]), item


def test_dependency_panel_labels_and_sources(monkeypatch):
    from runtime.media import music_reply

    def collect(status):
        text = []
        for dependency in status["dependencies"]:
            text += [dependency["label"], dependency["source_summary"], *[source["label"] for source in dependency["sources"]]]
        return text

    chinese = music_reply.video_reply_dependency_status({}, performance_video_path=None, probe_runtime=False)
    assert any(HAN.search(line) for line in collect(chinese))
    ui_language.set_language("en")
    english = music_reply.video_reply_dependency_status({}, performance_video_path=None, probe_runtime=False)
    assert [d["id"] for d in english["dependencies"]] == [d["id"] for d in chinese["dependencies"]]
    assert all(line and _no_chinese(line) for line in collect(english)), [line for line in collect(english) if HAN.search(line)]
    ui_language.reset()
    again = music_reply.video_reply_dependency_status({}, performance_video_path=None, probe_runtime=False)
    assert [d["label"] for d in again["dependencies"]] == [d["label"] for d in chinese["dependencies"]]
    # the cloud variant
    import runtime.remote_pipeline as pipeline

    ui_language.set_language("en")
    monkeypatch.setattr(pipeline, "enabled", lambda env=None: True)
    monkeypatch.setattr(pipeline, "capabilities", lambda env: {"kinds": ["video"]})
    cloud = music_reply.video_reply_dependency_status({}, performance_video_path=None, probe_runtime=False)
    assert cloud["dependencies"] and all(line and _no_chinese(line) for line in collect(cloud))


def test_letter_maintenance_option_labels():
    from runtime.imports.letter_maintenance import preview

    def letter(identity, content="去信", reply="回信", **extra):
        return dict(letter_id=identity, content=content, reply_text=reply, letter_status="COMPLETED", created_at=None, **extra)

    rows = [letter("a"), letter("b"), letter("c", imported_by="tool"), dict(letter("d", "失败的信"), letter_status="FAILED")]

    def labels(plan):
        return sorted(option["label"] for item in plan["items"] for option in item["options"])

    chinese = preview(rows, {})
    assert {"保留右侧，收起左侧", "从信箱收起这封失败信", "收起旧工具导入信"} <= set(labels(chinese))
    ui_language.set_language("en")
    english = preview(rows, {})
    assert {"Keep the right one, fold away the left one", "Fold away this failed letter from the mailbox",
            "Fold away letters imported by the old tool"} <= set(labels(english))
    assert all(_no_chinese(label) for label in labels(english))
    # the plan itself (token and action ids) does not depend on the language
    assert english["token"] == chinese["token"]
    assert [option["id"] for item in english["items"] for option in item["options"]] == \
        [option["id"] for item in chinese["items"] for option in item["options"]]


def test_sticker_pack_names(tmp_path):
    from runtime.letter_stickers import packs

    assert [item["name"] for item in packs.status(tmp_path)] == ["手绘", "静蓝", "唱片", "戏剧漫画", "奇想漫画", "动图"]
    ui_language.set_language("en")
    names = [item["name"] for item in packs.status(tmp_path)]
    assert names == ["Hand-drawn", "Quiet blue", "Vinyl", "Dramatic manga", "Whimsical manga", "Animated"]


def test_local_wardrobe_catalogue():
    from runtime.wardrobe import STYLES, catalog

    chinese = catalog()
    assert [(item["label"], item["description"], item["pieces"]) for item in chinese] == \
        [(style["label"], style["description"], style["pieces"]) for style in STYLES.values()]
    ui_language.set_language("en")
    for item in catalog():
        assert _no_chinese([item["label"], item["description"], *item["pieces"]]), item["style_id"]
    assert [item["style_id"] for item in catalog()] == [item["style_id"] for item in chinese]


def test_relationship_page_text_is_translated_where_the_client_wrote_it(tmp_path):
    from runtime.private_world.ledger import SQLitePrivateWorldLedger
    from runtime.relationship_moments import record_moment
    from runtime.relationship_lite import details, writer_line

    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    target = SQLitePrivateWorldLedger(tmp_path / "world.sqlite3")
    with target._connection() as db:
        record_moment(db, "source", now.isoformat(), {"moment": "rel_disclosure:vulnerable", "user_quote": "合成隐私内容", "reply_quote": "谢谢你愿意告诉我"})
    server = SimpleNamespace(private_world_relationship_committer=SimpleNamespace(ledger=target), daily_life_runtime=None)

    chinese = details(server, now)
    assert chinese["routes"] == {"undecided": "先不定义", "romantic": "恋人", "friend": "挚友", "family": "家人"}
    writer_chinese = writer_line(target.snapshot(), target)
    ui_language.set_language("en")
    english = details(server, now)
    assert english["routes"] == {"undecided": "Not defined yet", "romantic": "Partner", "friend": "Close friend", "family": "Family"}
    shown = [english["title"], english["description"], english["dynamic"], english["hint"],
             *[row["text"] for row in english["dimensions"].values()], *[row["text"] for row in english["moments"]]]
    assert english["moments"] and all(line and _no_chinese(line) for line in shown), shown
    assert {key: value for key, value in english.items() if key in {"version", "route", "volume"}} == \
        {key: value for key, value in chinese.items() if key in {"version", "route", "volume"}}
    # what the model reads stays Chinese whatever the interface language is
    assert writer_line(target.snapshot(), target) == writer_chinese


@pytest.mark.skipif(os.name != "nt", reason="Windows file dialogs")
def test_file_dialog_titles_follow_the_language(monkeypatch):
    import original_client_capability_api as memory_api
    import original_client_video_capability_api as video_api

    def capture(module):
        scripts = []

        def run(command, **kwargs):
            scripts.append(command[-1])
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(module.subprocess, "run", run)
        return scripts

    calls = [(memory_api, memory_api._select_windows_offline_archive, ()),
             (video_api, video_api._select_windows_runtime_root, ()),
             (video_api, video_api._select_windows_offline_archive, ())]
    for module, picker, args in calls:
        scripts = capture(module)
        picker(*args)
        assert HAN.search(scripts[-1]), picker.__name__
    ui_language.set_language("en")
    for module, picker, args in calls:
        scripts = capture(module)
        picker(*args)
        assert "'" not in re.sub(r"'[^']*'", "", scripts[-1])  # the English text has no quote of its own
        assert _no_chinese(scripts[-1]), picker.__name__
    scripts = capture(memory_api)
    memory_api._select_windows_offline_archive()
    assert "Title = 'Choose an Olivia memory offline package'" in scripts[-1]
    assert "Filter = 'Olivia memory offline package (*.zip)|*.zip'" in scripts[-1]


def test_video_install_progress_labels(tmp_path):
    from video_capability_install import tr as installer_tr

    assert installer_tr is tr
    assert installer_tr("校验安装文件 {done} / {total} MiB", done="1.5", total="3.0") == "校验安装文件 1.5 / 3.0 MiB"
    ui_language.set_language("en")
    assert installer_tr("校验安装文件 {done} / {total} MiB", done="1.5", total="3.0") == "Verifying installed files 1.5 / 3.0 MiB"


def test_the_cloud_notice_for_an_outdated_client_follows_the_language():
    assert tr("请重启客户端以更新原创收费确认。草稿已保留。") == "请重启客户端以更新原创收费确认。草稿已保留。"
    ui_language.set_language("en")
    assert tr("请重启客户端以更新原创收费确认。草稿已保留。") == PAGE_EN["请重启客户端以更新原创收费确认。草稿已保留。"]


# --- launcher messages (the launcher reads the saved choice, not the running backend) ------------------------------

def test_the_launcher_message_follows_the_saved_choice(tmp_path):
    config = tmp_path / "config"
    path = config / ui_language.FILE_NAME
    zh = "LLM_API_KEY_NOT_CONFIGURED: 尚未连接 Olivia 账户 Key，请在设置的“回信服务 → Olivia 账户”中获取或导入。"
    assert tr_in(ui_language.read_saved(path), zh) == zh
    config.mkdir()
    path.write_text('{"language": "en", "schema_version": 1}', encoding="utf-8")
    english = tr_in(ui_language.read_saved(path), zh)
    assert english.startswith("LLM_API_KEY_NOT_CONFIGURED: No Olivia account Key is connected yet.") and _no_chinese(english)


def test_the_restart_failure_prompt_reads_the_saved_choice(tmp_path, monkeypatch):
    import sys

    from installer import version_launcher

    shown = []
    fake = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(MessageBoxW=lambda *args: shown.append(args[1]))))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    version_launcher._restart_failure_prompt(tmp_path)
    (tmp_path / "data" / "config").mkdir(parents=True)
    (tmp_path / "data" / "config" / "ui-language.json").write_text('{"language": "en", "schema_version": 1}', encoding="utf-8")
    version_launcher._restart_failure_prompt(tmp_path)
    version_launcher._restart_failure_prompt()
    assert shown == ["Olivia 自动重启失败，请手动打开", EN["Olivia 自动重启失败，请手动打开"], "Olivia 自动重启失败，请手动打开"]


def test_the_start_script_message_reads_the_saved_choice():
    script = (ROOT / "installer" / "start_hidden.vbs.txt").read_text(encoding="utf-8")
    assert r'"\data\config\ui-language.json"' in script
    assert 'InStr(languageText, """language"": ""en""")' in script
    assert "Olivia failed to start (error code " in script
    # the Chinese message is still the default
    assert "Olivia 启动失败（错误码 " in script and "Olivia 本地版" in script


def test_the_native_launcher_message_reads_the_saved_choice():
    source = (ROOT / "installer" / "olivia_launcher" / "Program.cs").read_text(encoding="utf-8")
    assert 'Path.Combine(root, "data", "config", "ui-language.json")' in source
    assert 'Contains("\\"language\\": \\"en\\"")' in source
    assert "Olivia failed to start (error code " in source
    # the Chinese message is still the default, and the English one is the same text as the start script's
    assert "Olivia 启动失败（错误码 " in source
    script = (ROOT / "installer" / "start_hidden.vbs.txt").read_text(encoding="utf-8")
    english = "Please send the startup logs in the data\\\\logs folder of the installation to the maintainer."
    assert english in source and english.replace("\\\\", "\\") in script


def test_the_web_player_message_reads_the_saved_choice():
    from patch_webplayer import _BOOTSTRAP_JAVASCRIPT

    assert 'localStorage.getItem("olivia.ui.language") === "en"' in _BOOTSTRAP_JAVASCRIPT
    assert "This local video can't be played right now." in _BOOTSTRAP_JAVASCRIPT
    assert "本地视频暂时无法播放。" in _BOOTSTRAP_JAVASCRIPT
    assert EN["本地视频暂时无法播放。"] == "This local video can't be played right now."


def test_the_english_maintenance_guide_uses_the_wording_of_the_page():
    tools = (ROOT / "tools" / "olivia_memory" / "letter-maintenance-guide.en.md").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "letter-maintenance-guide.en.md").read_text(encoding="utf-8")
    assert tools == docs  # the copy that ships with the tool and the one in docs stay the same
    assert not HAN.search(tools.replace("灵离", "").replace("运行.bat", ""))
    for chinese in ("信件对比与整理", "检查当前信箱", "选择备份对比 / 修复时间", "保持原样", "可修复时间与顺序", "完全重复",
                    "近似重复 · 请核对两侧正文", "失败或已取消的信件", "旧工具导入信", "已整理 · 可恢复", "内容一致", "仅信箱中存在",
                    "备份存在多个日期 · 暂不修复", "选择文件导入", "处理方式",
                    "正文有差异 · 仅对比，不自动覆盖或修复时间",
                    "备份无法读取。请选择完整的 .soul 或 JSON 文件（最大 64 MB）。",
                    "信箱已变化，请重新检查后选择。", "正在导入或整理记忆，请稍后重新检查。",
                    "无法保存信箱整理结果，请检查磁盘后重试。",
                    "信件较多，本次仅完成部分近似对比；完全重复检查已覆盖全部。"):
        assert PAGE_EN[chinese] in tools, chinese
    for chinese in ("保留右侧，收起左侧", "按备份修复时间和顺序", "恢复整理前的显示和时间"):
        assert EN[chinese] in tools, chinese
    readme = (ROOT / "tools" / "olivia_memory" / "README.md").read_text(encoding="utf-8")
    assert readme.count("letter-maintenance-guide.en.md") >= 2
