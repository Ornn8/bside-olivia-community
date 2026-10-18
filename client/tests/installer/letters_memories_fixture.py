"""A synthetic local service and page for the letters-and-memories window.

Everything here is made up: letters, replies, memories and the service answers. The page runs the complete injected
settings script (``BOOTSTRAP_JAVASCRIPT``) against an in-process stand-in for the local companion service, so the
window is exercised the way it runs in the app, without a profile, an account or a network.
Used by ``test_letters_memories_browser.py`` and ``scripts/preview_letters_memories.py``.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from original_client_settings_ui import BOOTSTRAP_JAVASCRIPT

ORIGIN = "http://127.0.0.1:27149"
TOKEN = "s" * 40

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>*{box-sizing:border-box}body{margin:0;background:#18191c;color:#ece9e2;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}
header{display:flex;justify-content:space-between;padding:14px 24px;color:#9a968e}
h1#native-title{position:absolute;left:24px;top:56px;margin:0;font:700 30px/44px system-ui,"Microsoft YaHei",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px}.tp-settings-item{padding:8px 0}
.synthetic{position:fixed;bottom:12px;left:24px;font-size:12px;color:#8f8b84}
/* the few utility classes the native app gives its buttons, so that the injected button looks as it does there */
button{background:transparent;color:#d1d5db;font:inherit}.px-6{padding-left:24px;padding-right:24px}[class~="py-2.5"]{padding-top:10px;padding-bottom:10px}
.rounded-full{border-radius:9999px}.border{border:1px solid}.border-grey-5{border-color:#6b7280}.cursor-pointer{cursor:pointer}</style>
<script src="%(origin)s/assets/olivia-companion-settings.js" data-olivia-companion-settings="p03.original-settings-shell.v1"
 data-ui-version="test" data-api-base="%(origin)s/"></script></head>
<body><header><span>Resonance Edition</span></header><h1 id="native-title">%(title)s</h1>
<main><div class="tp-settings-item"><div>Settings</div></div></main>
<span class="synthetic">%(note)s</span></body></html>"""

COPY = {
    "zh": {
        "title": "信箱",
        "note": "合成数据 · 界面预览（背景为测试外壳）",
        "letters": [
            ("今天路过那家花店", "今天走了一条和平时不同的路，经过河边的时候，风很舒服。路过那家花店，门口摆着一大束白色的花，想起你说过喜欢安静的颜色。",
             "收到你的来信了。我喜欢听你讲这些小事。下次路过的时候，也替我看看那里的花吧。"),
            ("你还记得我喜欢什么口味吗", "突然想问你一个很小的问题：你还记得我喜欢什么口味的冰淇淋吗？我上次说过一次，不知道你有没有记住。",
             "我记得的，是开心果味。你说那个绿色很像春天刚冒出来的叶子。"),
            ("早上好啊", "早上好啊。今天起得有点早，窗外的天还是灰蓝色的。", "早上好。灰蓝色的天很适合慢慢醒来，今天也不用着急。"),
            ("窗外的雨停了", "雨停了。" + "傍晚的时候我坐在窗边，听着屋檐上最后几滴水落下来，心里慢慢安静下来。" * 6,
             "雨停之后的空气总是很干净。愿你今晚睡得安稳。"),
            ("想和你分享的小事", "今天学会了一首很短的曲子，只有十六小节，但是弹下来很有成就感。", "能把一件小事做完整，就很了不起。"),
            ("周末去河边散步吧", "周末如果天气好，我想去河边散步，你要不要也想象一下和我一起走？", "我想象了一下，河面上有很细的光。"),
        ],
        "memories": [
            "喜欢开心果味的冰淇淋，觉得那种绿色像春天刚冒出来的叶子",
            "最近在练一首只有十六小节的短曲子",
            "喜欢安静的颜色，尤其是白色的花",
            "傍晚常常坐在窗边听雨",
            "周末想去河边散步",
            "早上起得早的时候，喜欢看灰蓝色的天",
        ],
        "empty_query": "不存在的词语",
    },
    "en": {
        "title": "Mailbox",
        "note": "Synthetic data · interface preview (the background is a test shell)",
        "letters": [
            ("Passing the flower shop today", "I took a different road today and passed the flower shop by the river. There was a big bunch of white flowers at the door, and I remembered you like quiet colours.",
             "I got your letter. I like hearing about small things like this. Next time you pass, look at the flowers for me too."),
            ("Do you remember my favourite flavour", "A very small question: do you remember which ice cream flavour I like? I only said it once.",
             "I remember. Pistachio. You said that green looks like the first leaves of spring."),
            ("Good morning", "Good morning. I woke up early and the sky outside was still grey-blue.", "Good morning. A grey-blue sky is a gentle way to wake up. There's no hurry today."),
            ("The rain has stopped", "The rain stopped. " + "In the evening I sat by the window and listened to the last drops fall from the eaves until everything went quiet. " * 4,
             "The air is always so clean after rain. I hope you sleep well tonight."),
            ("A small thing to share", "I learned a short piece today, only sixteen bars, but it felt good to play it through.", "Finishing a small thing properly is no small thing."),
            ("A walk by the river this weekend", "If the weather is good this weekend I'd like to walk by the river. Will you imagine walking with me?", "I imagined it. The river had very fine light on it."),
        ],
        "memories": [
            "Likes pistachio ice cream; the green looks like the first leaves of spring",
            "Has been practising a short sixteen-bar piece",
            "Likes quiet colours, especially white flowers",
            "Often sits by the window in the evening listening to the rain",
            "Wants to walk by the river at the weekend",
            "Likes watching the grey-blue sky when waking up early",
        ],
        "empty_query": "nonexistentword",
    },
}


