#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵离(.soul)的信件搬进月离，时间和顺序都要对。

两个字段格式相反，写错一个就出问题（前端算 createdAt * 1e3，给字符串显示 NaN-NaN-NaN）：
    metadata_json.backup_record.created_at   数字（epoch 秒），信箱前端读
    legacy_letters.occurred_at               ISO 串，记忆档案读
写库前 VACUUM INTO 备份，只新增去重、不动已有的信，触发器拆了原样重建。
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 有控制台时跟随控制台编码（运行.bat 不做 chcp）；重定向到文件/管道时钉 UTF-8。
try:
    _enc = (sys.stdout.encoding or "utf-8") if sys.stdout.isatty() else "utf-8"
    sys.stdout.reconfigure(encoding=_enc, errors="replace")
    sys.stderr.reconfigure(encoding=_enc, errors="replace")
except Exception:
    pass

# ───────────────────────────── 常量 ─────────────────────────────

TZ = timezone(timedelta(hours=8))        # .soul 的 date/time 是北京时间
MAGIC = b"SOUL0001"
MAX_MANIFEST = 64 * 1024 * 1024          # 清单大小上限

SCHEMA = "olivia.letters.v1"
KIND = "local_letter_backup_v1"

# 月离 _record() 只认这 9 个字段，多出来的键读备份时忽略
RECORD_KEYS = ("content", "reply_text", "title", "origin", "reply_mode",
               "letter_status", "created_at", "replied_at", "source_id")

# 触发器：拆掉之后必须原样重建，否则月离的只读保护就没了
TRIG_UPDATE = ("CREATE TRIGGER legacy_letters_no_update BEFORE UPDATE ON legacy_letters "
               "BEGIN SELECT RAISE(ABORT, 'legacy_letters are read-only'); END")
TRIG_DELETE = ("CREATE TRIGGER legacy_letters_no_delete BEFORE DELETE ON legacy_letters "
               "BEGIN SELECT RAISE(ABORT, 'legacy_letters require whole-library unload'); END")

# 月离安装根目录的特征文件，满足任意一条就算（在 install\ 下找，不写死盘符）
INSTALL_MARKERS = (
    ("install", "data", "memory", "memory.sqlite3"),
    ("install", "START.vbs"),
)

# 记住用户选的月离目录（存在工具自己旁边）
CONFIG_NAME = "olivia_memory.json"


# 固定水印，不是递增的版本号：加功能也不要动它
__version__ = "7.0.2.0"

# ───────────────────────────── 日志 ─────────────────────────────
# 控制台看到什么日志里就写什么；日志里多写运行环境、路径和完整 traceback

_LOG = None
LOG_PATH = None
_LOG_MAX = 2 * 1024 * 1024          # 超过 2 MB 轮转一次


def open_log():
    """打开日志文件：优先放工具自己旁边，写不了就退到 %TEMP%，再不行就放弃。"""
    global _LOG, LOG_PATH
    cands = []
    try:
        cands.append(Path(__file__).resolve().parent / "olivia_memory.log")
    except Exception:
        pass
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if tmp:
        cands.append(Path(tmp) / "olivia_memory.log")
    for cand in cands:
        try:
            if cand.exists() and cand.stat().st_size > _LOG_MAX:
                try:
                    cand.replace(cand.with_name(cand.name + ".1"))
                except OSError:
                    pass
            _LOG = cand.open("a", encoding="utf-8")
            LOG_PATH = cand
            return
        except OSError:
            continue
    _LOG = None      # 日志写不了也不能让程序跑不起来


def log(msg=""):
    """控制台 + 日志，两边都写。"""
    print(msg, flush=True)
    log_line(msg)


def log_line(msg=""):
    """只写日志，不打印。"""
    if _LOG is not None:
        try:
            _LOG.write(msg + "\n")
            _LOG.flush()
        except Exception:
            pass


def set_console_title():
    """把控制台窗口标题改成工具名（否则任务栏上只有 python.exe 的路径）。"""
    try:
        if sys.platform == "win32" and sys.stdout.isatty():
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("%s v%s" % (BANNER, __version__))
    except Exception:
        pass          # 设不上不影响功能