class Unavailable(Exception):
    """Raised by an answer to make the service reply with an HTTP error."""


class Service:
    """The answers of the local service, kept in memory, with every request recorded."""

    def __init__(self, language: str = "zh", *, memory_capability: dict | None = None, repeat: int = 1):
        copy = COPY[language]
        self.language = language
        self.copy = copy
        self.requests: list[tuple[str, dict, dict | None]] = []
        self.letters = []
        self.originals = []
        for number in range(repeat):
            for index, (summary, content, reply) in enumerate(copy["letters"]):
                key = number * len(copy["letters"]) + index
                self.letters.append({"letter_id": f"synthetic-{key}", "created_at": 1792230000 - key * 86400, "summary": summary,
                                     "content": content, "reply_text": reply, "replied_at": 1792230000 - key * 86400 + 3600, "origin": "user"})
        for key, row in enumerate(self.letters):
            for speaker, text in (("user", row["content"]), ("linli", row["reply_text"])):
                stamp = f"2026-10-{17 - key % 15:02d}T09:{'00' if speaker == 'user' else '30'}:00+00:00"
                self.originals.append({"source_id": f"letter-{key}", "speaker": speaker, "created_at": stamp, "full": text,
                                       "text": text[:120] if len(text) > 160 else text, "excerpt": len(text) > 160})
        self.trash = [{"id": "trash-1", "letter_id": "synthetic-archived", "part": "user", "status": "deleted", "expires_at": 1794822000,
                       "summary": copy["letters"][5][1][:40]}]
        self.memories = [{"memory_id": f"memory-{index}", "text": text, "source_id": f"letter-{index}",
                          "created_at": f"2026-10-{16 - index:02d}T08:00:00+00:00"} for index, text in enumerate(copy["memories"])]
        self.memory_capability = memory_capability or {"state": "available", "count": len(self.memories)}
        self.deleted: list[dict] = []

    # --- helpers ---------------------------------------------------------------------------------------------
    def calls(self, path: str) -> list[tuple[dict, dict | None]]:
        return [(query, body) for seen, query, body in self.requests if seen == path]

    def _browse(self, query: dict) -> dict:
        word = query.get("query", "")
        page = int(query.get("page", "1"))
        limit = int(query.get("limit", "20"))
        if query.get("collection") == "originals":
            rows = [row for row in self.originals if (not word or word in row["full"]) and
                    (not query.get("source_id") or row["source_id"] == query["source_id"])]
            window = rows[(page - 1) * limit: page * limit]
            full = query.get("full") == "1"
            return {"status": "READY", "schema_version": "x", "indexed_letters": len(self.originals), "archive_total": 24, "archive_indexed": 20,
                    "archive_removed": 0, "scanned_at": None, "total": len(rows), "page": page, "limit": limit,
                    "originals": [{"source_id": r["source_id"], "speaker": r["speaker"], "created_at": r["created_at"],
                                   "text": r["full"] if full else r["text"], "excerpt": False if full else r["excerpt"]} for r in window]}
        rows = [row for row in self.memories if not word or word in row["text"]]
        if query.get("sort") == "old":
            rows = list(reversed(rows))
        return {"status": "READY", "memories": rows[(page - 1) * limit: page * limit], "total": len(rows),
                "total_count": len(self.memories), "page": page, "limit": limit}

    def answer(self, method: str, path: str, query: dict, body: dict | None):
        self.requests.append((path, query, body))
        envelope = lambda data: {"code": 0, "data": data}  # noqa: E731
        if path == "/toy/companion/status":
            return {"status": "READY", "capabilities": {"memory": self.memory_capability, "private_world": {"state": "available"},
                                                         "candidates": {"state": "available"}}}
        if path == "/toy/companion/memory":
            return self._browse(query)
        if path.startswith("/toy/companion/memory/"):
            return {"status": "APPLIED", "retried_count": 0}
        if path == "/toy/letter/list":
            return envelope({"list": [{k: v for k, v in row.items() if k in ("letter_id", "created_at", "summary", "origin")}
                                      for row in self.letters if not (self.gone(row["letter_id"], "user") and self.gone(row["letter_id"], "reply"))]})
        if path == "/toy/letter/detail":
            row = next(item for item in self.letters if item["letter_id"] == query["letter_id"])
            return envelope({**row, "content": "" if self.gone(row["letter_id"], "user") else row["content"],
                             "reply_text": "" if self.gone(row["letter_id"], "reply") else row["reply_text"]})
        if path == "/toy/letter/deletion/list":
            return envelope({"items": self.trash})
        if path == "/toy/letter/deletion/delete":
            self.deleted.append(body)
            return envelope({})
        if path == "/toy/letter/deletion/restore":
            self.trash = []
            return envelope({})
        if path == "/toy/setup/status":
            return {"status": "READY", "session_token": TOKEN, "setup_completed": True, "show_initial_setup": False, "llm": {"key_configured": True}}
        if path == "/toy/updates/local/status":
            return {"version": "2.4.7"}
        return {"code": 200, "data": {}}

    def gone(self, letter_id: str, part: str) -> bool:
        return any(item["letter_id"] == letter_id and item["part"] == part for item in self.deleted)


def boot(browser, service: Service, *, hash: str = "#/collection", language: str | None = None, width: int = 1200, height: int = 900,
         locale: str = "en-US"):
    """The whole injected script in a page, answered by ``service``."""
    page = browser.new_page(viewport={"width": width, "height": height}, locale=locale)
    if language:
        page.add_init_script(f"try{{localStorage.setItem('olivia.ui.language','{language}')}}catch(_){{}}")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.errors = errors
    html = PAGE % {"origin": ORIGIN, "title": service.copy["title"], "note": service.copy["note"]}

    def handle(route):
        request = route.request
        url = urlparse(request.url)
        if url.path == "/assets/olivia-companion-settings.js":
            return route.fulfill(content_type="application/javascript", body=BOOTSTRAP_JAVASCRIPT)
        if url.path == "/":
            return route.fulfill(content_type="text/html", body=html)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            body = request.post_data_json if request.method == "POST" else None
        except Exception:  # noqa: BLE001
            body = None
        try:
            answer = service.answer(request.method, url.path, query, body)
        except Unavailable:
            return route.fulfill(status=503, content_type="application/json",
                                 body=json.dumps({"status": "UNAVAILABLE", "error_code": "COMPANION_READ_UNAVAILABLE"}))
        route.fulfill(content_type="application/json", body=json.dumps(answer))

    page.route(ORIGIN + "/**", handle)
    page.goto(ORIGIN + "/" + hash)
    return page