def log_header(argv):
    log_line()
    log_line("=" * 72)
    log_line("运行时间 : %s" % datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z"))
    log_line("工具版本 : %s" % __version__)
    log_line("命令行   : %s" % " ".join([str(sys.argv[0])] + list(argv)))
    log_line("工作目录 : %s" % os.getcwd())
    log_line("Python   : %s  (%s)" % (sys.version.split()[0], sys.executable))
    log_line("日志文件 : %s" % LOG_PATH)
    log_line("-" * 72)


def log_footer(state: str):
    log_line("-" * 72)
    log_line("结束状态 : %s" % state)


def log_traceback():
    import traceback
    log_line()
    log_line("---- 完整堆栈（贴给别人查问题用这一段）----")
    log_line(traceback.format_exc())
    log_line("---- 堆栈结束 ----")


# ───────────────────────────── 时间 ─────────────────────────────

def epoch_of(date: str, timestr: str):
    """`YYYY-MM-DD` + `HH:MM`（北京时间）-> epoch 秒；解析不出来返回 None。"""
    d = (date or "").strip()
    if not d:
        return None
    t = (timestr or "").strip()
    if len(t) != 5 or t[2] != ":":
        t = "00:00"
    try:
        dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            return None
    return int(dt.replace(tzinfo=TZ).timestamp())


def to_epoch(value):
    """把各种写法的时间统一成 epoch 秒，认不出来返回 None。
    数字、纯数字串、ISO 串都收；前端做 createdAt * 1e3，字符串会变 NaN。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = str(value).strip()
        if not s:
            return None
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            v = float(s)
        else:
            try:
                v = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    if v > 1e11:        # 秒级 epoch ~1.8e9，比这大得多的一律当毫秒
        v /= 1000.0
    return int(v)


def iso_of(epoch):
    """epoch 秒 -> ISO 字符串（occurred_at 要 ISO 不要数字）。"""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, TZ).isoformat()


# ─────────────────────────── 读来源文件 ───────────────────────────

def _what_is_this(head: bytes) -> str:
    """给文件头加一句"它像什么"，帮用户认出自己拖错的那个文件（只报十六进制看不出问题）。"""
    t = head.decode("utf-8", errors="replace")
    keep = "".join(c if (c.isprintable() and c not in "\r\n\t") else "·" for c in t)
    if not keep.strip("·"):
        return ""
    return ("；这文件的开头是「%s…」，看着是【文字文件】不是 .soul"
            "（是不是拖错了？）" % keep[:34])


def read_soul(path: Path) -> dict:
    """.soul -> {"letters": [...], "videos": n, "bytes": n, "name": str}。
    结构：'SOUL0001'(8字节) + 清单长度(8字节小端) + JSON 清单 + 媒体二进制；只读清单
    那一段，后面可能是几百 MB 视频，没必要读进来。
    """
    with path.open("rb") as fh:
        # 只能读 16 字节：清单紧接着第 16 字节开始，多读就把流位置推后、清单少读。
        head = fh.read(16)
        if len(head) < 16 or head[:8] != MAGIC:
            # 只有出错路径里才多读几十字节，留给 _what_is_this() 认"开头像什么"。
            peek = head + fh.read(48)
            msg = ("魔数不是 SOUL0001（实际字节 %s），不是 .soul 文件%s"
                   % (head[:8].hex(" "), _what_is_this(peek)))
            if path.suffix.lower() == ".txt":
                # 收 .txt 是为了兼容"把 .soul 改名成 .txt 再发"（.soul 常被聊天软件拦）
                msg += ("\n         （收 .txt 是为了兼容「把 .soul 改名成 .txt 再发」的"
                        "情况，内容还得是 .soul，普通文本信件不支持。）")
            raise ValueError(msg)
        length = int.from_bytes(head[8:16], "little")
        if length <= 0 or length > MAX_MANIFEST:
            raise ValueError("清单长度异常：%d 字节" % length)
        manifest = fh.read(length)
    if len(manifest) < length:
        raise ValueError("清单读取不完整（%d / %d）" % (len(manifest), length))

    bundle = json.loads(manifest.decode("utf-8"))
    memory = bundle.get("memory") or {}
    exchanges = memory.get("exchanges") or []
    videos = bundle.get("videos") or []

    rows = []
    for e in exchanges:
        inc = str(e.get("incoming") or "")
        rep = str(e.get("reply") or "")
        if not (inc.strip() or rep.strip()):
            continue
        rows.append({
            # 月离要的 9 个
            "content": inc,
            "reply_text": rep,
            "title": "",
            "origin": "user",
            "reply_mode": "text",
            "letter_status": "COMPLETED",
            "created_at": epoch_of(e.get("date"), e.get("time")),   # 数字
            "replied_at": None,
            "source_id": "",
            # 存档附带，月离读备份时忽略
            "letter_id": str(e.get("letterId") or ""),
            "summary": str(e.get("summary") or ""),
            "reply_label": str(e.get("replyLabel") or ""),
            "reply_video_url": str(e.get("replyVideoUrl") or ""),
            "content_md5": str(e.get("contentMd5") or ""),
        })
    return {"letters": rows, "videos": len(videos),
            "bytes": path.stat().st_size, "name": path.name}


def read_json(path: Path) -> dict:
    """olivia.letters.v1 json -> 同样的结构。"""
    raw = path.read_text(encoding="utf-8-sig")
    d = json.loads(raw)
    rows = d.get("letters") if isinstance(d, dict) else d
    if not isinstance(rows, list):
        raise ValueError("json 里没有 letters 数组")

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        inc = str(r.get("content") or "")
        rep = str(r.get("reply_text") or r.get("reply") or "")
        if not (inc.strip() or rep.strip()):
            continue
        out.append({
            "content": inc,
            "reply_text": rep,
            "title": str(r.get("title") or ""),
            "origin": str(r.get("origin") or "user"),
            "reply_mode": str(r.get("reply_mode") or "text"),
            "letter_status": str(r.get("letter_status") or "COMPLETED"),
            # 数字/数字串/ISO 串都收，统一成数字
            "created_at": to_epoch(r.get("created_at")),
            "replied_at": to_epoch(r.get("replied_at")),
            "source_id": "",
            "letter_id": str(r.get("letter_id") or ""),
            "summary": str(r.get("summary") or ""),
            "reply_label": str(r.get("reply_label") or ""),
            "reply_video_url": str(r.get("reply_video_url") or ""),
            "content_md5": str(r.get("content_md5") or ""),
        })
    videos = d.get("_video_count") if isinstance(d, dict) else None
    return {"letters": out, "videos": videos,
            "bytes": path.stat().st_size, "name": path.name}


def parse_source(path: Path) -> dict:
    ext = path.suffix.lower()
    if ext == ".soul" or ext == ".txt":
        return read_soul(path)
    if ext == ".json":
        return read_json(path)
    raise ValueError("不认识的扩展名：%s" % (ext or "(无)"))


def collect_files(src: Path, out_dir):
    """找候选文件 -> (files, 输出目录)"""
    if src.is_dir():
        files = sorted([p for p in src.rglob("*")
                        if p.is_file() and p.suffix.lower() in (".soul", ".txt")])
        base_out = Path(out_dir) if out_dir else src
    else:
        files = [src]
        base_out = Path(out_dir) if out_dir else src.parent
    return files, base_out


def load_source(path: Path):
    """文件或文件夹 -> (letters 列表, 读成功的文件名列表)。"""
    if path.is_dir():
        cands = sorted([p for p in path.rglob("*")
                        if p.is_file() and p.suffix.lower() in (".soul", ".txt", ".json")])
        rows, names = [], []
        for f in cands:
            try:
                parsed = parse_source(f)
            except Exception as ex:
                log("    跳过 %s（%s）" % (f.name, ex))
                continue
            rows += parsed["letters"]
            names.append(f.name)
            log("    读入 %s" % f.name)
        return rows, names
    parsed = parse_source(path)
    return parsed["letters"], [path.name]


def record_of(row: dict) -> dict:
    """只取月离 _record() 认的那 9 个字段。"""
    return {k: row.get(k) for k in RECORD_KEYS}


def norm_text(s) -> str:
    """判重用的规范化文本：把所有空白（含换行、全角空格）压成一个空格。
    不归一化的话，"有换行版"和"没换行版"会被当成两封，整批重复导入。
    """
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _bigrams(s: str) -> set:
    """字符二元组集合，用来算两段文字有多像。不用 difflib：上千字的信两两比是
    O(n²)，几百封就卡住；二元组是线性的，且对"改了几个字"敏感。
    """
    t = re.sub(r"\s+", "", s or "")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similar(a: str, b: str) -> float:
    """二元组 Dice 相似度，0~1。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return 2.0 * len(A & B) / (len(A) + len(B))


NEAR_CHOICES = ("source", "library", "both")
NEAR_LABEL = {
    "source": "按源文件导入（用 .soul 那版的正文，并打上 .soul 的时间）",
    "library": "按库导入（保留库里的正文，但按 .soul 打时间并正确排序）",
    "both": "两个都保留（两封都打 .soul 的时间；库里那版在上）",
}


def _ask_near_choice(conflicts, preset, dry_run):
    """问用户"源文件和库各有一版"怎么处理，返回 source / library / both。
    不给默认值、不猜：交互式就问，非交互式且没给 --near 直接报错退出。
    """
    if preset in NEAR_CHOICES:
        return preset
    n = len(conflicts)
    log()
    log("  有 %d 封是「源文件和库里各有一版」（正文不一样）：" % n)
    for it in conflicts[:8]:
        r = it["row"]
        t = (datetime.fromtimestamp(r["created_at"], TZ).strftime("%Y-%m-%d %H:%M")
             if r["created_at"] else "时间未知")
        log("      %s  %-24s  %s"
            % (t, _peek(r["content"], 22), it["hit"].get("conflict_kind", "")))
    if n > 8:
        log("      …另有 %d 封" % (n - 8))
    log()
    for k, i in (("source", 1), ("library", 2), ("both", 3)):
        log("     %d) %s" % (i, NEAR_LABEL[k]))
    log()
    if dry_run:
        log("     （体检模式不写库。真要跑时用 --near source|library|both 指定；")
        log("       下面按【1 按源文件】的口径估算。）")
        return "source"
    try:
        if not sys.stdin.isatty():
            raise SystemExit(
                "  ✗ 有 %d 封需要你决定留哪一版，但当前不是交互式。\n"
                "    请加 --near source|library|both 明确指定，工具不替你猜。" % n)
        c = input("  选 [1/2/3]（直接回车 = 取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("  没有选择，退出（什么都没改）。")
    if c not in ("1", "2", "3"):
        raise SystemExit("  没有选择，退出（什么都没改）。")
    return {"1": "source", "2": "library", "3": "both"}[c]


# 近似匹配门槛，别往下调：短文本的二元组相似度虚高，一松就会把真新信配到别的信头上
FUZZY_MIN = 0.85
FUZZY_MIN_LEN = 50


def _best_near(row: dict, rows, claimed, minimum: float = FUZZY_MIN,
               min_len: int = FUZZY_MIN_LEN):
    """在库里给这封源文件信找【最接近的一行】，返回 (hit, score, sc, sr) 或 None。
    判据是去信、回信有一边足够像（取 max）；只用来【配对】，不替用户决定留哪版。
    """
    best = None
    for L in rows:
        if L.get("mid") in claimed:
            continue
        ca, cb = norm_text(row.get("content")), norm_text(L.get("raw_content"))
        ra, rb = norm_text(row.get("reply_text")), norm_text(L.get("raw_reply"))
        sc = _similar(ca, cb) if min(len(ca), len(cb)) >= min_len else 0.0
        sr = _similar(ra, rb) if min(len(ra), len(rb)) >= min_len else 0.0
        score = max(sc, sr)
        if score >= minimum and (best is None or score > best[1]):
            best = (L, score, sc, sr)
    return best


def _similar_pair(a, a_reply, b, b_reply):
    """两封信的 (去信相似度, 回信相似度)，门槛和近似匹配那套一模一样。

    口径只有这一处：_nearest_source（孤儿行 vs 源文件）和 near_dupe_plan
    （库里两份互相比）都走它。门槛分叉过一次，后果是把真新信配到了别的信头上。
    """
    ac, bc = norm_text(a), norm_text(b)
    ar, br = norm_text(a_reply), norm_text(b_reply)
    sc = _similar(ac, bc) if min(len(ac), len(bc)) >= FUZZY_MIN_LEN else 0.0
    sr = _similar(ar, br) if min(len(ar), len(br)) >= FUZZY_MIN_LEN else 0.0
    return sc, sr


def near_dupe_plan(lib):
    """找 legacy 里"两份正文很像、但不是逐字相同"的对子（同一封信的两个版本）。

    比法走 _similar_pair（门槛和近似匹配一致）。先按去信长度粗筛一下，免得几百行做
    全量两两比；每一行只认它最像的那个搭档，一对只算一次。

    **只返回"能定的"**：两边【恰好一份有时间】—— 用户 2026-09-26 定的规矩是
    **留有时间的那份、藏掉没时间的**。两边都有时间的不用管（两行显示都正常），
    都没有的也没法偏袒谁，都不动。

    返回 [(要藏的行, 要留的行, 去信相似度, 回信相似度), ...]。
    """
    rows = [L for L in lib
            if L.get("store") == "legacy"
            and (norm_text(L["content"]) or norm_text(L["reply"]))]
    out, used = [], set()
    for i, a in enumerate(rows):
        if i in used:
            continue
        la = len(norm_text(a["content"]))
        best = None
        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            b = rows[j]
            lb = len(norm_text(b["content"]))
            if la and lb and abs(la - lb) > max(la, lb) * 0.5:
                continue                      # 长度差一半以上，不用比
            sc, sr = _similar_pair(a["content"], a["reply"], b["content"], b["reply"])
            score = max(sc, sr)
            if score >= FUZZY_MIN and (best is None or score > best[0]):
                best = (score, b, sc, sr, j)
        if best is None:
            continue
        _score, b, sc, sr, j = best
        ta, tb = _has_time(a), _has_time(b)
        if ta == tb:
            continue                          # 都有、或都没有 → 不猜，也不动
        hide, keep = (b, a) if ta else (a, b)
        used.add(i)
        used.add(j)
        out.append((hide, keep, sc, sr))
    return out


def _nearest_source(L, rows):
    """孤儿行反过来去源文件里找最像的一封，返回 (源文件下标, 行, 去信相似度, 回信相似度) 或 None。

    口径和 _best_near 完全一样（同一个 _similar、同样两个门槛），只是方向相反：
    那边是"源文件这封在库里找最像的"，这边是"库里这行在源文件里找最像的"。

    为什么要它：源文件每一封都精确命中了别的行时，孤儿行永远走不到 _best_near，
    报告里就只剩一句"库里多出来一封"，看不出和谁像。实测有人因此以为判重坏了
    （那封信其实和源文件某封"几乎一样"，差在一两个字上）。
    """
    best = None
    for i, r in enumerate(rows):
        sc, sr = _similar_pair(L["content"], L["reply"], r["content"], r["reply_text"])
        score = max(sc, sr)
        if score >= FUZZY_MIN and (best is None or score > best[4]):
            best = (i, r, sc, sr, score)
    return best


def _pair_id(index: int, content: str, reply: str, above: bool) -> str:
    """"两个都保留"时，给一对信生成相邻且可分先后的 memory_id。
    数字部分不动，只把盐前缀换成 'z'（上面那封）/'y'（下面那封）；不重排整个下标，
    那会让所有信的 id 都变。
    """
    salt = hashlib.sha256((content + "\x00" + reply).encode("utf-8")).hexdigest()[:8]
    return "lb-%010d-%s%s" % (10 ** 9 - index, "z" if above else "y", salt)


def _verdict(sc: float, sr: float) -> str:
    """这两版像到什么程度、像是哪边被改过。
    措辞必须跟着分数走：回信只比去信高 15 个点就写"几乎一样"，58% 也会被说成"几乎一样"。
    """
    top = max(sc, sr)
    if top < 0.5:
        return "两面都不太像，多半只是恰好相似，未必是同一封"
    if sr >= 0.8 and sr > sc + 0.15:
        return "回信几乎一样、去信差很多 → 像是【去信】被改过"
    if sc >= 0.8 and sc > sr + 0.15:
        return "去信几乎一样、回信差很多 → 像是【回信】被改过"
    if sc >= 0.8 and sr >= 0.8:
        return "两面都很像 → 同一封，只差个别字"
    return "只有一边比较像，另一边出入不小，要人看"


def _col_pair(raw: str):
    """从 legacy_letters.content 那一列里，尽量挖出 (去信, 回信)。
    那一列存什么随导入路径而变：① offline_letter_pairs 是 '{"content":…,"reply":…}'
    （键是 reply）② letter_backup 是整条 record 的 JSON（键是 reply_text）
    ③ 客户端信件导入那条是 '用户来信：…\\n林离回信：…'，其它是原样文本。挖不出回信就
    返回 (原文, None)，调用方据此知道"回信未知"，别当成空回信。
    """
    s = raw or ""
    if s[:1] in "{[":
        try:
            d = json.loads(s)
        except Exception:
            d = None
        if isinstance(d, dict):
            c = d.get("content")
            r = d.get("reply") if "reply" in d else d.get("reply_text")
            if isinstance(c, str):
                return c, (r if isinstance(r, str) else None)
    if s.startswith("用户来信："):
        body = s[len("用户来信："):]
        if "\n林离回信：" in body:
            c, r = body.split("\n林离回信：", 1)
            return c, r
    return s, None


def legacy_view(meta: dict, content_col) -> tuple:
    """按月离渲染信箱的规则取值：metadata 里不是字符串就回落到 content 列。"""
    uc = meta.get("user_content")
    rt = meta.get("reply_text")
    if not isinstance(uc, str):
        col_c, col_r = _col_pair(content_col)
        uc = col_c
        if not isinstance(rt, str) and col_r:
            rt = col_r
    return (uc if isinstance(uc, str) else ""), (rt if isinstance(rt, str) else "")


# ──────────────── 读库 + 判重索引（compare / apply 共用）────────────────
# 两边必须看同一份库、用同一套判重。

def read_library(con, install: Path):
    """读 legacy_letters 表 + state.json -> (所有库行, legacy 行数, 靠列救回的行数)。
    每行统一成一套键；读库只有这一处，compare 和 apply 共用。
    """
    rows = []
    rescued = 0
    # imported_at = 这行什么时候进库的，菜单 8 拿它判新旧。**不能假设这个列一定在**：
    #   老库/手搓的库可能没有，取不到就退回只读那 5 列、当 None，菜单 8 改按入库顺序判。
    try:
        legacy_rows = list(con.execute(
            "SELECT memory_id, metadata_json, occurred_at, source, content, imported_at "
            "FROM legacy_letters"))
    except sqlite3.OperationalError:
        legacy_rows = [(mid, mj, occ, src, col, None) for mid, mj, occ, src, col
                       in con.execute("SELECT memory_id, metadata_json, occurred_at, "
                                      "source, content FROM legacy_letters")]
    for mid, mj, occ, srcname, ccol, imp_at in legacy_rows:
        try:
            meta = json.loads(mj or "{}")
        except Exception:
            continue
        uc, rt = legacy_view(meta, ccol)
        # 正文是从 content 列捞出来的（metadata 里没有），不计数会被误判成新信。
        if not str(meta.get("user_content") or "").strip() and uc.strip():
            rescued += 1
        br = meta.get("backup_record")
        bca = (br or {}).get("created_at") if isinstance(br, dict) else None
        rows.append({
            "store": "legacy", "mid": mid, "content": uc, "reply": rt,
            "raw_content": uc, "raw_reply": rt,       # _best_near / 正文比对用
            "ca": bca,                                # 展示用的时间
            "imported_at": imp_at,                    # 入库时间（判新旧用）
            "import_kind": meta.get("import_kind"),
            "has_backup": isinstance(br, dict),
            "backup_created_at": bca,
            "occurred_at": occ, "source": srcname, "meta": meta, "raw_col": ccol,
        })
    n_legacy = len(rows)

    for i, x in enumerate(load_state_letters(install)):
        rows.append({
            "store": "state",
            "mid": x.get("letter_id") or ("state:%d" % (i + 1)),
            "content": x.get("content") or "", "reply": x.get("reply_text") or "",
            "raw_content": x.get("content") or "", "raw_reply": x.get("reply_text") or "",
            "ca": x.get("created_at"),
            "imported_at": None,
            "import_kind": None, "has_backup": False, "backup_created_at": None,
            "occurred_at": None, "source": "state.json", "meta": x, "raw_col": None,
        })
    return rows, n_legacy, rescued


def build_match_index(lib):
    """建判重用的索引表，返回 (按精确键 / 按去信 / 按回信两张)；legacy 行排在 state
    行前面，所以同名取 [0] 拿到的总是 legacy 那行。
    """
    by_key, by_content = {}, {}
    by_reply_filled, by_reply_empty = {}, {}
    for L in lib:
        ck, rk = norm_text(L["content"]), norm_text(L["reply"])
        if ck or rk:
            by_key.setdefault((ck, rk), []).append(L)
        if ck:
            by_content.setdefault(ck, []).append(L)
        if rk:
            (by_reply_empty if not ck else by_reply_filled).setdefault(rk, []).append(L)
    return by_key, by_content, by_reply_filled, by_reply_empty


def unique_source_rows(rows):
    """来源里同一批逐字重复的只算一次，返回 [(原始下标, 行)]（apply 和 compare 共用）。"""
    seen, out = set(), []
    for i, r in enumerate(rows):
        k = (norm_text(r["content"]), norm_text(r["reply_text"]))
        if k in seen:
            continue
        seen.add(k)
        out.append((i, r))
    return out


def split_orphans(rows):
    """把"库里没被来源认领的那些行"分成两份：(本工具自己藏起来的, 其它)。藏起来的
    行**必须继续算"库里已经有这封"**，不然菜单 2 会又插一份新的（那才是真造重复）；
    但**报"多出来的"时不能算它们**，那是用户自己藏掉的重复。
    """
    ours, others = [], []
    for L in rows:
        if not (norm_text(L["content"]) or norm_text(L["reply"])):
            continue      # 两边都空的行判重认不出，体检那栏专门报
        if (L.get("meta") or {}).get(HIDDEN_KIND_KEY):
            ours.append(L)
        else:
            others.append(L)
    return ours, others


# 一封来源信和库里的关系，只可能是这四种
MATCH_EXACT = "exact"         # 去信+回信逐字一样，就是同一封
MATCH_FAKED = "faked"         # 回信一样，库里那句去信是"为了能导入"硬加的
MATCH_CONFLICT = "conflict"   # 两边各有一版（去信同回信不同，或正文被改过）
MATCH_NEW = "new"             # 库里没有

CONFLICT_SAME_CONTENT = "去信一样、回信不同"
CONFLICT_NEAR = "近似匹配（正文被改过）"


def classify_source(uniq, lib, index):
    """把每封来源信在库里找到对应 -> (每封的归类, 库里没被任何来源认领的行)。
    compare 和 apply 共用这一份，两边口径不会分叉。
    """
    by_key, by_content, by_r_filled, by_r_empty = index
    claimed = set()
    items = []
    for i, r in uniq:
        ck, rk = norm_text(r["content"]), norm_text(r["reply_text"])
        item = {"index": i, "row": r, "kind": MATCH_NEW, "hit": None,
                "candidates": [], "conflict_kind": None, "near": None}

        pool = by_key.get((ck, rk))
        if pool:
            hit = pool.pop(0)          # 一对一消耗：源文件 2 封 ↔ 库里 2 封
            hit["used_by"] = i
            claimed.add(hit.get("mid"))
            item["kind"], item["hit"] = MATCH_EXACT, hit
            items.append(item)
            continue

        pool = (by_r_filled if not ck else by_r_empty).get(rk) if rk else None
        if pool:
            hit = dict(pool.pop(0))    # 拷贝：别把标记写进共享的索引对象
            hit["content_is_faked"] = True
            hit["used_by"] = i
            claimed.add(hit.get("mid"))
            item["kind"], item["hit"] = MATCH_FAKED, hit
            items.append(item)
            continue

        cands = ([x for x in by_content.get(ck, []) if x.get("mid") not in claimed]
                 if ck else [])
        if cands:
            cands[0]["ambiguous_for"] = i
            claimed.add(cands[0].get("mid"))
            item["kind"] = MATCH_CONFLICT
            item["candidates"] = cands
            item["hit"] = cands[0]
            item["conflict_kind"] = CONFLICT_SAME_CONTENT
            items.append(item)
            continue

        near = _best_near(r, lib, claimed)
        if near is not None:
            hit = near[0]
            hit["fuzzy_for"] = i
            claimed.add(hit.get("mid"))
            item["kind"] = MATCH_CONFLICT
            item["hit"] = hit
            item["conflict_kind"] = CONFLICT_NEAR
            item["near"] = near
            items.append(item)
            continue

        items.append(item)

    unclaimed = [L for L in lib
                 if L.get("store") == "legacy" and L.get("mid") not in claimed]
    return items, unclaimed


# ──────────────────── 转换：.soul -> json ────────────────────

def cmd_convert(src: Path, out_dir, merge: bool) -> int:
    files, base_out = collect_files(src, out_dir)
    if not files:
        log("  这个文件夹里没找到 .soul / .txt 文件")
        return 1

    log()
    log("  源: %s" % src)
    log("  输出目录: %s" % base_out)
    log("  找到 %d 个候选文件" % len(files))
    log()
    base_out.mkdir(parents=True, exist_ok=True)

    all_letters = []
    ok = 0
    skipped = []
    for f in files:
        try:
            parsed = parse_source(f)
        except Exception as ex:
            log("  [跳过] %-46s %s" % (f.name[:46], ex))
            skipped.append(f.name)
            continue

        letters = parsed["letters"]
        no_time = sum(1 for r in letters if r["created_at"] is None)
        tail = "  (其中 %d 封无时间)" % no_time if no_time else ""
        log("  [OK] %-46s %3d 封%s" % (f.name[:46], len(letters), tail))

        if merge:
            all_letters += letters
        else:
            payload = {
                "schema_version": SCHEMA,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "media_included": False,
                "letters": letters,
                # 源文件信息（月离不读，纯存档）
                "_source_file": f.name,
                "_source_bytes": parsed["bytes"],
                "_video_count": parsed["videos"],
                "_letter_count": len(letters),
                "_no_time_count": no_time,
            }
            out = base_out / (f.stem + ".json")
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            log("       -> %s" % out.name)
        ok += 1

    if merge:
        if not all_letters:
            log()
            log("  ✗ 一封都没解析出来，没有生成任何文件。")
            return 1
        payload = {
            "schema_version": SCHEMA,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "media_included": False,
            "letters": all_letters,
            "_source_files": ok,
            "_letter_count": len(all_letters),
        }
        out = base_out / "Olivia信件备份-合并.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        log()
        log("  已合并 %d 个文件、%d 封信 -> %s" % (ok, len(all_letters), out.name))

    log()
    if ok == 0:
        log("  ✗ 完成：成功 0 个，跳过 %d 个，什么都没转出来。" % len(skipped))
        if skipped:
            log("    跳过的是：" + ", ".join(skipped))
        return 1
    log("  完成：成功 %d 个，跳过 %d 个" % (ok, len(skipped)))
    if skipped:
        log("  跳过的是：" + ", ".join(skipped))
    log()
    log("  提示：生成的 json 里 created_at 是 epoch 秒（数字）。")
    log("        用【本工具的 apply】写进月离才能显示正确时间；")
    log("        走月离自己的「选择文件导入」它会把时间转成字符串，信箱会显示 NaN-NaN-NaN。")
    log()
    return 0


# ─────────────── 导出 letter_pairs.json（给月离自己的导入用）───────────────

def pairs_target() -> Path | None:
    """月离会从哪儿找 letter_pairs.json？找不到就返回 None（只认 local_server.py:989
    那一条路：.olivia-full-patch.json 里记的源目录下的 letter_pairs.json）。
    """
    install = load_saved_install()          # 只读记住的那个，不弹选择框
    if install is None:
        return None
    try:
        marker = json.loads(
            (install / "install" / ".olivia-full-patch.json").read_text(encoding="utf-8"))
        root = Path(str(marker.get("official_source") or ""))
        if not root.is_absolute():
            return None
        return root / "letter_pairs.json"
    except Exception:
        return None


def cmd_pairs(src: Path, out_dir) -> int:
    """导出 letter_pairs.json，给月离自己的"选择文件导入"用（和 convert 那个存档
    json 不是一回事）。想让林离记得这些信（记忆档案 + 关系判定）只能走这条导入。
    """
    files, base_out = collect_files(src, out_dir)
    if not files:
        log("  这个文件夹里没找到 .soul / .txt 文件")
        return 1

    log()
    log("  导出 letter_pairs.json（给月离自己的导入用）")
    log("=" * 62)
    log("  源: %s" % src)
    log()

    rows, ok_names = [], []
    for f in files:
        try:
            parsed = parse_source(f)
        except Exception as ex:
            log("  [跳过] %-43s %s" % (f.name[:43], ex))
            continue
        rows += parsed["letters"]
        ok_names.append(f.name)
    if not rows:
        log("  一封都没解析出来。")
        return 1

    log("  读到 %d 封（来自 %s）" % (len(rows), "、".join(ok_names[:3])))

    # ── 月离的导入要求【两边都非空】，有一边空整份文件都会被拒 ──
    usable, blocked = [], []
    for r in rows:
        if r["content"].strip() and r["reply_text"].strip():
            usable.append(r)
        else:
            blocked.append(r)
    log("     其中能进月离档案的  %d 封" % len(usable))
    if blocked:
        log("     进不去的           %d 封，去信或回信是空的：" % len(blocked))
        for r in blocked[:8]:
            log("         去信 %d 字 · 回信 %d 字   开头：「%s」"
                % (len(r["content"].strip()), len(r["reply_text"].strip()),
                   _peek(r["content"] or r["reply_text"], 24) or "（空）"))
        if len(blocked) > 8:
            log("         …另有 %d 封" % (len(blocked) - 8))
        log("       这几封【永远进不了月离的记忆】，只能留在信箱里（本工具写库那批）。")

    if not usable:
        log()
        log("  ✗ 一封都进不去（全都有空的边），没有生成文件。")
        return 1

    # ── 顺序：月离按【数组下标】算时间（第 1 项 = 最早），所以要按时间【升序】 ──
    timed = sorted([r for r in usable if r["created_at"] is not None],
                   key=lambda r: r["created_at"])
    untimed = [r for r in usable if r["created_at"] is None]
    ordered = timed + untimed          # 没时间的排最后，保持源文件里的相对顺序

    pairs = [{"content": r["content"], "reply": r["reply_text"]} for r in ordered]

    base_out.mkdir(parents=True, exist_ok=True)
    out = base_out / "letter_pairs.json"
    out.write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")

    log()
    log("  已写出：%s" % out)
    log("     %d 对，%d 字节，按【时间从早到晚】排好了" % (len(pairs), out.stat().st_size))
    if untimed:
        log("     （其中 %d 封没有时间，排在最后面）" % len(untimed))
    log("     顺序很重要：月离是按【数组下标】算历史时间的（第 1 项 = 最早），")
    log("       所以不能直接用 .soul 的顺序，它是新的在前。这里已经倒过来了。")

    target = pairs_target()
    log()
    log("=" * 62)
    log("  接下来怎么用（三步）")
    log("=" * 62)
    if target:
        log("  1) 把这个文件放到这里（月离只认这个位置）：")
        log("       %s" % target)
        log("     （目录没有就自己建一个）")
    else:
        log("  1) 把这个文件放到【月离记录的那个「官方客户端目录」】下，文件名必须叫")
        log("     letter_pairs.json。位置记在 <月离>\\install\\.olivia-full-patch.json")
        log("     的 official_source 里，用记事本打开就能看到。")
    log()
    log("  2) 打开月离 → 设置里的「选择文件导入」→ 走它【自己的导入】。")
    log("     它会依次：写记忆档案（林离的回忆）→ 算关系（熟悉度/信任/亲密…）")
    log("     → 提交「私有世界」。这一步会调用一次模型，所以模型服务要通。")
    log()
    log("  3) 导完之后，再用本工具的菜单 2 修时间/顺序，不会破坏上面建好的东西")
    log("     （档案的编号挂在源文件的指纹上，本工具只改时间和 metadata）。")
    log()
    log("  为什么非要绕这一道：林离的【回忆】和【关系判定】只在月离自己的导入里")
    log("     建立。本工具是直接写库的，那两样一样都不会有，信在信箱里看得见，")
    log("     但林离「想不起来」，也没参与过关系判定。")
    log()
    log("  一次要给全。关系判定的幂等键是按【整份文件的指纹】算的：重导同一份")
    log("     不变；换一份内容不同的文件，它会重算一次关系。")
    log()
    return 0


# ──────────────────────── 找月离 / 进程 ────────────────────────

def looks_like_install(p: Path) -> bool:
    """这个目录像不像月离安装根？（看 install 下有没有特征文件）"""
    try:
        for parts in INSTALL_MARKERS:
            if p.joinpath(*parts).is_file():
                return True
    except OSError:
        pass
    return False


def config_path() -> Path:
    try:
        return Path(__file__).resolve().parent / CONFIG_NAME
    except Exception:
        return Path(CONFIG_NAME)


def load_saved_install():
    """读上次记住的月离目录；没记住过或者已经失效就返回 None。"""
    try:
        d = json.loads(config_path().read_text(encoding="utf-8"))
        p = Path(str(d.get("install") or ""))
        if str(p) and looks_like_install(p):
            return p
    except Exception:
        pass
    return None


def save_install(p: Path) -> None:
    """记住这次选的月离目录，下次不用再问。"""
    try:
        config_path().write_text(
            json.dumps({"install": str(p)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log_line("已记住月离目录：%s" % p)
    except OSError:
        pass          # 记不住就算了，下次再问一遍，不影响功能


def pick_folder(title: str):
    """弹一个文件夹选择框让用户挑。月离自带的 python 没有 tkinter，所以借 PowerShell
    的 .NET；套 1x1 的 TopMost 窗体当父窗口，否则对话框会弹在控制台后面看不见。
    """
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$f = New-Object System.Windows.Forms.Form; "
        "$f.TopMost = $true; $f.ShowInTaskbar = $false; "
        "$f.Width = 1; $f.Height = 1; $f.StartPosition = 'CenterScreen'; "
        "$f.Show(); $f.Activate(); "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = '%s'; $d.ShowNewFolderButton = $false; "
        "if ($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK) { "
        "  Write-Output $d.SelectedPath }; "
        "$f.Close()"
    ) % title.replace("'", "''")
    out = _powershell(script, timeout=180)
    lines = [x.strip().strip('"') for x in out.splitlines() if x.strip()]
    if not lines:
        return None
    cand = Path(lines[-1])
    try:
        return cand if cand.is_dir() else None
    except OSError:
        return None


def ask_install_path():
    """让用户指定月离目录：先弹选择框，用不了/取消了就退回让他粘贴路径。"""
    log("  正在打开文件夹选择框…")
    log("    （没看到弹窗的话，它可能躲在别的窗口后面，看一眼任务栏）")
    log()
    for _ in range(6):
        p = pick_folder("请选择月离的安装目录（里面应该有 install 文件夹）")
        if p is None:
            break
        try:
            r = p.resolve()
        except OSError:
            r = p
        if looks_like_install(r):
            save_install(r)
            log("  已选定：%s" % r)
            return r
        log("  这个目录里没有 install\\START.vbs 或")
        log("  install\\data\\memory\\memory.sqlite3，不像月离安装目录，再选一次。")
        log()

    log("  改用粘贴路径：")
    for _ in range(3):
        p = ask_path("  把月离安装目录拖进来，或粘贴路径（直接回车 = 放弃）：")
        if p is None:
            return None
        try:
            r = p.resolve()
        except OSError:
            r = p
        if looks_like_install(r):
            save_install(r)
            log("  已选定：%s" % r)
            return r
        log("  这个目录不像月离安装目录，再试一次。")
    return None


def find_install(explicit=None) -> Path:
    """定下要用哪个月离安装目录：命令行 --install / 上次记住的 / 让用户自己选。
    【绝不自己猜一个就动人家的库。】
    """
    if explicit:
        p = Path(explicit).resolve()
        if looks_like_install(p):
            # --install 只对【本次】有效，不写进配置（否则默认目标会被改成副本）。
            saved = load_saved_install()
            if saved is not None and str(saved).lower() != str(p).lower():
                log("  注意：这次是用 --install 指定的，只对本次有效。")
                log("        默认的月离仍然是：%s" % saved)
                log("        要改默认，用菜单第 3 项。")
            return p
        raise SystemExit("  这个目录不像月离安装目录（里面没有\n"
                         "      install\\data\\memory\\memory.sqlite3 或 install\\START.vbs）：\n"
                         "      %s" % p)

    saved = load_saved_install()
    if saved is not None:
        return saved

    if not sys.stdin.isatty():
        raise SystemExit("  ✗ 还没指定过月离目录。请先用 --install <月离目录> 指定一次，\n"
                         "    之后本工具会记住，不用再填。")

    log("  第一次使用，需要指定月离的安装目录（只问这一次，之后会记住）。")
    p = ask_install_path()
    if p is None:
        raise SystemExit("  没有指定月离目录，退出。")
    return p


def _powershell(script: str, timeout: int = 90) -> str:
    """跑一段 PowerShell，返回 stdout；失败返回空串。输出编码必须钉成 UTF-8：默认
    跟随系统 ANSI 代码页，命令行里一旦有非 GBK 字节，解码线程会抛异常、stdout 变空。
    """
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except Exception:
        return ""
    return done.stdout or ""


def _ps_processes():
    """本机所有进程 -> [(pid, 进程名小写, 可执行路径, 命令行), ...]。不能用 wmic：
    Win11 24H2 起已被移除，一调用就报错，被吞掉当成"没有进程"就会假报"月离已关闭"。
    """
    out = _powershell(
        "Get-CimInstance Win32_Process | ForEach-Object { "
        "'{0}|{1}|{2}|{3}' -f $_.ProcessId, $_.Name, $_.ExecutablePath, $_.CommandLine }"
    )
    rows = []
    for line in out.splitlines():
        parts = line.rstrip().split("|", 3)      # 命令行里可能有 |，只切前三刀
        if len(parts) == 4 and parts[0].strip().isdigit():
            rows.append((int(parts[0].strip()), parts[1].strip().lower(),
                         parts[2].strip(), parts[3].strip()))
    return rows


def procs_under(prefix: Path):
    """返回该目录下所有进程 pid。查不到进程时直接报错，绝不假装"没有进程"。"""
    rows = _ps_processes()
    if not rows:
        raise SystemExit("  ✗ 枚举不了本机进程（PowerShell 调用失败）。\n"
                         "    这一步是用来确认月离已经关掉的，查不到就不敢动数据库，"
                         "先手工关闭月离再试。")
    key = str(prefix).rstrip("\\").lower() + "\\"
    # 必须排除自己：本工具拿月离自带的 python.exe 跑，不排除会把自己 taskkill 掉。
    me = os.getpid()
    return [pid for pid, _, path, cmd in rows
            if pid != me
            and "olivia_memory" not in cmd.lower()
            and path.lower().startswith(key)]


def stop_yueli(install: Path):
    pids = procs_under(install)
    if pids:
        log("  停掉月离进程：%s" % ", ".join(map(str, pids)))
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    # 只杀命令行里指向【本安装目录】的那个，/IM wscript.exe /F 会带走别人所有脚本。
    needle = str(install / "install" / "START.vbs").lower()
    for pid, name, _, cmd in _ps_processes():
        if name == "wscript.exe" and needle in cmd.lower():
            log("  停掉启动器 wscript.exe (pid %s)" % pid)
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    time.sleep(4)
    left = procs_under(install)
    if left:
        raise SystemExit("  仍有 %d 个进程没关掉，请手动关闭月离后重试" % len(left))
    log("  月离已关闭 ✓")


def start_yueli(install: Path):
    vbs = install / "install" / "START.vbs"
    if not vbs.is_file():
        log("  找不到 START.vbs，请手动启动月离")
        return
    subprocess.Popen(["wscript.exe", "//B", "//Nologo", str(vbs)], shell=False)
    log("  已发出启动指令（月离启动约需 1 分钟）")


# ─────────────────────── 写进月离信件库 ───────────────────────

# 月离【自己写】的信才有的字段，用来区分 state.json 里哪些是月离写的。
NATIVE_MARKERS = (
    "private_world_status", "private_world_delivery_id", "private_world_occurred_at",
    "triage", "route_preflight", "daily_life_status", "relationship_status",
    "initiative_tier", "reply_signature", "reply_route_videos", "reply_revision",
    "life_received_at", "audit_status", "contact_qualification",
)


def load_state_letters(install: Path) -> list:
    """读 state.json 里的 letters[]（月离自己写的 + 外部工具导进去的，混在一起）。"""
    p = install / "install" / "data" / "state.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in (d.get("letters") or [])
                if isinstance(x, dict) and not x.get("superseded_by")]
    except Exception:
        return []


def is_external_import(item: dict) -> bool:
    """state.json 里这封是不是外部工具导进来的（而不是月离自己写的）。
    判据宽松没关系：只影响报告措辞，不影响改不改数据。
    """
    if not isinstance(item, dict):
        return False
    if item.get("imported_by"):
        return True
    return not any(k in item for k in NATIVE_MARKERS)


def make_memory_id(index: int, content: str, reply: str) -> str:
    """按 .soul 里的位次生成可排序的 memory_id：月离按 created_at, letter_id 降序排，
    同一分钟的信只能靠它分先后；位次越小 id 越大（.soul 和信箱都是新的在前）。
    """
    salt = hashlib.sha256((content + "\x00" + reply).encode("utf-8")).hexdigest()[:8]
    return "lb-%010d-%s" % (10 ** 9 - index, salt)


def is_iso_stamp(s) -> bool:
    """occurred_at 必须是能被解析的 ISO 字符串（记忆档案要 datetime）。"""
    if not isinstance(s, str) or not s.strip():
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def plan_state_order(state_letters: list, soul_index: dict):
    """算 state.json 里同一分钟那几封该怎么排，返回 (改动清单, 预览)。
    只动外部工具导入、且能在来源里找到的组；只改秒数，不动 letter_id。
    """
    groups = {}
    for i, x in enumerate(state_letters):
        ca = x.get("created_at")
        if isinstance(ca, (int, float)) and not isinstance(ca, bool):
            groups.setdefault(int(ca) // 60, []).append(i)

    plan, preview = [], []
    for mkey, idxs in sorted(groups.items()):
        if len(idxs) < 2 or len(idxs) > 60:
            continue
        members, ok = [], True
        for i in idxs:
            x = state_letters[i]
            key = (norm_text(x.get("content")), norm_text(x.get("reply_text")))
            if not is_external_import(x) or key not in soul_index:
                ok = False
                break
            members.append((i, soul_index[key]))
        if not ok:
            continue
        n = len(members)
        raw = [int(state_letters[i]["created_at"]) % 60 for i in idxs]
        # 只处理①秒数全一样（真并列）②秒数正好是 0..n-1 的排列（我们自己排过）两种；
        #   其它（比如 12:11:30 那种真实秒数）不碰
        if not (len(set(raw)) == 1 or sorted(raw) == list(range(n))):
            continue

        members.sort(key=lambda m: m[1])          # 来源下标小的在前
        shown, changes = [], []
        for k, (i, si) in enumerate(members):
            old = state_letters[i]["created_at"]
            new = mkey * 60 + (n - 1 - k)         # 下标小的拿最大的秒 -> 排上面
            shown.append((new, state_letters[i]))
            if new != old:
                changes.append((i, old, new, si, state_letters[i]))
        if changes:
            plan.extend(changes)
            preview.append((mkey * 60, sorted(shown, key=lambda t: t[0], reverse=True)))
    return plan, preview


def log_order_preview(preview):
    """把"排完会长什么样"打出来，依据错了用户一眼能看出来。
    必须把【哪头是上】说死：只写"从上到下"的话有人会反着读，以为排反了。
    """
    for base, items in sorted(preview, reverse=True):
        n = len(items)
        log("     %s（%d 封同一分钟），下面按【信箱从上到下】列，第 1 条在最上面："
            % (datetime.fromtimestamp(base, TZ).strftime("%Y-%m-%d %H:%M"), n))
        for pos, (new, x) in enumerate(items, 1):
            if pos == 1:
                tag = "   ︿ 最上面"
            elif pos == n:
                tag = "   ﹀ 最下面"
            else:
                tag = ""
            log("       %d. %s%s" % (pos, (x.get("content") or "")[:32].replace("\n", " "), tag))


def apply_state_order(install: Path, plan: list):
    """把顺序改动落到 state.json：备份 -> 原子写 -> 立即校验 -> 不对就还原。"""
    state_path = install / "install" / "data" / "state.json"
    bakdir = install / "install" / "data" / "memory" / "_backups"
    bakdir.mkdir(parents=True, exist_ok=True)
    bak = bakdir / ("state-order-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")

    before_text = state_path.read_text(encoding="utf-8")
    data = json.loads(before_text)
    letters = data.get("letters") or []
    n_before = len(letters)
    shutil.copy2(state_path, bak)

    before_json = json.dumps(data, ensure_ascii=False, indent=2)   # 动手前的状态拷贝
    for i, old, new, si, x in plan:
        letters[i]["created_at"] = new

    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_path)

    problems = []
    try:
        after = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        after = None
        problems.append("读不回来：%s" % e)
    if after is not None:
        al = after.get("letters") or []
        if len(al) != n_before:
            problems.append("封数变了：%d -> %d" % (n_before, len(al)))
        else:
            changed = {i: old for i, old, new, si, x in plan}
            for i, x in enumerate(al):
                if i in changed:
                    x["created_at"] = changed[i]      # 换回老值再比
            if json.dumps(after, ensure_ascii=False, indent=2) != before_json:
                problems.append("除了那几处 created_at，还有别的地方被改了")

    if problems:
        shutil.copy2(bak, state_path)                  # 还原
        log("  ✗ state.json 排序校验没过：%s" % "；".join(problems))
        log("  → 已用备份还原：%s" % bak)
        return False
    log("  state.json 排序已写入（封数 %d 未变，其余内容逐字节一致 ✓）" % n_before)
    log_line("  备份：%s" % bak)
    return True


def hidden_note(n: int) -> str:
    """给体检的数字补一句"其中 N 封是藏起来的"（0 封返回空串）。藏起来的行还在库里、
    体检照样会数到，但界面上看不到，报成"要修"只会让人白紧张。
    """
    if not n:
        return ""
    return "（其中 %d 封是工具藏起来的，界面上看不到）" % n


def cmd_check(install_arg) -> int:
    """体检：看看库里现在什么状况。【只读，绝不改动，也不关月离】"""
    log()
    log("=" * 62)
    log("  体检：现在库里是什么状况（只读，绝不改动，也不关月离）")
    log("=" * 62)
    install = find_install(install_arg)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    log("  月离: %s" % install)
    log("  数据库: %s" % db)
    log()

    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)

    legacy = []
    for mid, mj in con.execute("SELECT memory_id, metadata_json FROM legacy_letters"):
        meta = json.loads(mj or "{}")
        br = meta.get("backup_record") if isinstance(meta.get("backup_record"), dict) else {}
        legacy.append({
            "where": "legacy_letters",
            "content": (meta.get("user_content") or "").strip(),
            "reply": (meta.get("reply_text") or "").strip(),
            "ca": br.get("created_at"),
            "kind": meta.get("import_kind"),
            "has_backup": bool(br),
            "mid": mid,
            # 菜单 8 藏起来的行界面上看不到，体检的数字要单独标出来，免得让人白紧张
            "hidden": bool(meta.get(HIDDEN_KIND_KEY)),
        })
    arch_n = {}
    try:
        c2 = sqlite3.connect("file:%s?mode=ro"
                            % str(db.parent / "mem0" / "original-text-index.sqlite3").replace("\\", "/"),
                            uri=True)
        for t in ("originals", "chunks"):
            try:
                arch_n[t] = c2.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            except Exception:
                arch_n[t] = "?"
        c2.close()
    except Exception:
        pass
    con.close()

    state_all = load_state_letters(install)
    state_rows = []
    for x in state_all:
        state_rows.append({
            "where": "state.json",
            "content": (x.get("content") or "").strip(),
            "reply": (x.get("reply_text") or "").strip(),
            "ca": x.get("created_at"),
            "kind": "月离写的" if not is_external_import(x) else "外部工具导入的",
            "mid": x.get("letter_id") or "",
            "hidden": False,      # state.json 里带 superseded_by 的已经被 load_state_letters 滤掉了
        })

    # ── 1) 总量 ──
    imp = sum(1 for r in state_rows if r["kind"] == "外部工具导入的")
    nat = len(state_rows) - imp
    log("  信箱一共 %d 封" % (len(legacy) + len(state_rows)))
    log("     legacy_letters 表（导入进来的）  %d 封%s"
        % (len(legacy), hidden_note(sum(1 for r in legacy if r.get("hidden")))))
    log("     state.json 里的                  %d 封" % len(state_rows))
    if state_rows:
        log("        ├ 外部工具导入的（imported_by 那类）  %d 封" % imp)
        log("        └ 月离自己写的（有 private_world 那套） %d 封" % nat)
    log()

    # ── 2) 时间是否正确 ──
    ok_t = bad_text = bad_none = bad_other = 0
    hid_t = collections.Counter()      # 每一类里有多少是藏起来的
    for r in legacy + state_rows:
        ca = r["ca"]
        if isinstance(ca, bool) or ca is None:
            # 连 backup_record 都没有：信箱显示"时间未知"（两种成因算一类就够）
            bucket = "none"
            bad_none += 1
        elif isinstance(ca, (int, float)):
            bucket = "ok"
            ok_t += 1
        elif isinstance(ca, str):
            bucket = "text"            # 字符串：信箱显示 NaN-NaN-NaN
            bad_text += 1
        else:
            bucket = "other"
            bad_other += 1
        if r.get("hidden"):
            hid_t[bucket] += 1
    log("  时间")
    log("     数字（正确）                    %d 封" % ok_t)
    if bad_text:
        log("     存成了文字（信箱显示 NaN-NaN-NaN）  %d 封   ← 要修%s"
            % (bad_text, hidden_note(hid_t["text"])))
    if bad_none:
        log("     没有时间（信箱显示「时间未知」）    %d 封   ← 要修%s"
            % (bad_none, hidden_note(hid_t["none"])))
    if bad_other:
        log("     其它格式                        %d 封" % bad_other)

    # ── 3) 重复导入 ──
    pairs = collections.Counter((norm_text(r["content"]), norm_text(r["reply"]))
                                for r in legacy + state_rows
                                if (r["content"] or r["reply"]))
    dups = {k: v for k, v in pairs.items() if v > 1}
    log()
    log("  重复（比正文时忽略空白和换行差异）")
    log("     同一封出现多次的  %d 组，涉及 %d 封"
        % (len(dups), sum(dups.values())))
    n_dup_hidden = sum(1 for r in legacy + state_rows if r.get("hidden")
                       and (norm_text(r["content"]), norm_text(r["reply"])) in dups)
    if n_dup_hidden:
        log("       （其中 %d 封是工具藏起来的，界面上看不到）" % n_dup_hidden)
    for k, v in list(dups.items())[:3]:
        log("       %d 次: %s" % (v, k[0][:34].replace("\n", " ")))

    # ── 内容完整性 ──
    # 判重按 (去信 + 回信) 比：有行没存下正文的话判重失效、那些信会被【再导一遍】。
    all_rows = legacy + state_rows
    empty_c = sum(1 for r in all_rows if not (r["content"] or r["reply"]))
    log()
    log("  内容完整性（判重就是比这个）")
    log("     正文和回信都是空的    %d 封" % empty_c)
    log("     内容去重后            %d 种" % len(pairs))
    # 差额是恒等式、不是"对不上"：非空封数 - 去重种数 = 重复多出来的份数，别当异常报。
    extra = (len(all_rows) - empty_c) - len(pairs)
    if extra:
        log("     （非空的 %d 封去重后是 %d 种，少掉 %d 份，正好是上面「重复」"
            "那栏多出来的，正常）"
            % (len(all_rows) - empty_c, len(pairs), extra))
    if empty_c:
        log("     有 %d 封连正文都没存下来，【判重认不出它们】，" % empty_c)
        log("       下次导入同一个 .soul 会被当成新信再导一遍。")

    # 疑似重复：连空格换行、零宽字符都去掉后正文仍一样，判重最容易漏的就是它们。
    tight = {}
    for r in all_rows:
        k = re.sub(r"[\s\u200b-\u200f\ufeff\u00ad\u2060]+", "",
                   (r["content"] or "") + "\x00" + (r["reply"] or ""))
        if k:
            tight.setdefault(k, []).append(r)
    tight_dups = {k: v for k, v in tight.items() if len(v) > 1}
    log()
    log("  疑似重复（连空格、换行、零宽字符都去掉后仍相同的）")
    log("     %d 组，涉及 %d 封" % (len(tight_dups), sum(len(v) for v in tight_dups.values())))
    n_tight_hidden = sum(1 for v in tight_dups.values() for x in v if x.get("hidden"))
    if n_tight_hidden:
        log("     （其中 %d 封是工具藏起来的，界面上看不到）" % n_tight_hidden)
    if len(tight_dups) > len(dups):
        log("     比上面那栏多出 %d 组，说明有信的差异【只在空白或不可见字符上】，"
            % (len(tight_dups) - len(dups)))
        log("       这正是判重最容易漏的情况。把这张截图发出来。")
        for k, v in list(tight_dups.items())[:3]:
            log("       例：%d 封，%s" % (len(v), v[0]["content"][:34].replace("\n", " ")))

    # 只比"去信"（不管回信）：分辨"同一封来信、回信不一样"，按规则【不算重复】。
    by_content = {}
    for r in all_rows:
        k = norm_text(r["content"])
        if k:
            by_content.setdefault(k, []).append(r)
    c_dups = {k: v for k, v in by_content.items()
              if len(v) > 1 and len({norm_text(x["reply"]) for x in v}) > 1}
    log()
    log("  去信相同、但回信不同的")
    log("     %d 组，涉及 %d 封" % (len(c_dups), sum(len(v) for v in c_dups.values())))
    n_c_hidden = sum(1 for v in c_dups.values() for x in v if x.get("hidden"))
    if n_c_hidden:
        log("     （其中 %d 封是工具藏起来的，界面上看不到）" % n_c_hidden)
    if c_dups:
        log("     这些是【同一封来信配了不同的回信】。按现在规则不算重复（不会合并），")
        log("     但如果你觉得该算，把这栏截图发出来，我们再商量怎么办。")
        for k, v in list(c_dups.items())[:2]:
            log("       例（%d 个版本）: %s" % (len(v), k[:34]))

    # ── 4) 同一分钟并列 ──
    groups = collections.Counter(int(r["ca"]) // 60 for r in legacy + state_rows
                                 if isinstance(r["ca"], (int, float))
                                 and not isinstance(r["ca"], bool))
    tie = [m for m, n in groups.items() if n > 1]
    log()
    log("  顺序")
    log("     同一分钟并列的  %d 组，涉及 %d 封" % (len(tie), sum(groups[m] for m in tie)))
    log("     （这些信的先后靠内部 id 定，界面只显示到分钟所以看不出来）")

    # ── 4.5) 僵尸信（发送失败的） ──
    # 必须看【没过滤过的】state.json（load_state_letters() 会把带 superseded_by 的滤掉）。
    _st_txt, _st_raw = _load_state_raw(install)
    zs = _zombies_of(_st_raw if isinstance(_st_raw, dict) else {"letters": state_all})
    log()
    log("  发送失败的信（信箱里那张「寄信通道好像有点忙」）")
    log("     一共 %d 封" % len(zs))
    for ln in _zombie_lines(zs):
        log(ln)
    n_hid = sum(1 for x in zs if x.get("superseded_by"))
    if n_hid:
        log("     其中 %d 封月离已经自己藏了（界面上本来就看不到）" % n_hid)
    if zs:
        log("     想让它们从信箱里消失：菜单 6（一键清理）")

    # ── 5) 记忆档案 ──
    if arch_n:
        log()
        log("  记忆档案（月离「回忆」用的）")
        for t, n in arch_n.items():
            log("     %-12s %s 行" % (t, n))

    log()
    log("=" * 62)
    log("  以上都是只读得出的：数据库一个字节没改，月离也没被关过。")
    log("=" * 62)
    log()
    return 0


# ─────────── 并排对比：源文件 vs 库里（只读，给用户拍板用） ───────────

def _peek(s, n: int = 46) -> str:
    """一句预览：开头 n 个字。换行和看不见的字符（零宽、软连字符）都压成一个空格，
    否则控制台里会断行，也容易把只差不可见字符的两版看成"一模一样"。
    """
    t = re.sub(r"[\s\u200b-\u200f\ufeff\u00ad\u2060]+", " ", str(s or "")).strip()
    return t[:n] + ("…" if len(t) > n else "")


def _when(ca) -> str:
    """库里那封的时间长什么样，三种坏法要分辨得出来。"""
    if isinstance(ca, bool) or ca is None:
        return "时间未知"
    if isinstance(ca, (int, float)):
        return datetime.fromtimestamp(ca, TZ).strftime("%Y-%m-%d %H:%M")
    return str(ca)[:16].replace("T", " ") + "（存成了文字）"


def _diff_lines(lib_reply, src_reply, n: int = 46):
    """两版回信差在哪，返回 1~2 行，这是用户拍板的依据。最常见的是"一版 = 另一版 +
    后面多一截"，所以先认这种关系、再把多出来的那截开头摆出来。
    """
    a, b = norm_text(lib_reply), norm_text(src_reply)
    if a == b:
        return ["两版只差空白/换行（内容其实一样）"]
    if a.startswith(b):
        return ["库里那版 = 源文件那版 + 后面多出 %d 字" % (len(a) - len(b)),
                "库里那版第 %d 字之后是：「%s」" % (len(b), _peek(a[len(b):], n))]
    if b.startswith(a):
        return ["源文件那版 = 库里那版 + 后面多出 %d 字" % (len(b) - len(a)),
                "源文件那版第 %d 字之后是：「%s」" % (len(a), _peek(b[len(a):], n))]
    p = 0
    for x, y in zip(a, b):
        if x != y:
            break
        p += 1
    return ["两版前面 %d 字一样，从第 %d 字起就不一样" % (p, p + 1)]


def cmd_compare(src: Path, install_arg) -> int:
    """并排对比源文件和库里"去信一样、回信不同"的那几封。【只读】回信换了代判重就
    认不出来、会被当新信再导一遍，但改成"去信相同就算同一封"又会吃掉真信。
    """
    log()
    log("=" * 62)
    log("  并排对比：源文件 和 库里「去信一样、回信不同」的那几封")
    log("  只读：不关月离、不写库、不改任何文件")
    log("=" * 62)
    install = find_install(install_arg)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    log("  月离: %s" % install)
    log("  数据库: %s" % db)
    log()

    log("  读取来源…")
    rows, names = load_source(src)
    if not rows:
        log("  没读到任何信件。")
        return 1
    log("  读到 %d 封（来自 %s）" % (len(rows), "、".join(names[:3])))
    log()

    # ── 只读打开库，两个存储都收进来（判重本来就是跨两个存储做的）──
    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    lib, n_leg, rescued = read_library(con, install)
    con.close()
    for L in lib:
        L["detail"] = ("legacy_letters" if L["store"] == "legacy"
                       else ("state.json" if is_external_import(L["meta"])
                             else "state.json（月离自己写的）"))
    log("  库里 %d 封（legacy_letters %d + state.json %d）"
        % (len(lib), n_leg, len(lib) - n_leg))
    if rescued:
        log("     其中 %d 封的正文只落在 content 那一列里（metadata 里没有），" % rescued)
        log("       月离渲染信箱时会回落到那一列，所以界面上看得见；改造前的本工具")
        log("       只读 metadata，看不见 → 会把它们误判成新信。现已按月离的规则认出来。")
    log()

    # ── 第一遍：认逐字相同的 ── 索引 pop 消耗，让源 2 封 ↔ 库 2 封一对一配上
    by_key, by_content, by_reply_filled, by_reply_empty = build_match_index(lib)
    uniq = unique_source_rows(rows)

    # ── 分类：和 cmd_apply 走同一个函数，两边门槛结构上没法再分叉 ──
    items, unclaimed_all = classify_source(
        uniq, lib, (by_key, by_content, by_reply_filled, by_reply_empty))
    # 菜单 8 自己藏起来的那批也在这里面，报"多出来的"时得单独说。
    hidden_orphans, orphans = split_orphans(unclaimed_all)

    matched = sum(1 for it in items if it["kind"] == MATCH_EXACT)
    filled = [it for it in items if it["kind"] == MATCH_FAKED]
    ambiguous = [it for it in items if it["kind"] == MATCH_CONFLICT
                 and it["conflict_kind"] == CONFLICT_SAME_CONTENT]
    near_pairs = [it for it in items if it["kind"] == MATCH_CONFLICT
                  and it["conflict_kind"] == CONFLICT_NEAR]
    brand_new = [it for it in items if it["kind"] == MATCH_NEW]

    no_inc = sum(1 for it in brand_new if not norm_text(it["row"]["content"]))
    no_rep = sum(1 for it in brand_new if not norm_text(it["row"]["reply_text"]))

    # ── 第二遍：把"去信一样、回信不同"的摆出来 ──
    src_name = names[0] if len(names) == 1 else "源文件"
    cards = []
    for n_i, it in enumerate(ambiguous):
        i, r, cands = it["index"], it["row"], it["candidates"]
        L = cands[0]
        rep_lib, rep_src = norm_text(L["reply"]), norm_text(r["reply_text"])
        diffs = _diff_lines(L["reply"], r["reply_text"])
        card = [
            "",
            "  ── [%d/%d] ──────────────────────────────────────"
            % (n_i + 1, len(ambiguous)),
            "     去信（两边逐字相同，%d 字）：%s"
            % (len(norm_text(r["content"])), _peek(r["content"])),
            "",
            "      ① 库里那版    %s · %s · 回信 %d 字"
            % (L["detail"], _when(L["ca"]), len(rep_lib)),
            "           「%s」" % _peek(L["reply"]),
            "      ② 源文件那版  %s · %s · 回信 %d 字"
            % (src_name, _when(r["created_at"]), len(rep_src)),
            "           「%s」" % _peek(r["reply_text"]),
            "",
            "     差别：%s" % diffs[0],
        ]
        card += ["           %s" % x for x in diffs[1:]]
        if L.get("used_by") is not None:
            card.append("     库里这封已经和源文件第 %d 封【逐字】对上了，"
                        " 这里只是去信又撞了一次。" % (L["used_by"] + 1))
        if len(cands) > 1:
            card.append("     （库里还有 %d 封去信也一样，这里只摆了第一封）"
                        % (len(cands) - 1))
        cards.append(card)

    # ── 第三遍：把"库里找不到能配上的"逐封列出来（只报数字没法查是哪几封）──
    lost_head, lost_items = [], []
    if brand_new:
        lost_head = ["", "  ══ 库里找不到能配上的 %d 封（逐封列出，按源文件顺序）══"
                     % len(brand_new), ""]
        if no_inc or no_rep:
            lost_head += [
                "     其中 去信为空 %d 封 / 回信为空 %d 封。" % (no_inc, no_rep),
                "       月离自带的 letter_pairs.json 导入【要求去信和回信都非空】",
                "       （offline_letter_pairs.py:228），这种信在那条路径上会被整封",
                "       拒掉，所以库里本来就不会有它，不是判重漏了。",
                "",
            ]
        for it in brand_new:
            i, r = it["index"], it["row"]
            t = (datetime.fromtimestamp(r["created_at"], TZ).strftime("%Y-%m-%d %H:%M")
                 if r["created_at"] else "时间未知")
            c, p = norm_text(r["content"]), norm_text(r["reply_text"])
            item = ["     [源文件第 %d 封] %s   去信 %d 字 · 回信 %d 字"
                    % (i + 1, t, len(c), len(p)),
                    "         去信：「%s」" % (_peek(r["content"], 40) or "（空）"),
                    "         回信：「%s」" % (_peek(r["reply_text"], 40) or "（空）")]
            if not c or not p:
                item.append("         %s是空的，见开头那段说明"
                            % ("去信" if not c else "回信"))
            lost_items.append(item)

    # ── 近似匹配：配对在 classify_source 里做完了，这里只摆出来 ──
    fuzzy_head, fuzzy_items = [], []
    pairs = [(it["index"], it["row"], it["hit"], it["near"][2], it["near"][3])
             for it in near_pairs]
    if pairs:
        fuzzy_head = [
            "",
            "  ══ 近似匹配：和库里很像、但正文被改过的 %d 封 ══" % len(pairs),
            "",
            "     这几对【多半是同一封信】，只是正文被改过（换字、换歌名、多一段前缀），",
            "     按全等比认不出来。本工具只配对、不改库也不改判重，留哪版你看完再定。",
            "     （相似度用字符二元组算，满分 100；只列最接近的一对。"
            "门槛 %d 分、且参与比对的那一边至少 %d 字，短文本的相似度虚高，"
            % (int(FUZZY_MIN * 100), FUZZY_MIN_LEN),
            "       不设这两道关会把「测试来信六」这种配到「测试来信一」头上。）",
            "",
        ]
        for i, r, L, sc, sr in pairs:
            t = (datetime.fromtimestamp(r["created_at"], TZ).strftime("%Y-%m-%d %H:%M")
                 if r["created_at"] else "时间未知")
            fuzzy_items.append([
                "     [源文件第 %d 封] %s   去信 %d 字 · 回信 %d 字"
                % (i + 1, t, len(norm_text(r["content"])),
                   len(norm_text(r["reply_text"]))),
                "         源文件版  去信：「%s」" % (_peek(r["content"], 38) or "（空）"),
                "                   回信：「%s」" % (_peek(r["reply_text"], 38) or "（空）"),
                "         库里那行  memory_id=%s" % L["mid"],
                "                   去信：「%s」" % (_peek(L["content"], 38) or "（空）"),
                "                   回信：「%s」" % (_peek(L["reply"], 38) or "（空）"),
                "         相似度：去信 %d%% · 回信 %d%%   ← %s"
                % (sc * 100, sr * 100, _verdict(sc, sr)),
                "",
            ])

    # ── 反过来也要报：库里有、源文件里找不到对应的那几行 ──
    #   由 classify_source 一并算出。信箱"总数"是 legacy_letters 的行数，不查内容不去重。
    orphan_head, orphan_items = [], []
    if orphans:
        orphan_head = [
            "",
            "  ══ 库里有、但源文件里找不到对应的 %d 封 ══" % len(orphans),
            "",
            "     这几行在源文件里都没有能【单独】配上的那一封。正常应该是 0。",
            "     不是 0 就说明库里多出了东西，重复导入、来自别的 .soul 导出、或被人改过。",
            "     （同一个去信在库里有两封、而源文件只有一封，也会算进来。）",
            "     只报 legacy_letters 表；state.json 里的信本来就可能不来自 .soul。",
        ]
        if fuzzy_items:
            orphan_head += [
                "     已经和上面「近似匹配」配上对的那几行，不在这里重复列。",
            ]
        orphan_head.append("")
        for L in orphans:
            item = [
                "     [库里] memory_id=%s" % L["mid"],
                "         去信「%s」" % (_peek(L["content"], 40) or "（空）"),
                "         回信「%s」" % (_peek(L["reply"], 40) or "（空）"),
            ]
            near = _nearest_source(L, rows)
            if near is not None:
                si, _r, sc, sr, _score = near
                item.append("         ↑ 和源文件第 %d 封很像：去信 %d%% · 回信 %d%%"
                            % (si + 1, sc * 100, sr * 100))
                item.append("           多半是同一封信的两个版本。正文不是逐字相同，")
                item.append("           所以判重不会合并它们；用菜单 8 收拾（留有时间的、藏没时间的）。")
            orphan_items.append(item)

    # ── "回信一样、库里那句去信是硬加的"也列出来 ──
    filled_head, filled_items = [], []
    if filled:
        filled_head = [
            "",
            "  ══ 回信一样、但库里那句去信是硬加的 %d 封 ══" % len(filled),
            "",
            "     这些是【同一封】，不是两封。月离自带的导入要求去信非空，所以当初",
            "     有人给本来没去信的信硬加了一句。按规矩【以没有去信的那版为准】：",
            "     跑菜单 2 会把这句硬加的去信清掉，不会重复导入。",
            "",
        ]
        for it in filled:
            i, r, L = it["index"], it["row"], it["hit"]
            t = (datetime.fromtimestamp(r["created_at"], TZ).strftime("%Y-%m-%d %H:%M")
                 if r["created_at"] else "时间未知")
            filled_items.append([
                "     [源文件第 %d 封] %s   回信 %d 字"
                % (i + 1, t, len(norm_text(r["reply_text"]))),
                "         源文件（以此为准）：去信【空】",
                "         库里那句硬加的去信：「%s」" % (_peek(L["content"], 40) or "（空）"),
            ])

    # ── 汇总 ──
    log("  源文件 %d 封里：" % len(rows))
    if len(uniq) != len(rows):
        log("     （其中 %d 封是同一批里逐字重复的，和 apply 一样只算一次）"
            % (len(rows) - len(uniq)))
    log("     已经对上库里的（去信+回信都逐字一样）  %d 封" % matched)
    if filled:
        log("     回信一样、库里那句去信是硬加的        %d 封   ← 算同一封，"
            % len(filled))
        log("                                                  以【没有去信】的版为准")
    log("     去信一样、回信不同                      %d 封   ← 就是这几封"
        % len(ambiguous))
    if near_pairs:
        log("     近似匹配（和库里很像、正文被改过）      %d 封   ← 详细在下面"
            % len(near_pairs))
    log("     库里找不到能配上的（真的新信）          %d 封   ← 逐封列在下面"
        % len(brand_new))
    if no_inc or no_rep:
        log("       （其中 去信为空 %d 封 / 回信为空 %d 封）" % (no_inc, no_rep))
    if ambiguous:
        log()
        log("     ※ 上面「去信一样、回信不同」和下面的「近似匹配」，是同一类：")
        log("       【源文件和库里各有一版】。菜单 2 会问你怎么处理（--near 也行）：")
        log("         1 按源文件 / 2 按库  → 算【修复】，不会新增")
        log("         3 两个都保留          → 算【修复】，另外每个再【新增】一封")
        log("       所以菜单 2 的「需要新增」= 上面的【真新】+（选 3 时的冲突数）。")
    if orphans:
        log("     库里有、源文件里没对应的（多出来的）    %d 封   ← 逐封列在下面"
            % len(orphans))
        log("       ↑ 不计入上面三栏（那些是源文件侧的）。这几行就是「数量对得上、")
        log("         内容对不上」的来源。")
    if hidden_orphans:
        log("     另外 %d 封是【本工具自己藏起来的】（菜单 8 藏掉的重复），不算多出来 ✓"
            % len(hidden_orphans))

    if ambiguous:
        # ambiguous 里是 classify_source 的字典（7 个键），不能再用元组方式解包：
        #   一旦有冲突信就 ValueError，且崩在【汇总之后】，后面几栏和报告都没产出。
        pref = sum(1 for it in ambiguous
                   if "多出" in _diff_lines(it["hit"]["reply"], it["row"]["reply_text"])[0])
        log()
        log("  下面按【源文件里的顺序】摆出来，每组两版并排。")
        log("  其中 %d 组是「一版 = 另一版 + 后面多一截」（加减歌词就是这种）。" % pref)
        log()
        CONSOLE_MAX = 40
        for idx, card in enumerate(cards):
            for ln in card:
                if idx < CONSOLE_MAX:
                    log(ln)
                else:
                    log_line(ln)          # 控制台不刷屏，日志里记全
        if len(cards) > CONSOLE_MAX:
            log()
            log("      …另有 %d 组（控制台不刷屏了，完整清单在下面的报告文件里）"
                % (len(cards) - CONSOLE_MAX))
    else:
        log()
        log("  没有「去信一样、回信不同」的信，判重不会漏，这一栏不用管。")

    if lost_head:
        for ln in lost_head:
            log(ln)
        LOST_MAX = 40
        for k, item in enumerate(lost_items):
            for ln in item:
                if k < LOST_MAX:
                    log(ln)
                else:
                    log_line(ln)          # 控制台不刷屏，日志和报告里记全
        if len(lost_items) > LOST_MAX:
            log("     …另有 %d 封（完整清单在报告文件里）" % (len(lost_items) - LOST_MAX))

    if filled_head:
        for ln in filled_head:
            log(ln)
        for item in filled_items:
            for ln in item:
                log(ln)

    if fuzzy_head:
        for ln in fuzzy_head:
            log(ln)
        for item in fuzzy_items:
            for ln in item:
                log(ln)

    if orphan_head:
        for ln in orphan_head:
            log(ln)
        for item in orphan_items:
            for ln in item:
                log(ln)

    # ── 报告文件：放源文件旁边，方便整份发出来核对 ──
    rep = (src if src.is_dir() else src.parent) / (
        "对比-%s.txt" % (src.stem if src.is_file() else "记忆包"))
    head = [
        "Olivia 记忆迁移工具 · 并排对比",
        "",
        "本文件不是 .soul，导入时请拖 .soul，别把本文件拖进去。",
        "  （实测有人拖错过：它和 .soul 放在同一层、名字也像。）",
        "=" * 62,
        "月离 : %s" % install,
        "源   : %s" % src,
        "时间 : %s" % datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z"),
        "",
        "源文件 %d 封：已对上 %d 封 / 去信一样回信不同 %d 封 / 真新信 %d 封"
        % (len(rows), matched, len(ambiguous), len(brand_new)),
        ("（同一批里逐字重复的 %d 封只算了一次，和 apply 口径一致）"
         % (len(rows) - len(uniq))) if len(uniq) != len(rows) else "",
        "",
        "下面是这些信的两版片段，用来判断「同一封换了回信」还是「两封真的各写了一次」。",
        "（只截了开头，不是全文；核对完这个文件可以删）",
        "",
    ]
    body = [ln for card in cards for ln in card]
    body += lost_head + [ln for item in lost_items for ln in item]
    body += filled_head + [ln for item in filled_items for ln in item]
    body += fuzzy_head + [ln for item in fuzzy_items for ln in item]
    body += orphan_head + [ln for item in orphan_items for ln in item]
    try:
        rep.write_text("\n".join(head + body) + "\n", encoding="utf-8")
        log()
        log("  完整对比已写成：%s" % rep)
    except OSError as ex:
        log()
        log("  （报告文件没写出来：%s）" % ex)

    log()
    log("=" * 62)
    log("  以上都是只读得出的：数据库一个字节没改，月离也没被关过。")
    log("  看清之后要决定的是：这种信算同一封（留哪版），还是两封都留。")
    log("=" * 62)
    log()
    return 0


def plan_apply(items, rows, install, near, dry_run):
    """把 classify_source 的结果变成"要修哪些、要新加哪些"，返回 (要修的, 要加的,
    用的策略, 统计)；冲突在这一步问用户或由 --near 指定。纯计算，不写库。
    """
    # 同一分钟的信只能靠 memory_id 定先后，先找出哪些时间撞在一起
    by_time = {}
    for r in rows:
        by_time[r["created_at"]] = by_time.get(r["created_at"], 0) + 1
    tied_times = {t for t, n in by_time.items() if n > 1}

    to_fix, to_add, to_conflict = [], [], []
    ok_already = ok_in_state = 0
    stat = collections.Counter()

    for it in items:
        index, r = it["index"], it["row"]
        item = {"row": r, "index": index, "rec": record_of(r),
                "occ": iso_of(r["created_at"]),
                "id": make_memory_id(index, r["content"], r["reply_text"])}
        hit = it["hit"]

        if it["kind"] == MATCH_NEW:
            to_add.append(item)
            continue
        if it["kind"] == MATCH_CONFLICT:
            h2 = dict(hit)
            h2["conflict_kind"] = it["conflict_kind"]
            item["hit"] = h2
            to_conflict.append(item)
            continue
        if hit["store"] == "state":
            # 已经在 state.json 里了，不新增也不动它：那里的 letter_id 是记忆档案的索引键
            # （reply:<letter_id>），改了会让档案按新 id 另建一套、旧的成孤儿。
            ok_in_state += 1
            continue

        reasons = []
        if hit["import_kind"] != KIND:
            reasons.append("形态")
            stat["kind"] += 1
        if not isinstance(hit["backup_created_at"], (int, float)):
            if hit["has_backup"]:
                reasons.append("时间文字")     # 有 backup_record 但时间写成了字符串
                stat["time_text"] += 1
            else:
                reasons.append("没有时间")     # 连 backup_record 都没有，信箱显示"时间未知"
                stat["no_time"] += 1
        if not is_iso_stamp(hit["occurred_at"]):
            reasons.append("档案时间")
            stat["occ"] += 1
        # 库里那句去信是【硬加】的（与来源内容根本不同），要清掉，以没去信的那版为准。
        if hit.get("content_is_faked") and norm_text(hit.get("raw_content")):
            reasons.append("硬加的去信")
            stat["faked"] += 1
        # 正文只差空白（导入时换行丢了），用来源的正文覆盖回去。
        elif (hit.get("raw_content", "") != r["content"]
                or hit.get("raw_reply", "") != r["reply_text"]):
            reasons.append("正文")
            stat["text_diff"] += 1
        # 顺序只在【同一分钟】才有歧义，没必要为它去动别的主键
        if hit["mid"] != item["id"] and r["created_at"] in tied_times:
            reasons.append("顺序")
            item["reorder"] = True
        if reasons:
            item["hit"] = hit
            item["reasons"] = reasons
            to_fix.append(item)
        else:
            ok_already += 1

    # ── 冲突怎么处理：交给用户选（或命令行 --near 指定）──
    near_choice = near
    n_state_skip = 0
    if to_conflict:
        near_choice = _ask_near_choice(to_conflict, near_choice, dry_run)
        for item in to_conflict:
            hit = item["hit"]
            if hit.get("store") == "state":
                # state.json 工具只读：改它的 letter_id 会让记忆档案另建一套、旧的孤儿。
                #   所以"就地改"做不了，只能按策略办：按源文件/两个都保留就【加一封】，
                #   按库则什么都不用做。不能说"跳过就完了"，那样策略等于没生效。
                if near_choice in ("source", "both"):
                    item["also_add"] = True
                else:
                    n_state_skip += 1
                continue
            item["keep_library_text"] = near_choice in ("library", "both")
            if near_choice == "both":
                # 库里那版要排在上面，给它的 id 换成 'z' 前缀（见 _pair_id）
                item["id"] = _pair_id(item["index"], item["row"]["content"],
                                      item["row"]["reply_text"], above=True)
                item["reorder"] = True
                item["also_add"] = True
            item["reasons"] = ["冲突:" + near_choice]
            to_fix.append(item)

        for item in to_conflict:
            if item.get("also_add"):
                # 源文件那版另插一封，id 用 'y' 前缀，紧挨在库里那版下面
                to_add.append({
                    "row": item["row"], "index": item["index"],
                    "rec": item["rec"], "occ": item["occ"],
                    "id": _pair_id(item["index"], item["row"]["content"],
                                   item["row"]["reply_text"], above=False),
                })

        log()
        log("  冲突处理方式：%s  共 %d 封" % (NEAR_LABEL[near_choice], len(to_conflict)))
        n_state_conf = sum(1 for it in to_conflict if it["hit"].get("store") == "state")
        if n_state_conf:
            log("     其中 %d 封库里那版在 state.json，工具对它只读（改 id 会毁记忆档案）。"
                % n_state_conf)
            if n_state_skip:
                log("     选「按库」时这 %d 封不用动（库里那版本来就在）。" % n_state_skip)
        if near_choice == "both":
            log("     另新增 %d 封（源文件那版）；库里那版原样保留，只改时间/排序。"
                % sum(1 for it in to_conflict if it.get("also_add")))
        log()

    stat["ok"] = ok_already
    stat["in_state"] = ok_in_state
    stat["conflict"] = len(to_conflict)
    stat["state_skip"] = n_state_skip
    return to_fix, to_add, near_choice, stat


def cmd_apply(src: Path, install_arg, dry_run: bool = False, near=None) -> int:
    log()
    log("=" * 62)
    if dry_run:
        log("  体检：看看现在是什么状况（只读，绝不改动）")
    else:
        log("  把 .soul / json 写进月离的信件档案（时间用正确格式）")
    log("=" * 62)

    if not install_arg:
        log("  确定月离安装目录…")
    install = find_install(install_arg)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    log("  月离: %s" % install)
    log("  数据库: %s" % db)
    log()

    log("  读取来源…")
    rows, names = load_source(src)
    withtime = sum(1 for r in rows if r["created_at"] is not None)
    log("  读到 %d 封（带时间 %d 封）" % (len(rows), withtime))
    if not rows:
        log("  没读到任何信件。")
        return 1
    log()

    if dry_run:
        log("  体检模式：不关月离、不备份、不写库。")
        log("  数据库用【只读】方式打开，物理上就写不进去。")
        con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
        bak = None
    else:
        log("  关闭月离…")
        stop_yueli(install)

        bakdir = db.parent / "_backups"
        bakdir.mkdir(parents=True, exist_ok=True)
        bak = bakdir / ("memory-apply-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
        # isolation_level=None = 改由我们显式 BEGIN/COMMIT（DDL 不进自动事务，会丢触发器）。
        con = sqlite3.connect(str(db), isolation_level=None)
        con.execute("VACUUM INTO ?", (str(bak),))
        log("  已备份 -> %s (%.2f MB)" % (bak.name, bak.stat().st_size / 1024 / 1024))

    try:
        # ── 读库 ──
        # 判重：去信+回信全同才算同一封；读库和建索引跟 cmd_compare 共用一处。
        lib, total_rows, _rescued = read_library(con, install)
        by_key, by_content, by_r_filled, by_r_empty = build_match_index(lib)
        lib_rows = lib

        state_all = load_state_letters(install)
        state_imported = sum(1 for x in state_all if is_external_import(x))
        state_native = len(state_all) - state_imported

        log("  现在库里 %d 封（legacy_letters 表）" % total_rows)
        if state_all:
            log("  state.json 里 %d 封：%d 封是外部工具导入的，%d 封是月离自己写的"
                % (len(state_all), state_imported, state_native))

        soul_index = {(norm_text(r["content"]), norm_text(r["reply_text"])): i
                      for i, r in enumerate(rows)}
        state_plan, state_preview = plan_state_order(state_all, soul_index)

        # ── 分类 + 定策略 ── 归类由 classify_source 说了算，compare 用的是同一个
        items, unclaimed_all = classify_source(
            unique_source_rows(rows), lib,
            (by_key, by_content, by_r_filled, by_r_empty))
        hidden_unclaimed, unclaimed = split_orphans(unclaimed_all)
        to_fix, to_add, near_choice, stat = plan_apply(
            items, rows, install, near, dry_run)

        log()
        if stat["kind"]:
            log("      其中 %d 封是「无时间」形态（走 letter_pairs.json 导入的，"
                "光改时间字段对它没用）" % stat["kind"])
        if stat["no_time"]:
            log("      其中 %d 封没有时间（信箱显示「时间未知」）" % stat["no_time"])
        if stat["time_text"]:
            log("      其中 %d 封的时间存成了文字（信箱显示 NaN-NaN-NaN）" % stat["time_text"])
        if stat["occ"]:
            log("      其中 %d 封的档案时间不是合法 ISO" % stat["occ"])
        if stat["text_diff"]:
            log("      其中 %d 封的正文和来源【只差空白】（当初导入时换行丢了），"
                % stat["text_diff"])
            log("      会用来源的正文覆盖回去，把换行补好。")
        if stat["faked"]:
            log("      其中 %d 封库里的去信是【硬加】的（来源里根本没有去信），"
                % stat["faked"])
            log("      当初为了能导进月离才加的。会清掉它，以没有去信的那版为准。")
        log()
        if stat["in_state"]:
            log("  有 %d 封已经在 state.json 里了（社区工具之类导进去的）"
                "→ 不重复导入，也不会去动它" % stat["in_state"])
        if state_native:
            log("  月离自己写的 %d 封：也在 state.json 里，本工具不碰" % state_native)
        log()
        log("  需要修复 %d 封，需要新增 %d 封，已经是对的 %d 封"
            % (len(to_fix), len(to_add), stat["ok"]))
        if unclaimed:
            # 这些行在库里、但【源文件里没有对应】。菜单 2 只处理源文件里有的，
            #   所以永远碰不到它们，不报出来的话用户只会觉得"怎么没修"。
            n_off = sum(1 for L in unclaimed if L.get("import_kind") == KIND_OFFLINE_PAIR)
            log()
            log("  ⚠ 库里还有 %d 封【源文件里没有对应】的 —— 本工具不碰它们。" % len(unclaimed))
            if n_off:
                log("    其中 %d 封是「无时间」形态（走 letter_pairs.json 导入的）。" % n_off)
            log("    这多半是同一个 .soul 被导入过两次留下的重复（设计要点里记过这个坑：")
            log("      换一份内容不同的 letter_pairs.json，月离会当新信再导一遍）。")
            log("    想看是哪几封：菜单 5（并排对比，给他同一份 .soul），会逐封列出来。")
            log("    想把重复的那批从信箱里藏起来：菜单 8。")
        if hidden_unclaimed:
            log()
            log("  （另有 %d 封是本工具自己藏起来的——菜单 8 藏掉的重复。"
                "它们不算「多出来」，只是继续算作「库里已经有这封」。）"
                % len(hidden_unclaimed))
        if state_preview:
            log()
            log("  另外：state.json 里有 %d 组「同一分钟」的信顺序是乱的（靠随机 UUID 排的），"
                % len(state_preview))
            log("        将按来源文件的顺序重排（只动秒数，界面显示不变）：")
            log_order_preview(state_preview)
            log()
            log("        ↑ 看着不对的话先别继续，把这段发给帮忙的人核对。")

        shown = 0
        for item in to_fix + to_add:
            r = item["row"]
            t = (datetime.fromtimestamp(r["created_at"], TZ).strftime("%Y-%m-%d %H:%M")
                 if r["created_at"] else "时间未知")
            tag = ("修复(%s)" % "+".join(item["reasons"])) if "reasons" in item else "新增"
            line = "      %-12s %s  %s" % (tag, t, r["content"][:26].replace("\n", " "))
            if shown < 8:
                log(line)
            else:
                log_line(line)
            shown += 1
        if shown > 8:
            log("      …另有 %d 封（完整清单记在日志里）" % (shown - 8))
        log()

        # ── 报告到此结束。体检模式就停在这儿 ──
        if dry_run:
            log("=" * 62)
            log("  体检完毕：以上都是只读得出的。")
            log("  数据库一个字节没改，月离也没被关过。")
            log("  要真的动手，请回到菜单选 2。")
            log("=" * 62)
            log()
            con.close()
            return 0

        if not to_fix and not to_add:
            if state_plan:
                # 信不用动但 state.json 顺序要排：社区工具导过的常见这种情形
                log()
                log("  信不用动，只排 state.json 里的顺序。")
                apply_state_order(install, state_plan)
            else:
                log("  没有需要改动的信件，什么都不做。")
            con.close()
            start_yueli(install)
            log()
            log("  备份留着没动：%s" % bak.name)
            return 0

        # 必须显式开事务：sqlite3 默认只在 DML 前自动 BEGIN，而 DROP TRIGGER 是 DDL、
        #   不进自动事务，会被【立即提交】。万一在重建触发器前崩溃/被强杀，月离的只读
        #   保护就被永久拆掉、数据还没写进去。显式 BEGIN 后整批是一个原子动作。
        con.execute("BEGIN IMMEDIATE")
        try:
            # 照月离自己的 unload_legacy() 流程拆锁
            con.execute("DROP TRIGGER IF EXISTS legacy_letters_no_delete")
            con.execute("DROP TRIGGER IF EXISTS legacy_letters_no_update")

            now_imported = int(time.time())
            added = fixed = reordered = 0

            # ── 新增 ──
            for item in to_add:
                r = item["row"]
                rec_json = json.dumps(item["rec"], ensure_ascii=False, sort_keys=True)
                content_hash = hashlib.sha256(rec_json.encode("utf-8")).hexdigest()
                meta = {
                    "import_kind": KIND,
                    "backup_record": item["rec"],
                    "import_position": item["index"],
                    "user_content": r["content"],
                    "reply_text": r["reply_text"],
                    "replied_at": r["replied_at"],
                }
                try:
                    con.execute(
                        "INSERT INTO legacy_letters "
                        "(memory_id, source_record_id, source, occurred_at, content, content_hash, "
                        " imported_at, metadata_json, read_only) VALUES (?,?,?,?,?,?,?,?,1)",
                        (item["id"], "letter-backup:" + content_hash[:48], "letter-backup",
                         item["occ"], rec_json, content_hash, now_imported,
                         json.dumps(meta, ensure_ascii=False, sort_keys=True)))
                except sqlite3.IntegrityError:
                    continue      # content_hash UNIQUE 撞了 = 已存在，跳过
                # FTS 没有触发器，必须手动同步（列：memory_id UNINDEXED, content, source）
                con.execute("DELETE FROM legacy_letters_fts WHERE memory_id=?", (item["id"],))
                con.execute("INSERT INTO legacy_letters_fts VALUES (?,?,?)",
                            (item["id"], rec_json, "letter-backup"))
                added += 1

            # ── 修复（匹配到的）──
            # 会互换位置的那几封先挪到临时 id，再落最终 id（直接改会撞主键）。
            moves = {}
            for item in to_fix:
                # 只挪【同一分钟】那几封：改主键要连带同步 FTS，是有代价的
                if not item.get("reorder"):
                    continue
                old, new = item["hit"]["mid"], item["id"]
                moves[old] = new
                con.execute("UPDATE legacy_letters SET memory_id=? WHERE memory_id=?",
                            ("TMP" + new, old))
                con.execute("DELETE FROM legacy_letters_fts WHERE memory_id=?", (old,))

            for item in to_fix:
                hit = item["hit"]
                old, new = hit["mid"], item["id"]
                live = ("TMP" + new) if old in moves else old

                meta = dict(hit["meta"])
                # 菜单 8 藏起来的行：别把它弄回可见。隐藏标记就存在 import_kind 上，
                #   而下面这行赋值会覆盖它，于是修一次、重复就回来了。
                meta["import_kind"] = HIDDEN_KIND if HIDDEN_KIND_KEY in meta else KIND
                meta["backup_record"] = item["rec"]
                meta["import_position"] = item["index"]
                meta["replied_at"] = item["rec"].get("replied_at")
                if item.get("keep_library_text"):
                    # "按库"/"两个都保留"：库里的正文原样留着，只改时间和排序。
                    meta["user_content"] = hit.get("raw_content", "")
                    meta["reply_text"] = hit.get("raw_reply", "")
                elif hit.get("content_is_faked"):
                    # 一边有去信一边没有：以没有去信的那版为准，别把硬加那句写回去。
                    meta["user_content"] = ""
                    meta["reply_text"] = item["row"]["reply_text"]
                else:
                    meta["user_content"] = item["row"]["content"]
                    meta["reply_text"] = item["row"]["reply_text"]
                # 离线配对留下的两个键已经不成立了，清掉免得以后对不上
                meta.pop("offline_mailbox_publish_status", None)
                meta.pop("offline_letter_pair_provenance", None)

                # 档案时间已经是合法 ISO 就不动它（少改一点少一份风险）
                new_occ = hit["occurred_at"] if is_iso_stamp(hit["occurred_at"]) else item["occ"]
                con.execute(
                    "UPDATE legacy_letters SET metadata_json=?, occurred_at=? WHERE memory_id=?",
                    (json.dumps(meta, ensure_ascii=False, sort_keys=True), new_occ, live))

                if old in moves:
                    con.execute("UPDATE legacy_letters SET memory_id=? WHERE memory_id=?",
                                (new, live))
                    # FTS 的 content 列沿用原来那行的（正文列我们没动过）
                    con.execute("INSERT INTO legacy_letters_fts VALUES (?,?,?)",
                                (new, hit["raw_col"], hit["source"]))
                    reordered += 1
                fixed += 1

            con.execute(TRIG_UPDATE)
            con.execute(TRIG_DELETE)
            con.execute("COMMIT")
        except BaseException:
            # 回滚会把 DROP TRIGGER 一起撤销，触发器不会丢
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            log()
            log("  ✗ 写入失败，改动已全部回滚（数据库回到动手之前的样子，触发器完好）。")
            log("    月离现在是【关闭】状态，请手动重新打开它。")
            raise

        total = con.execute("SELECT COUNT(*) FROM legacy_letters").fetchone()[0]
        log("  完成：修复 %d 封（其中重排顺序 %d 封），新增 %d 封，库内共 %d 封"
            % (fixed, reordered, added, total))
        trg = [x[0] for x in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='legacy_letters'")]
        log("  触发器已重建: %s" % ", ".join(sorted(trg)))

        if state_plan:
            log()
            apply_state_order(install, state_plan)
    finally:
        con.close()

    log()
    start_yueli(install)
    log()
    log("  完成。等月离起来（约 1 分钟）后看看信箱。")
    log("  要回滚：把 %s 覆盖回" % bak.name)
    log("          %s" % db)
    log()
    return 0


# ──────────────────── 僵尸信（发送失败的信）────────────────────

# 判据抄月离自己的（local_server.py:1719：failed = letter_status in {...}）
ZOMBIE_STATUSES = ("FAILED", "CANCELED", "CANCELLED")
ZOMBIE_MARK = "tool-zombie-clean"      # 隐藏时打的值；月离只认"有没有值"


def _load_state_raw(install: Path):
    """读 state.json 的【原始文本】+ 解析结果；读不了返回 (None, None)。
    原始文本要留着：写完读回来校验时，得跟动手前的文本逐字节比。
    """
    p = install / "install" / "data" / "state.json"
    try:
        txt = p.read_text(encoding="utf-8")
        return txt, json.loads(txt)
    except Exception:
        return None, None


def _zombies_of(data: dict):
    """state.json 里的失败信，就是信箱里那张"寄信通道好像有点忙"的卡片。"""
    ls = [x for x in (data.get("letters") or []) if isinstance(x, dict)]
    return [x for x in ls if x.get("letter_status") in ZOMBIE_STATUSES]


def _zombie_lines(zs):
    out = []
    for x in zs:
        ca = x.get("created_at")
        t = (datetime.fromtimestamp(ca, TZ).strftime("%Y-%m-%d %H:%M")
             if isinstance(ca, (int, float)) and not isinstance(ca, bool) else "时间未知")
        mark = "   ← 月离已经自己藏起来了（界面上看不到）" if x.get("superseded_by") else ""
        out.append("     %s  「%s」%s" % (t, _peek(x.get("content"), 34), mark))
    return out


def _letter_in_archive(install: Path, letter_id: str):
    """这封信在记忆档案里有没有条目？有就不能删，会造出孤儿。
    档案 source 的格式是 `reply:<letter_id>:<revision>`（original-text-index.sqlite3
    的 originals 表）。返回值 True / False / None（查不了，调用方应当保守处理）。
    """
    p = install / "install" / "data" / "memory" / "mem0" / "original-text-index.sqlite3"
    if not p.is_file():
        return False            # 连档案都没有，不可能有孤儿
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.as_posix(), uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM originals WHERE source LIKE ?",
                            ("reply:" + letter_id + ":%",)).fetchone()[0]
            return n > 0
        finally:
            con.close()
    except Exception:
        return None


def save_state_verified(install: Path, data, before_text, todo, mode, marker, tag) -> bool:
    """把 state.json 的改动落盘，并验证落盘结果正好是预期的，返回 True/False。
    mode "hide" 打上 marker，mode "delete" 移除那几行；流程是备份、原子写、读回校验、
    不对就用备份还原。安全关键的落盘只有这一个实现：菜单 6 和"藏社区工具那批"共用。
    """
    state_path = install / "install" / "data" / "state.json"
    bakdir = state_path.parent / "memory" / "_backups"
    bakdir.mkdir(parents=True, exist_ok=True)
    bak = bakdir / ("%s-%s.json" % (tag, datetime.now().strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(state_path, bak)
    log("  已备份 -> %s" % bak.name)

    letters = data.get("letters") or []
    # 校验按【下标】比：before 是重新 loads 出来的另一批对象，按对象身份永远比不中。
    todo_ids = {id(x) for x in todo}
    todo_idx = {i for i, x in enumerate(letters) if id(x) in todo_ids}

    if mode == "hide":
        for x in todo:
            x["superseded_by"] = marker
        log("  隐藏 %d 封（只打一个 superseded_by 标记，内容一个字节没动）" % len(todo))
        note_ok, note_bad = "封数未变，其余逐字节一致", "（除了那几处 superseded_by）"
    else:
        data["letters"] = [x for x in letters if id(x) not in todo_ids]
        log("  删除 %d 封（%d -> %d）" % (len(todo), len(letters), len(data["letters"])))
        note_ok, note_bad = "只少了几封信", "（除了少掉的那几封）"

    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state_path)

    want = json.loads(before_text)
    wl = want.get("letters") or []
    if mode == "hide":
        for i in todo_idx:
            if i < len(wl) and isinstance(wl[i], dict):
                wl[i]["superseded_by"] = marker
    else:
        want["letters"] = [x for i, x in enumerate(wl) if i not in todo_idx]

    problems = []
    try:
        after = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        after = None
        problems.append("读不回来：%s" % e)
    if after is not None and json.dumps(after, ensure_ascii=False, indent=2) != json.dumps(
            want, ensure_ascii=False, indent=2):
        problems.append("写回的内容和预期不一致" + note_bad)

    if problems:
        shutil.copy2(bak, state_path)
        log("  ✗ 校验没过：%s" % "；".join(problems))
        log("  → 已用备份还原：%s" % bak)
        return False
    log("  校验通过 ✓（%s）" % note_ok)
    log_line("  备份：%s" % bak)
    return True


def cmd_zombies(install_arg, mode=None) -> int:
    """检测并清理"发送失败"的信（信箱里那张"寄信通道有点忙"）。hide 打上
    superseded_by（月离自己就用它藏信，可逆）；delete 从 state.json 移除（删前逐封
    查记忆档案，有挂靠的跳过）。
    """
    log()
    log("=" * 62)
    log("  僵尸信：检测并清理「发送失败」的信")
    log("=" * 62)
    install = find_install(install_arg)
    state_path = install / "install" / "data" / "state.json"
    log("  月离: %s" % install)
    log("  文件: %s" % state_path)
    log()

    before_text, data = _load_state_raw(install)
    if data is None:
        log("  ✗ 读不了 state.json（%s）" % state_path)
        return 1

    zs = _zombies_of(data)
    log("  发送失败的信一共 %d 封" % len(zs))
    for ln in _zombie_lines(zs):
        log(ln)
    n_hidden = sum(1 for x in zs if x.get("superseded_by"))
    if n_hidden:
        log("     其中 %d 封月离已经自己藏了（清不清都行）" % n_hidden)
    log()
    if not zs:
        log("  没有需要清理的，信箱里那些「寄信通道好像有点忙」都没有。")
        log("=" * 62)
        log()
        return 0

    if mode not in ("hide", "delete"):
        log("  怎么清？")
        log("     1) 隐藏，打上 superseded_by，界面不再显示；一个字都不删（可逆）")
        log("     2) 删除，从 state.json 里移除（不可逆，有备份；有档案关联的跳过）")
        log()
        try:
            if not sys.stdin.isatty():
                raise SystemExit("  ✗ 需要你选隐藏还是删除，但当前不是交互式。\n"
                                 "    请加 --mode hide 或 --mode delete。")
            c = input("  选 [1/2]（直接回车 = 取消）：").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("  没有选择，退出（什么都没改）。")
        mode = {"1": "hide", "2": "delete"}.get(c)
        if mode is None:
            raise SystemExit("  没有选择，退出（什么都没改）。")

    skipped = []
    if mode == "delete":
        for x in zs:
            lid = str(x.get("letter_id") or "")
            if not lid:
                skipped.append((x, "没有 letter_id"))
                continue
            has = _letter_in_archive(install, lid)
            if has is True:
                skipped.append((x, "记忆档案里有 reply:%s 的条目" % lid))
            elif has is None:
                skipped.append((x, "档案查不了（保守起见不删）"))
        if skipped:
            log("  下面这几封不动：")
            for x, why in skipped:
                log("     [跳过] 「%s」，%s" % (_peek(x.get("content"), 24), why))
            log()

    skip_ids = {id(x) for x, _ in skipped}

    # ── 先算清楚"这次到底要动哪些"。没得动就【不关月离、不写文件】：
    #   否则明明一封都不用动，照样关月离、写文件，还白重启一次。
    if mode == "hide":
        todo = [x for x in zs if id(x) not in skip_ids and not x.get("superseded_by")]
    else:
        todo = [x for x in zs if id(x) not in skip_ids]
    if not todo:
        log("  没有需要动的，%s"
            % ("它们都已经藏过了（月离界面上本来就不显示）。" if mode == "hide"
               else "全都被跳过了。"))
        log("=" * 62)
        log()
        return 0

    log("  关闭月离…")
    stop_yueli(install)

    if not save_state_verified(install, data, before_text, todo, mode,
                               ZOMBIE_MARK, "zombie-clean"):
        start_yueli(install)
        log()
        return 1
    start_yueli(install)
    log()
    log("=" * 62)
    log("  以上都做完了。界面要重开月离才看得到（约 1 分钟）。")
    log("=" * 62)
    log()
    return 0


# ────────────── 回退月离的版本（菜单 7 / `rollback`）──────────────
# 月离的后端是按版本目录管理的：install\.olivia-update-state.json 里的
# active_components / previous_components 各指一份 versions\local_backend\<版本>-<sha256>，
# launcher\version_launcher.py 只读这一个指针文件决定跑哪一份。所以"回退版本"就是让
# 客户端自带的 rollback-update 换一下这两个指针（原子、可逆，payload / data / profile 不碰）。
# 更新补丁之后游戏打不开（窗口不出来、进程却在跑）时用它；客户端那套补丁住在
# payload 里、每次启动现打，所以回退版本会连补丁代码一起回退。

RB_STATE_NAME = ".olivia-update-state.json"
RB_STATE_SCHEMA = "olivia.update-state.v1"


def read_update_state(install_dir: Path):
    """读 .olivia-update-state.json，返回 (state, 错误信息)。"""
    p = install_dir / RB_STATE_NAME
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, "读不了 %s（%s）" % (p, exc)
    except (UnicodeError, json.JSONDecodeError) as exc:
        return None, "%s 不是合法的 json（%s）" % (p, exc)
    if not isinstance(state, dict) or state.get("schema_version") != RB_STATE_SCHEMA:
        return None, "%s 的 schema_version 不是 %s" % (p, RB_STATE_SCHEMA)
    if set(state) != {"schema_version", "active_components", "previous_components"}:
        return None, "%s 的字段和预期不符（多或少）" % p
    return state, None


def _rb_component(state: dict, which: str):
    """取 active_components / previous_components 里的 local_backend 那一块。"""
    sec = state.get(which)
    if not isinstance(sec, dict):
        return None
    comp = sec.get("local_backend")
    if not isinstance(comp, dict):
        return None
    if set(comp) != {"version", "manifest_sha256", "payload_path"}:
        return None
    return comp


def _rb_payload_dir(install_dir: Path, comp: dict) -> Path:
    """payload_path 是 posix 风格的相对路径：versions/local_backend/<版本>-<sha256>。"""
    return install_dir / str(comp["payload_path"]).replace("/", os.sep)


def _rb_code_payload(install_dir: Path, active):
    """决定用哪份 payload 里客户端自带的代码执行回退。优先用【当前 active 那份】；它缺
    installer 的话，就在 versions 里找任意一份有 installer\\__main__.py 的。
    """
    cands = []
    if active is not None:
        cands.append(_rb_payload_dir(install_dir, active))
    base = install_dir / "versions" / "local_backend"
    if base.is_dir():
        cands += sorted([d for d in base.iterdir() if d.is_dir()])
    for d in cands:
        if (d / "installer" / "__main__.py").is_file():
            return d
    return None


# 干活的子进程脚本。走 stdin 交给 `python -`，不用拼很长的 -c 字符串；里面【全部复用
# 客户端自带的那些东西】（校验 _validate_installation 等，落盘 _write_state + os.replace）。
RB_CHILD_SCRIPT = r'''
import json, os, shutil, sys, uuid
from pathlib import Path

payload, install, action = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, payload)

if action == "rollback":
    import runpy
    sys.argv = ["olivia-full-patch", "rollback-update", "--installation", install]
    runpy.run_module("installer", run_name="__main__")
    raise SystemExit(0)

from installer.component_update import (
    STAGING_ROOT, STATE_NAME, _read_state, _resolve_state_payload,
    _validate_installation, _validate_state_descriptor, _write_state)

version, sha, rel = sys.argv[4], sys.argv[5], sys.argv[6]
component = "local_backend"
root = _validate_installation(Path(install))
chosen = _validate_state_descriptor(
    component,
    {"version": version, "manifest_sha256": sha, "payload_path": rel},
)
_resolve_state_payload(root, component, chosen)
state_path = root / STATE_NAME
state = _read_state(state_path)
active = state["active_components"][component]
if active == chosen:
    print(json.dumps({"status": "ALREADY_ACTIVE", "version": version}, sort_keys=True))
    raise SystemExit(0)
next_state = {
    "schema_version": state["schema_version"],
    "active_components": {**state["active_components"], component: chosen},
    "previous_components": {component: active},
}
staging = root / STAGING_ROOT / uuid.uuid4().hex
try:
    staging.mkdir(parents=True)
    staged = staging / "switch-state.json"
    _write_state(staged, next_state)
    os.replace(staged, state_path)
finally:
    shutil.rmtree(staging, ignore_errors=True)
print(json.dumps({"status": "SWITCHED", "version": version}, sort_keys=True))
'''

_RB_VER_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_RB_SHA_RE = re.compile(r"[0-9a-f]{64}")


def rb_list_payloads(install_dir: Path):
    """列出 versions\\local_backend 下现存的 payload -> [(描述, 目录, 完整吗, mtime)]。
    描述里的 version / manifest_sha256 **必须和目录名严格对上**：客户端要求 payload_path
    正好是 "versions/local_backend/<版本>-<sha256>"，所以目录名是唯一依据。版本号本身
    可以带 '-'，要从**末尾**切 64 位 sha，不能从第一个 '-' 处切。
    """
    out, base = [], install_dir / "versions" / "local_backend"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        name = d.name
        if not d.is_dir() or len(name) < 67 or name[-65] != "-":
            continue
        ver, sha = name[:-65], name[-64:]
        if not _RB_SHA_RE.fullmatch(sha) or not _RB_VER_RE.fullmatch(ver):
            continue
        comp = {"version": ver, "manifest_sha256": sha,
                "payload_path": "versions/local_backend/%s-%s" % (ver, sha)}
        try:
            mtime = d.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append((comp, d, _rb_payload_complete(d), mtime))
    return out


def _rb_payload_complete(d: Path) -> bool:
    """这份 payload 看起来完整吗？缺东西的话切过去游戏也起不来，所以列出来时要标一眼。"""
    return ((d / "installer" / "start_local.py").is_file()
            and (d / "installer" / "__main__.py").is_file()
            and (d / "version.json").is_file())


def _rb_backup_state(install_dir: Path):
    """把状态文件备份到 data\\memory\\_backups\\（和别的操作放同一个地方）。"""
    bakdir = install_dir / "data" / "memory" / "_backups"
    try:
        bakdir.mkdir(parents=True, exist_ok=True)
        bak = bakdir / ("update-state-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        shutil.copy2(install_dir / RB_STATE_NAME, bak)
        return bak
    except OSError:
        return None


def _rb_run_child(install_dir: Path, payload: Path, action: str, chosen=None) -> int:
    """活全扔给子进程干，返回它的退出码。
    不扔给本进程 import：payload 根目录既放 `installer` 包、也放 `patch_*.py` 那堆顶层
      模块，插进本进程的 sys.path 会挡住之后才 import 的东西；客户端也是这么 spawn 的。
    不能写 `-m installer`：月离自带的嵌入式 Python 有 python312._pth，屏蔽 PYTHONPATH、
      也不把 cwd 放进 sys.path，必须像客户端那样手动 sys.path.insert()。
    """
    args = [sys.executable, "-", str(payload), str(install_dir), action]
    if chosen is not None:
        args += [chosen["version"], chosen["manifest_sha256"], chosen["payload_path"]]
    try:
        done = subprocess.run(args, input=RB_CHILD_SCRIPT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
    except Exception as exc:
        log("  ✗ 起不了子进程：%s: %s" % (type(exc).__name__, exc))
        return 1
    for ln in (done.stdout or "").splitlines():
        if ln.strip():
            log("    %s" % ln)
    errs = [ln for ln in (done.stderr or "").splitlines() if ln.strip()]
    for ln in errs:
        log_line("    stderr: %s" % ln)      # 完整堆栈进日志
    if done.returncode != 0 and errs:
        # 出错时把最后几行也摆到台面上，不然用户手上没有任何可查的东西。
        log("    （子进程的报错，完整堆栈在日志里）")
        for ln in errs[-4:]:
            log("      %s" % ln)
    return done.returncode


def cmd_rollback(install_arg, yes=False, to=None) -> int:
    """把月离的后端版本退回去。【会关月离】
    两条路：退回上一版走客户端自带的 rollback-update（原子交换 active/previous，最稳，且
      【自反】：再跑一次就回到原来的版本）；退到指定版本它不支持，所以自己写状态
      文件（复用客户端自带那套校验和落盘），动手前备份、写完立刻校验。
    两条路都只改 .olivia-update-state.json 一个文件：data\\（你的信、记忆、关系）和
    profile\\ 一个字节都不碰。
    """
    log()
    log("=" * 62)
    log("  回退月离的版本（游戏打不开时用）")
    log("=" * 62)
    home = find_install(install_arg)          # 这里拿到的是【月离根目录】
    install_dir = home / "install"            # 状态文件和 payload 在它下面的 install 里
    log("  月离: %s" % home)
    log("  状态文件: %s" % (install_dir / RB_STATE_NAME))
    log()

    state, err = read_update_state(install_dir)
    if state is None:
        log("  ✗ %s" % err)
        log("    这个目录可能不是走「完整补丁」装的（那就没有版本可回退）。")
        return 1
    active = _rb_component(state, "active_components")
    previous = _rb_component(state, "previous_components")
    if active is None:
        log("  ✗ 状态文件里 active_components.local_backend 读不出来，不敢动它。")
        return 1
    cands = []
    for comp, d, ok, mt in rb_list_payloads(install_dir):
        if comp == active:
            continue
        cands.append({"comp": comp, "dir": d, "ok": ok, "mtime": mt,
                      "is_prev": previous is not None and comp == previous})
    # "上一版"永远排第一（客户端自带那条路最稳），其余按时间从新到旧
    cands.sort(key=lambda c: (not c["is_prev"], -c["mtime"]))
    if not cands:
        log("  这台机器上除了现在跑的这一版，没有别的 payload 可退。")
        if previous is not None:
            log("    （状态文件里记着上一版是 %s，但那份 payload 已经不在了）"
                % previous["version"])
        log("    只能重装一份旧补丁包。")
        return 1

    log("  现在跑的是 : local_backend %s" % active["version"])
    log("  这台机器上还能退到：")
    for i, c in enumerate(cands, 1):
        if not c["ok"]:
            tag = "   ⚠ 这份不完整，退过去游戏也起不来"
        elif c["is_prev"]:
            tag = "   ← 上一版（走官方那条路）"
        else:
            tag = ""
        log("     %d) %-12s %s%s"
            % (i, c["comp"]["version"],
               datetime.fromtimestamp(c["mtime"], TZ).strftime("%Y-%m-%d %H:%M"), tag))
    log("     0) 算了，不改")
    log()
    log("  只改 .olivia-update-state.json 一个文件；你的信件、记忆、关系（data\\）")
    log("  和 profile\\ 一个字节都不碰。")
    log()

    if to:                               # 指名道姓的优先，别被 --yes 吃掉
        hits = [c for c in cands if c["comp"]["version"] == to]
        if not hits:
            log("  ✗ 盘上没有 %s 这一版。现在能退的是：%s"
                % (to, "、".join(c["comp"]["version"] for c in cands)))
            return 1
        pick = hits[0]
    elif yes:
        pick = cands[0]                      # --yes = 取第一项（上一版，若在的话）
    else:
        try:
            if not sys.stdin.isatty():
                raise SystemExit("  ✗ 需要你选退到哪一版。非交互时用 --yes（退上一版）"
                                 "或 --to <版本>。")
            raw = input("  退到哪一版？输入序号（直接回车 = 取消）：").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("  没有选择，退出（什么都没改）。")
        if not raw:
            raise SystemExit("  没有选择，退出（什么都没改）。")
        if not raw.isdigit() or not (1 <= int(raw) <= len(cands)):
            raise SystemExit("  没有这个序号，退出（什么都没改）。")
        pick = cands[int(raw) - 1]

    if not pick["ok"]:
        log("  ✗ %s 这份 payload 不完整（缺 installer\\start_local.py、"
            "installer\\__main__.py 或 version.json），退过去游戏也起不来，所以不动它。"
            % pick["comp"]["version"])
        return 1

    log()
    log("  退到 : local_backend %s" % pick["comp"]["version"])
    log("    从 : local_backend %s" % active["version"])
    if pick["is_prev"]:
        log("    走官方 rollback-update（原子交换 active/previous），")
        log("    而且它是【自反】的：再跑一次就回到 %s。" % active["version"])
    else:
        log("    这条路官方不支持（官方只认「退上一版」），改的是同一个文件：")
        log("    动手前先把状态文件备份到 data\\memory\\_backups\\，写完立刻校验。")
        log("    退过去之后「%s」会占住 previous 槽，所以还能一键回到它。" % active["version"])
    log()

    if not yes:
        try:
            if not sys.stdin.isatty():
                raise SystemExit("  ✗ 这个操作要改月离的版本。非交互时请加 --yes 确认。")
            answer = input("  确认吗？输入 yes 继续（直接回车 = 取消）：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("  没有确认，退出（什么都没改）。")
        if answer != "yes":
            raise SystemExit("  没有确认，退出（什么都没改）。")
        log()

    code_payload = _rb_code_payload(install_dir, active)
    if code_payload is None:
        log("  ✗ versions\\local_backend 下找不到带 installer 的 payload，没法执行。")
        return 1

    # 先动手、再停进程：万一失败，月离还开着，不至于白关一次
    if pick["is_prev"]:
        log("  正在回退（子进程里跑官方 rollback-update）…")
        rc = _rb_run_child(install_dir, code_payload, "rollback")
    else:
        log("  正在切换版本（子进程里写状态文件，动手前先备份）…")
        bak = _rb_backup_state(install_dir)
        if bak is not None:
            log("    状态文件已备份 -> %s" % bak.name)
        rc = _rb_run_child(install_dir, code_payload, "switch", pick["comp"])
        if rc != 0 and bak is not None:
            try:
                shutil.copy2(bak, install_dir / RB_STATE_NAME)
                log("    已用备份把状态文件还原")
            except OSError as exc:
                log("    ✗ 还原也失败了：%s" % exc)
                log("      请手动把 %s 覆盖回" % bak)
                log("      %s" % (install_dir / RB_STATE_NAME))
    log()
    if rc != 0:
        log("  ✗ 没成功（上面那行 json / 报错里有原因）。常见成因：")
        log("      UPDATE_ROLLBACK_UNAVAILABLE   没有上一版可退回（或状态文件里没记）")
        log("      UPDATE_INSTALLATION_INVALID   目录不像完整的月离安装（标记文件对不上）")
        log("      UPDATE_ACTIVATION_FAILED      写状态文件失败（权限、杀软拦截）")
        log("      UPDATE_STATE_INVALID          状态文件本身不合法")
        log("    月离应该还是原来的版本。")
        return rc

    after, err2 = read_update_state(install_dir)
    if after is None:
        log("  ⚠ 回退命令成功了，但回读状态文件失败：%s" % err2)
        log("    请把上面的输出发出来。")
        return 1
    new_active = _rb_component(after, "active_components")
    new_prev = _rb_component(after, "previous_components")
    log("  状态文件现在是：")
    log("     active   = %s" % (new_active["version"] if new_active else "（读不出来）"))
    log("     previous = %s" % (new_prev["version"] if new_prev else "（读不出来）"))
    log()
    log("  也就是说：月离下次启动会用 %s。" % (new_active["version"] if new_active else "新版本"))
    log()
    if new_active is None or new_active["version"] != pick["comp"]["version"]:
        log("  ⚠ 状态文件里的 active 不是你选的那一版。先别启动月离，")
        log("    把这个输出发出来看一眼。")
        return 1

    log("  关闭月离…")
    stop_yueli(home)
    log()
    log("=" * 62)
    log("  回退做完了。")
    log("=" * 62)
    log()
    start_yueli(home)
    log()
    return 0


# 社区工具导进 state.json 的信打这个标记藏起来（填哨兵值，那个真 id 我们算不到）。
HIDDEN_MARK = "tool-hidden-import"


def cmd_hide_imported(install_arg, yes=False) -> int:
    """把【社区工具导进 state.json 的那批信】藏起来。【会关月离】
    那批信在信箱里看得见，但记忆档案里一条都没有、林离对它们一无所知；想让林离真
      记得得走月离自己的导入。可月离的离线导入【只查 legacy_letters、不查 state.json】，
      会把同一批信再导一遍、信箱里就成了两份，所以顺序是【先导、再藏】：
        ① 月离导入  ② 跑这里藏旧的那份  ③ 菜单 2 修时间顺序
    两边都不丢：正文一个字节没删，去掉标记就回来。
    """
    log()
    log("=" * 62)
    log("  隐藏社区工具导入的那批信（让月离的导入接管）")
    log("=" * 62)
    install = find_install(install_arg)
    state_path = install / "install" / "data" / "state.json"
    log("  月离: %s" % install)
    log("  文件: %s" % state_path)
    log()

    before_text, data = _load_state_raw(install)
    if data is None:
        log("  ✗ 读不了 state.json（%s）" % state_path)
        return 1

    letters = data.get("letters") or []
    todo = [x for x in letters if isinstance(x, dict)
            and is_external_import(x) and not x.get("superseded_by")]
    log("  state.json 里 %d 封，其中社区工具导入的 %d 封" % (len(letters), len(todo)))
    for x in todo[:10]:
        ca = x.get("created_at")
        t = (datetime.fromtimestamp(ca, TZ).strftime("%Y-%m-%d %H:%M")
             if isinstance(ca, (int, float)) and not isinstance(ca, bool) else "时间未知")
        log("     %s  「%s」" % (t, _peek(x.get("content"), 30)))
    if len(todo) > 10:
        log("     …另有 %d 封" % (len(todo) - 10))
    log()

    if not todo:
        log("  没有需要藏的，要么没有社区工具导入的信，要么都藏过了。")
        log("=" * 62)
        log()
        return 0

    log("  藏起来之后：")
    log("     · 界面上不再显示这些信，总数少 %d" % len(todo))
    log("     · 正文一个字节不删，去掉标记就回来")
    log("     · 记忆档案和关系判定不受影响（本来就查不到这些信）")
    log()
    log("  前提是你已经跑过月离自己的导入了（说明书第五节），")
    log("  否则藏起来之后就找不到这些信了。")
    log()
    if not yes:
        try:
            if not sys.stdin.isatty():
                raise SystemExit("  ✗ 这个操作要改 state.json。非交互时请加 --yes 确认。")
            c = input("  确定要藏吗？输入 yes 继续（直接回车 = 取消）：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("  没有确认，退出（什么都没改）。")
        if c != "yes":
            raise SystemExit("  没有确认，退出（什么都没改）。")

    log("  关闭月离…")
    stop_yueli(install)
    if not save_state_verified(install, data, before_text, todo, "hide",
                               HIDDEN_MARK, "hide-imported"):
        start_yueli(install)
        log()
        return 1
    start_yueli(install)
    log()
    log("=" * 62)
    log("  藏好了。界面要重开月离才看得到（约 1 分钟）。")
    log("=" * 62)
    log()
    return 0


# ───────────── 藏起 legacy 里重复的旧信（菜单 8 / `hide-dupes`）─────────────
# 重复的来源：月离自己那条 letter_pairs.json 导入路径**不做去重**，换一份内容不同
#   的文件就当新信再导一遍。那批新行是 ①型（created_at 被强制 None，信箱里"时间
#   未知"），而且**不归菜单 2 管**（菜单 2 只处理"源文件里有的"）。
# 能藏的原理：月离的信箱投影只认三类 import_kind（那个 *_mailbox_projection 函数的白名单：
#   local_letter_backup_v1 / offline_recovered_text_reply / 第三个见下方代码里的字面量），
#   挪出这三类就不进信箱；正文、payload、记忆档案一个字节不碰，而且可逆。
KIND_OFFLINE_PAIR = "offline_recovered_text_reply"
HIDDEN_KIND = "tool-hidden-legacy"          # 挪出白名单用的哨兵值
HIDDEN_KIND_KEY = "tool_hidden_import_kind"  # 原值存这儿，还原时写回去
HIDDEN_AT_KEY = "tool_hidden_at"


def kind_in_mailbox(meta) -> bool:
    """这行的形态在不在月离那三类白名单里（= 信箱里显示不显示）。
    只按 import_kind 判：三个判定函数（is_letter_backup 等）都要求 import_kind 正好
      等于各自那一个值，所以换成别的值一定三类都不沾。
    """
    return (meta or {}).get("import_kind") in (
        KIND, KIND_OFFLINE_PAIR, "official_text_reply")


def archive_texts(install: Path):
    """记忆档案里所有条目的正文（归一化后），用来回答"这封在档案里有没有条目"。
    档案 = `original-text-index.sqlite3` 的 `originals` 表（列 user/source/actor/stamp/text）。
    **不按档案里 `…:offline:<sha256>` 那条哈希查**：那个哈希不是对正文直接算的，试过
      八种算法全部 0 命中，而 `originals.text` 存着正文本身，按内容查又稳又简单。
    档案不存在（没跑过月离自己的导入）返回 None；**调用方必须把 None 当成"查不出来"**。
    """
    p = install / "install" / "data" / "memory" / "mem0" / "original-text-index.sqlite3"
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.as_posix(), uri=True)
        try:
            return {norm_text(t) for (t,) in con.execute("SELECT text FROM originals")}
        finally:
            con.close()
    except Exception:
        return None


def _has_memory(L, texts) -> bool:
    if not texts:
        return False
    return (norm_text(L["content"]) in texts) or (norm_text(L["reply"]) in texts)


def _keep_score(L) -> int:
    """这一行有多"该留"。
    **①型（import_kind = offline_recovered_text_reply）压倒性优先**：这个形态是
      "月离自己那条离线导入留下的"标志，也就是**带着林离记忆的那份**（档案里的
      `…:offline:*` 那批就是那条路径写的），规矩是"留月离导入的那份"。
      工具修过的那批会变成 ②③型，所以这个形态本身就能把它们区分开。
    """
    meta = L.get("meta") or {}
    ca = L.get("backup_created_at")
    score = 0
    if L.get("import_kind") == KIND_OFFLINE_PAIR:
        score += 100                     # 月离自己导入留下的那份（有记忆）
    if isinstance(ca, (int, float)) and not isinstance(ca, bool):
        score += 4                       # 时间是数字（信箱里显示正常）
    if kind_in_mailbox(meta):
        score += 1                       # 确实在信箱里显示
    return score


def _best_time(rows):
    """一组重复行里最可信的那个时间（epoch 秒），没有就返回 None。
    ②③型里的时间是菜单 2 从 .soul 写进去的，最可信；其次任何数字时间。
    """
    fallback = None
    for L in rows:
        ca = L.get("backup_created_at")
        if isinstance(ca, (int, float)) and not isinstance(ca, bool):
            if L.get("import_kind") == KIND:
                return int(ca)
            if fallback is None:
                fallback = int(ca)
    return fallback


def _num(v):
    """排序用：把可能是 int/float/None/字符串的东西安全地变成一个数。"""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return 0.0


def _has_time(L) -> bool:
    ca = L.get("backup_created_at")
    return isinstance(ca, (int, float)) and not isinstance(ca, bool)


def _row_desc(L) -> str:
    """一行的描述（对比时用）：入库时间 · 显示时间 · 形态。"""
    n = _num(L.get("imported_at"))
    imp = datetime.fromtimestamp(n, TZ).strftime("%Y-%m-%d %H:%M") if n else "入库时间未知"
    t = (datetime.fromtimestamp(L["backup_created_at"], TZ).strftime("%Y-%m-%d %H:%M")
         if _has_time(L) else "时间未知")
    kind = (L.get("meta") or {}).get("import_kind") or "（没有 import_kind）"
    return "入库 %s · 显示 %s · 形态 %s" % (imp, t, kind)


def _compare_rows(older, newer):
    """摆出这两份的区别，给人拍板用。两份是按"逐字相同（忽略空白）"配到一起的，
    所以能看出的只有：时间有没有、形态是什么、**正文的空白（换行）全不全**。
    """
    out = []
    for label, a, b in (("去信", older.get("raw_content"), newer.get("raw_content")),
                        ("回信", older.get("raw_reply"), newer.get("raw_reply"))):
        sa, sb = str(a or ""), str(b or "")
        if sa == sb:
            out.append("%s：两份逐字一样" % label)
        elif norm_text(sa) == norm_text(sb):
            na, nb = sa.count("\n"), sb.count("\n")
            if na == nb:
                out.append("%s：只差空白（换行数一样，都是 %d 个）" % (label, na))
            else:
                who = "旧的那份" if na > nb else "新的那份"
                out.append("%s：只差空白，%s的换行更全（%d 个 vs %d 个）"
                           % (label, who, max(na, nb), min(na, nb)))
        else:
            out.append("%s：连非空白字符都不一样（按理不该出现，发出来看看）" % label)
    return out


def dupe_plan(lib, texts=None, keep=None):
    """找出 legacy_letters 里【逐字重复】的组，决定每组留哪一行。判重口径和菜单 2 /
    菜单 5 完全一致（去信+回信，忽略空白）；月离那条导入路径不做去重，由工具补。
    keep = None / "new" / "old"："new" 留后入库的那份，"old" 留先入库的那份；None 走
      自动：①型（见 `_keep_score`）优先，组里没有才回落"有时间、形态正常的那份"。
      新旧按 `imported_at` 排（取不到就按 SQL 顺序 = 入库顺序）。
    **一条走不通的路，别再试**：按"记忆档案里有没有这一封"判留哪份不行。记忆按
      【内容】存，而同一组的行内容逐字相同，查询结果对同组每一行必然一样。
    留下的那份要是没数字时间，就从同组另一份借过来补上（见 fixes）。返回 (要藏的行,
      要补时间的 [(行, epoch)], 预览)；texts 为 None 表示查不出来，别当成"没记忆"。
    """
    groups = {}
    for L in lib:
        if L.get("store") != "legacy":
            continue
        k = (norm_text(L["content"]), norm_text(L["reply"]))
        if not k[0] and not k[1]:
            continue                    # 两边都空的行不参与（判重也认不出它们）
        groups.setdefault(k, []).append(L)

    hide, fixes, preview = [], [], []
    for k, rows in groups.items():
        if len(rows) < 2:
            continue
        order = sorted(range(len(rows)), key=lambda i: _num(rows[i].get("imported_at")))
        older, newer = rows[order[0]], rows[order[-1]]

        # 只做报告用：这一组内容逐字相同，所以 mem 必然全 True 或全 False（见 docstring）
        mem = [_has_memory(L, texts) for L in rows]
        n_mem = sum(mem)

        if keep == "new":
            ki, why = order[-1], "你选的：留新的那份（后入库的）"
        elif keep == "old":
            ki, why = order[0], "你选的：留旧的那份（先入库的）"
        else:
            ki = max(range(len(rows)), key=lambda i: _keep_score(rows[i]))
            why = ("①型（月离自己导入留下的那份，带着记忆）"
                   if rows[ki].get("import_kind") == KIND_OFFLINE_PAIR
                   else "有时间、形态正常的那份")
        keeper = rows[ki]
        drop = [L for i, L in enumerate(rows) if i != ki]

        fix = None
        if not _has_time(keeper):
            t = _best_time(drop)        # 留下的那份没时间，从同组另一份借
            if t is not None:
                fix = (keeper, t)
                fixes.append(fix)
        preview.append({"keep": keeper, "drop": drop, "n": len(rows), "n_mem": n_mem,
                        "keep_has_mem": mem[ki], "fix": fix, "unknown": texts is None,
                        "why": why, "older": older, "newer": newer,
                        "cmp": _compare_rows(older, newer),
                        "keep_is_new": (ki == order[-1])})
        hide += drop
    return hide, fixes, preview


def _hide_meta(meta):
    m = dict(meta)
    if m.get("import_kind") is not None:
        m.setdefault(HIDDEN_KIND_KEY, m.get("import_kind"))
    m["import_kind"] = HIDDEN_KIND
    m[HIDDEN_AT_KEY] = int(time.time())
    return m


def _unhide_meta(meta):
    """还原。不是我们藏的（没有那个键）就返回 None，绝不乱动别人的行。"""
    m = dict(meta)
    old = m.pop(HIDDEN_KIND_KEY, None)
    if old is None:
        return None
    m["import_kind"] = old
    m.pop(HIDDEN_AT_KEY, None)
    return m


def _meta_with_time(meta, epoch):
    """把时间写进 metadata（backup_record.created_at 要**数字**，调用方同时改
    occurred_at 列）。①型光改时间字段没用（投影函数会强制 None），所以顺带把
    import_kind 一起挪进白名单；补时间只针对"留下的那封"，它本来就该能显示。
    """
    m = dict(meta)
    br = m.get("backup_record")
    br = dict(br) if isinstance(br, dict) else {}
    br["created_at"] = int(epoch)
    m["backup_record"] = br
    if m.get("import_kind") == KIND_OFFLINE_PAIR:
        m["import_kind"] = KIND             # 顺带挪进白名单，否则时间写了也不显示
    return m


def write_legacy_meta(con, updates):
    """改 legacy_letters 的 metadata_json / occurred_at，触发器原样重建。
    updates = [(memory_id, metadata_json, occurred_at 或 None), ...]；occurred_at 是记忆
      档案读的（要 ISO），metadata 里那份是信箱前端读的（要数字）。
    必须显式 BEGIN：sqlite3 只在 DML 前自动 BEGIN，DROP TRIGGER 是 DDL、不进自动事务，
      中途崩了月离的只读保护就永久拆掉了。
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DROP TRIGGER IF EXISTS legacy_letters_no_delete")
        con.execute("DROP TRIGGER IF EXISTS legacy_letters_no_update")
        n = 0
        for mid, meta_json, occ in updates:
            con.execute("UPDATE legacy_letters SET metadata_json=?, occurred_at=? "
                        "WHERE memory_id=?", (meta_json, occ, mid))
            n += 1
        con.execute(TRIG_UPDATE)
        con.execute(TRIG_DELETE)
        con.execute("COMMIT")
        return n
    except BaseException:
        try:
            con.execute("ROLLBACK")      # 回滚会把 DROP TRIGGER 一起撤销，触发器不会丢
        except Exception:
            pass
        raise


def cmd_hide_dupes(install_arg, mode=None, yes=False, keep=None) -> int:
    """藏起 legacy_letters 里重复的旧信（或把藏起来的放出来）。【会关月离】
    只改 metadata_json 里的 import_kind（+ 需要时补时间），正文和记忆档案一个字节
      不动，可逆。
    keep = "new"/"old"：留后入库的 / 留先入库的（菜单里会先摆出两边的区别让你选；
      非交互用 --keep，不给就按自动规则 = ①型优先）。
    """
    log()
    log("=" * 62)
    if mode == "unhide":
        log("  把藏起来的旧信放出来")
    else:
        log("  藏起 legacy 里重复的旧信（重复导入留下的那批）")
    log("=" * 62)
    install = find_install(install_arg)
    db = install / "install" / "data" / "memory" / "memory.sqlite3"
    log("  月离: %s" % install)
    log("  数据库: %s" % db)
    log()

    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    lib, n_legacy, _rescued = read_library(con, install)
    con.close()
    log("  legacy_letters 表 %d 封（state.json 里的 %d 封本操作不碰）"
        % (n_legacy, len(lib) - n_legacy))
    log()

    if mode == "unhide":
        fixes = []
        targets = [L for L in lib if L.get("store") == "legacy"
                   and (L.get("meta") or {}).get(HIDDEN_KIND_KEY)]
        if not targets:
            log("  没有藏起来的行，什么都不做。")
            log("=" * 62)
            log()
            return 0
        log("  找到 %d 封是我们藏起来的：" % len(targets))
        for L in targets[:8]:
            log("     「%s」" % (_peek(L["content"], 34) or "（空）"))
        if len(targets) > 8:
            log("     …另有 %d 封" % (len(targets) - 8))
    else:
        texts = archive_texts(install)
        if texts is None:
            log("  ⚠ 记忆档案读不到（没跑过月离自己的导入？）——「哪份有记忆」就查不了，")
            log("    下面只看时间和形态。")
            log()
        _h0, _f0, preview0 = dupe_plan(lib, texts)      # 逐字重复的
        # 很像但不等的要在这里先算：早退那句话不能只看逐字重复（实测有人只有这一类）
        near_pairs = near_dupe_plan(lib)
        if not preview0 and not near_pairs:
            log("  没有逐字重复的行，也没有「很像但不等」的，什么都不用做。")
            log("=" * 62)
            log()
            return 0

        if preview0:
            log("  逐字重复的：%d 组（同一封信被导进来两次留下的，每组留一份就行）"
                % len(preview0))
            log()
            log("  两边的区别（只摆前 3 组，其余同型）：")
        if near_pairs:
            log("  「很像但不等」的：%d 组（同一封信的两个版本，正文有差别）"
                % len(near_pairs))
            log("     只有一份有时间的，留有时间的那份、藏掉没时间的；")
            log("     两份都有时间或都没有的，工具不猜，先跑菜单 5 看差别。")
            log()
        for idx, g in enumerate(preview0[:3], 1):
            log()
            log("     ── [%d/%d] ────────────────────────────────" % (idx, len(preview0)))
            log("     去信（两边逐字相同，忽略空白，%d 字）：「%s」"
                % (len(norm_text(g["older"]["content"])), _peek(g["older"]["content"], 36)))
            log("       ① 旧的（先入库）  %s" % _row_desc(g["older"]))
            log("       ② 新的（后入库）  %s" % _row_desc(g["newer"]))
            for j, ln in enumerate(g["cmp"]):
                log("       %s%s" % ("差别：" if j == 0 else "      ", ln))
        if len(preview0) > 3:
            log()
            log("     …另有 %d 组（不刷屏；要逐组看就先跑菜单 5）" % (len(preview0) - 3))

        cnt = collections.Counter((_has_time(g["older"]), _has_time(g["newer"]))
                                  for g in preview0)
        if cnt:
            log()
            log("  %d 组的归类：" % len(preview0))
        for (o, n), c in cnt.most_common():
            if o and n:
                s = "两份都有时间"
            elif o:
                s = "旧的有时间、新的没有"
            elif n:
                s = "新的有时间、旧的没有"
            else:
                s = "两份都没有时间"
            log("     %-22s %d 组" % (s, c))
        blank = sum(1 for g in preview0 if any("只差空白" in x for x in g["cmp"]))
        if blank:
            log("     其中 %d 组的正文只差空白（换行全不全不一样）" % blank)
        if texts is not None:
            allmem = sum(1 for g in preview0 if g["n_mem"] == g["n"] and g["n_mem"])
            log("     记忆档案：%d 组两份都在档案里（档案按内容存，藏哪份都一样）" % allmem)
        log()

        choice = keep if keep in ("new", "old") else None
        if choice is None and not yes:
            log("  留哪一份？")
            log("     1) 留新的那份（后入库的，月离最近一次导入写进来的）")
            log("        —— 它一般没有时间，工具会从旧的那份借过来补上")
            log("     2) 留旧的那份（现在信箱里显示的那个）")
            log("        —— 时间本来就是好的，不用补")
            log("     0) 算了，不改")
            log()
            try:
                if not sys.stdin.isatty():
                    raise SystemExit("  ✗ 需要你选留哪一份。非交互时用 --keep new|old，"
                                     "或 --yes（按自动规则）。")
                c = input("  留哪一份？选 [1/2/0]（直接回车 = 取消）：").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("  没有选择，退出（什么都没改）。")
            if c not in ("1", "2"):
                raise SystemExit("  没有选择，退出（什么都没改）。")
            choice = {"1": "new", "2": "old"}[c]
            log()

        targets, fixes, preview = dupe_plan(lib, texts, keep=choice)

        # 第二类重复（很像但不等）已经在上面的展示段算过了，这里直接用
        targets += [L for L, _keep, _sc, _sr in near_pairs]
        targets = [L for L in targets if not (L.get("meta") or {}).get(HIDDEN_KIND_KEY)]

        n_hidden_already = sum(1 for g in preview for L in g["drop"]
                               if (L.get("meta") or {}).get(HIDDEN_KIND_KEY))
        if not targets and not fixes:
            log("  需要处理的都已经处理过了，什么都不做。")
            log("=" * 62)
            log()
            return 0
        log()
        log("  这一趟：要藏 %d 封" % len(targets))
        if n_hidden_already:
            log("           其中 %d 封之前就藏过了（跳过）" % n_hidden_already)
        if fixes:
            log("           还要给留下的那份补上时间 %d 封" % len(fixes))
        log("  依据：%s" % (preview[0]["why"] if preview else "（按规则）"))
        n_keep_new = sum(1 for g in preview if g["keep_is_new"])
        if len(preview) > 1:
            log("        共 %d 组：留新那份 %d 组、留旧那份 %d 组"
                % (len(preview), n_keep_new, len(preview) - n_keep_new))
        log()
        log("  它只动重复的那些；只出现一次的行一律不碰。")
    log()

    if mode == "unhide":
        log("  放出来之后：这些信会重新出现在信箱里。")
    else:
        log("  藏起来之后：")
        log("     · 这些行不再出现在信箱里（正文一个字节不删，记忆档案也不受影响）")
        log("     · 放出来的办法：菜单 8 选 2，或者再跑一次这个命令加 --mode unhide")
    log()

    if not yes:
        try:
            if not sys.stdin.isatty():
                raise SystemExit("  ✗ 这个操作要改库。非交互时请加 --yes 确认。")
            answer = input("  确认吗？输入 yes 继续（直接回车 = 取消）：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("  没有确认，退出（什么都没改）。")
        if answer != "yes":
            raise SystemExit("  没有确认，退出（什么都没改）。")
        log()

    log("  关闭月离…")
    stop_yueli(install)

    bakdir = db.parent / "_backups"
    bakdir.mkdir(parents=True, exist_ok=True)
    bak = bakdir / ("memory-hide-dupes-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
    con = sqlite3.connect(str(db), isolation_level=None)
    con.execute("VACUUM INTO ?", (str(bak),))
    log("  已备份 -> %s (%.2f MB)" % (bak.name, bak.stat().st_size / 1024 / 1024))

    try:
        updates = []
        if mode == "unhide":
            for L in targets:
                new = _unhide_meta(L.get("meta") or {})
                if new is None:
                    continue
                updates.append((L["mid"], json.dumps(new, ensure_ascii=False, sort_keys=True),
                                L.get("occurred_at")))
        else:
            for L in targets:
                new = _hide_meta(L.get("meta") or {})
                updates.append((L["mid"], json.dumps(new, ensure_ascii=False, sort_keys=True),
                                L.get("occurred_at")))
            for L, epoch in fixes:      # 留下的那封自己没时间，把同组那份的时间补给它
                new = _meta_with_time(L.get("meta") or {}, epoch)
                updates.append((L["mid"], json.dumps(new, ensure_ascii=False, sort_keys=True),
                                iso_of(epoch)))
        if not updates:
            log("  没有需要改的（都符合原样），什么都没做。")
            con.close()
            start_yueli(install)
            return 0
        n = write_legacy_meta(con, updates)
        total = con.execute("SELECT COUNT(*) FROM legacy_letters").fetchone()[0]
        trg = [x[0] for x in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='legacy_letters'")]
        log("  改了 %d 封（封数 %d 未变），触发器已重建: %s"
            % (n, total, ", ".join(sorted(trg))))
    except BaseException:
        log()
        log("  ✗ 写入失败，改动已全部回滚（数据库回到动手之前，触发器完好）。")
        log("    月离现在是【关闭】状态，请手动重新打开它。")
        raise
    finally:
        con.close()

    # 回读校验：确认改动生效了（藏：不在白名单里；放：回到白名单里）
    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    bad = 0
    for L in targets:
        row = con.execute("SELECT metadata_json FROM legacy_letters WHERE memory_id=?",
                          (L["mid"],)).fetchone()
        if row is None:
            bad += 1
            continue
        meta = json.loads(row[0] or "{}")
        want_visible = (mode == "unhide")
        if kind_in_mailbox(meta) != want_visible:
            bad += 1
    for L, epoch in fixes:              # 补时间的也要回读确认
        row = con.execute("SELECT metadata_json FROM legacy_letters WHERE memory_id=?",
                          (L["mid"],)).fetchone()
        if row is None:
            bad += 1
            continue
        m = json.loads(row[0] or "{}")
        if (m.get("backup_record") or {}).get("created_at") != epoch:
            bad += 1
    n_hidden_now = 0
    for (mj,) in con.execute("SELECT metadata_json FROM legacy_letters"):
        m = json.loads(mj or "{}")
        if not kind_in_mailbox(m):
            n_hidden_now += 1
    con.close()
    if bad:
        log("  ✗ 回读校验没过（%d 封没改对）。请把上面的输出发出来，先别启动月离。" % bad)
        log("    备份在：%s" % bak)
        return 1
    log("  回读校验通过 ✓")
    log("  现在整个 legacy 表里【信箱里不显示】的共 %d 封（含以前藏的和别人藏的）" % n_hidden_now)

    log()
    start_yueli(install)
    log()
    log("=" * 62)
    log("  做完了。界面要重开月离才看得到（约 1 分钟）。")
    log("  备份：%s" % bak.name)
    log("=" * 62)
    log()
    return 0


def cmd_dupe_menu(install_arg) -> int:
    """菜单 8 的子选择：藏 / 放。"""
    log()
    log("  处理 legacy 里重复的旧信")
    log()
    log("     1) 藏起来：重复的每组只留一封，其余的不再显示")
    log("        （逐字重复的；以及正文很像但不等、只有一份有时间的）")
    log("     2) 放出来：把以前藏起来的恢复显示")
    log()
    try:
        if not sys.stdin.isatty():
            raise SystemExit("  ✗ 需要选一种。非交互时用 hide-dupes --mode hide|unhide。")
        c = input("  选 [1/2]（直接回车 = 取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("  没有选择，退出。")
    if c == "1":
        return cmd_hide_dupes(install_arg, mode="hide")
    if c == "2":
        return cmd_hide_dupes(install_arg, mode="unhide")
    raise SystemExit("  没有选择，退出。")


def cmd_export(src: Path, out_dir) -> int:
    """把 .soul 转成 json，要哪一种在这里问。
    两种 json 长得像、用途不同：本工具的（olivia.letters.v1）存档、也能拿去菜单 2；
    月离的（letter_pairs.json）给月离自己的导入用，想建记忆就走它。
    """
    log()
    log("  要哪一种 json？")
    log()
    log("     1) 本工具用的，存档，也能拿去菜单 2")
    log("        文件名是 Olivia信件备份-合并.json")
    log("     2) 月离自己导入用的，想让林离记得这些信就走它")
    log("        文件名是 letter_pairs.json，导出时会告诉你放哪")
    log("     3) 两个都要")
    log()
    try:
        if not sys.stdin.isatty():
            raise SystemExit("  ✗ 需要选一种。非交互时用 convert 或 pairs 子命令。")
        c = input("  选 [1/2/3]（直接回车 = 取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("  没有选择，退出。")
    if c == "1":
        return cmd_convert(src, out_dir, True)
    if c == "2":
        return cmd_pairs(src, out_dir)
    if c == "3":
        a = cmd_convert(src, out_dir, True)
        b = cmd_pairs(src, out_dir)
        return 0 if (a == 0 and b == 0) else 1
    raise SystemExit("  没有选择，退出。")


def cmd_state_menu(install_arg) -> int:
    """处理 state.json 里的信（两种），问一下要哪种。"""
    log()
    log("  处理 state.json 里的信")
    log()
    log("     1) 发送失败的信（僵尸信）：检测并清理")
    log("     2) 社区工具导入的那批：藏起来，让月离的导入接管")
    log("        （要先跑过月离自己的导入，见说明书第五节）")
    log()
    try:
        if not sys.stdin.isatty():
            raise SystemExit("  ✗ 需要选一种。非交互时用 zombies 或 hide-imported 子命令。")
        c = input("  选 [1/2]（直接回车 = 取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("  没有选择，退出。")
    if c == "1":
        return cmd_zombies(install_arg)
    if c == "2":
        return cmd_hide_imported(install_arg)
    raise SystemExit("  没有选择，退出。")


# ─────────────────────────── 交互菜单 ───────────────────────────

BANNER = "Olivia 记忆迁移工具"


def clean_path(s: str) -> str:
    """拖进窗口的路径会自带引号，去掉。"""
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def ask_path(prompt: str) -> Path | None:
    try:
        raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    s = clean_path(raw)
    if not s:
        return None
    p = Path(s)
    if not p.exists():
        log("  路径不存在：%s" % p)
        return None
    return p


def menu(prefill: str = "") -> int:
    log()
    log("=" * 62)
    # 版本号打在界面上，不只在日志里（一台机器上可能有好几份副本）
    log("  %s   v%s" % (BANNER, __version__))
    log("=" * 62)
    log()

    src = None
    if prefill:
        p = Path(clean_path(prefill))
        if p.exists():
            src = p
            log("  已带入路径：%s" % p)
            log()

    while True:
        log("  你要做什么？")
        log()
        log("    1) 把 .soul 转成 json 文件（只读，不动月离）")
        log("       存档用，或者给月离自己的导入用，进去会让你选")
        log("    2) 把 .soul / json 写进月离的信件库（会关掉月离再开）")
        log("    3) 重新指定月离的安装目录")
        log("    4) 只体检：现在是什么状况？（只读，绝不改动，也不关月离）")
        log("    5) 并排对比：源文件和库里「去信一样、回信不同」的那几封")
        log("       （只读，要先给 .soul；用来决定这种信留哪一版）")
        log("    6) 处理 state.json 里的信（会关月离）")
        log("       1 僵尸信 / 2 社区工具导入的那批（藏起来，让月离导入接管）")
        log("    7) 回退月离的版本（补丁把游戏搞到打不开时用；会关月离）")
        log("       只换一个版本指针，data / profile 一个字节不碰；再跑一次就退回去")
        log("    8) 藏起 legacy 里重复的旧信（重复导入留下的那批；会关月离）")
        log("       进去会让你选：1 藏起来，2 放出来")
        log("    0) 退出")
        log()
        try:
            choice = input("  选择 [1/2/3/4/5/6/7/8/0]: ").strip()
        except (EOFError, KeyboardInterrupt):
            log()
            return 0
        log()

        if choice == "0" or choice == "":
            return 0

        if choice == "3":
            cur = load_saved_install()
            log("  当前记住的：%s" % (cur if cur else "（还没指定过）"))
            log()
            if ask_install_path() is None:
                log("  没有改动。")
            log()
            continue

        if choice == "4":
            return cmd_check(None)          # 体检不用给 .soul
        if choice == "6":
            return cmd_state_menu(None)     # 处理 state.json，不用给 .soul
        if choice == "7":
            return cmd_rollback(None)       # 回退版本，也不用给 .soul
        if choice == "8":
            return cmd_dupe_menu(None)      # 藏/放重复的旧信，也不用给 .soul

        if choice not in ("1", "2", "5"):
            log("  输入 1、2、3、4、5、6、7、8 或 0。")
            log()
            continue

        if src is None:
            src = ask_path("  把 .soul 或文件夹拖进来，或粘贴路径，然后回车：")
            log()
            if src is None:
                continue

        if choice == "1":
            return cmd_export(src, None)       # 要哪种 json，进去问
        if choice == "5":
            return cmd_compare(src, None)      # 对比要给 .soul，和体检不一样
        return cmd_apply(src, None)


# ─────────────────────────── 命令行 ───────────────────────────

def parse_common(args):
    """<源> [--out DIR] [--install DIR] [--per-file] [--check|--dry-run]
    [--near 策略] [--mode hide|delete] [--to 版本]"""
    src = None
    out = None
    install = None
    per_file = False
    dry = False
    near = None
    mode = None
    yes = False
    to = None
    keep = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out" and i + 1 < len(args):
            out = Path(args[i + 1]); i += 2
        elif a == "--install" and i + 1 < len(args):
            install = Path(args[i + 1]); i += 2
        elif a == "--near" and i + 1 < len(args):
            near = args[i + 1].strip().lower(); i += 2
        elif a == "--mode" and i + 1 < len(args):
            mode = args[i + 1].strip().lower(); i += 2
        elif a == "--to" and i + 1 < len(args):
            to = args[i + 1].strip(); i += 2
        elif a == "--keep" and i + 1 < len(args):
            keep = args[i + 1].strip().lower(); i += 2
        elif a in ("--yes", "-y"):
            yes = True; i += 1
        elif a in ("--per-file", "--no-merge"):
            per_file = True; i += 1
        elif a in ("--check", "--dry-run", "--dryrun"):
            dry = True; i += 1
        elif src is None:
            src = Path(a); i += 1
        else:
            i += 1
    if near is not None and near not in NEAR_CHOICES:
        raise SystemExit("  ✗ --near 只认 source / library / both，收到的是：%s" % near)
    if mode is not None and mode not in ("hide", "delete", "unhide"):
        raise SystemExit("  ✗ --mode 只认 hide / delete / unhide（unhide 给 hide-dupes 用），"
                         "收到的是：%s" % mode)
    if keep is not None and keep not in ("new", "old"):
        raise SystemExit("  ✗ --keep 只认 new / old（给 hide-dupes 用，留哪一份），"
                         "收到的是：%s" % keep)
    return src, out, install, per_file, dry, near, mode, yes, to, keep


def pause_at_exit(argv) -> None:
    """退出前停一下等回车。
    双击 .py 打开的窗口，程序一结束就关了，看着像"闪退"；只在没带命令行参数、
    且 stdin 是真控制台时才停。
    """
    if argv:
        return
    try:
        if not sys.stdin.isatty():
            return
    except Exception:
        return
    try:
        log()
        input("  按回车键关闭窗口… ")
    except (EOFError, KeyboardInterrupt):
        log()


def main(argv) -> int:
    """入口：先开日志、记运行环境，干完活再记结束状态。
    这里刻意【不】兜异常，让它冒到最外层再处理：那边先写完整堆栈、最后写结束状态，
    日志读起来的顺序才对。
    """
    open_log()
    set_console_title()
    log_header(argv)
    rc = _run(argv)
    log_footer("成功" if rc == 0 else "失败（退出码 %d）" % rc)
    return rc


def _run(argv) -> int:
    if not argv:
        return menu()

    cmd = argv[0].lower()
    if cmd in ("convert", "apply", "check", "compare", "diff", "zombies", "zombie",
               "pairs", "hide-imported", "rollback", "hide-dupes"):
        src, out, install, per_file, dry, near, mode, yes, to, keep = parse_common(argv[1:])
        if cmd == "check":
            return cmd_check(install)        # 体检只看库，不需要来源文件
        if cmd in ("zombies", "zombie"):
            return cmd_zombies(install, mode=mode)   # 清僵尸信也不用来源文件
        if cmd == "hide-imported":
            return cmd_hide_imported(install, yes=yes)
        if cmd == "rollback":
            return cmd_rollback(install, yes=yes, to=to)   # 退版本也不用来源文件
        if cmd == "hide-dupes":
            return cmd_hide_dupes(install, mode=mode, yes=yes, keep=keep)   # 藏/放重复的旧信
        if src is None:
            log("  用法: %s %s <源文件或文件夹>" % (Path(sys.argv[0]).name, cmd))
            return 1
        if not src.exists():
            log("  源不存在：%s" % src)
            return 1
        if cmd == "convert":
            return cmd_convert(src, out, not per_file)
        if cmd == "pairs":
            return cmd_pairs(src, out)
        if cmd in ("compare", "diff"):
            return cmd_compare(src, install)
        if cmd == "apply":
            return cmd_apply(src, install, dry_run=dry, near=near)
        # 认不出来的子命令【绝不猜】，宁可报错：掉到 apply 会在命令名打错时写库。
        log("  不认识的命令：%s" % cmd)
        log("  可用的是：convert / pairs / apply / compare / check / zombies / "
            "hide-imported / rollback / hide-dupes")
        return 1

    return menu(argv[0])


if __name__ == "__main__":
    _argv = sys.argv[1:]
    try:
        _rc = main(_argv)
    except SystemExit as _e:
        # SystemExit 那类报错的消息得先打出来，不然双击时一闪就没了
        if isinstance(_e.code, str) and _e.code:
            log()
            log("  %s" % _e.code)
        _rc = 0 if _e.code in (None, 0) else 1
    except KeyboardInterrupt:
        log()
        log("  已中断。")
        log_footer("被用户中断")
        _rc = 130
    except Exception as _ex:
        log()
        log("  ✗ 出错了：%s: %s" % (type(_ex).__name__, _ex))
        log("    数据库没有被改动的部分不受影响；若已自动备份，可用备份覆盖回 memory.sqlite3。")
        if LOG_PATH is not None:
            log("    完整堆栈已写进日志：%s" % LOG_PATH)
            log("    （把这个文件发给帮忙的人就能查）")
        else:
            log("    （日志文件没能写出来，请把上面的输出整段复制下来）")
        log_traceback()
        log_footer("异常中止：%s: %s" % (type(_ex).__name__, _ex))
        _rc = 1

    pause_at_exit(_argv)
    sys.exit(_rc)
