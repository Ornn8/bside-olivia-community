"""English wording for the text the backend itself produces for the user.

``runtime/ui_i18n.py`` covers the injected pages. This module covers what the backend writes: notices and short
messages sent into the chat, labels and descriptions it returns to the settings pages, dialog titles, launcher
messages. The source is written in Chinese as ``tr("<Chinese source>")``; ``tr`` returns the English text below when
``runtime.ui_language.current()`` is ``"en"`` and an entry exists, otherwise the Chinese string unchanged, so a missing
entry never shows a raw key and Chinese mode stays exactly as it was. ``{name}`` placeholders are filled from the
keyword arguments in both languages (one sentence with named values, so English can reorder them).

Not translated here, on purpose: anything the model reads or writes (prompts, letters, chat replies, diary prose,
the daily plan), data keys and commands the services recognise (for example ``/连接测试``), and file names the user
already has.
"""

from __future__ import annotations

import re

from runtime import ui_language

# Chinese source text -> English. Keep placeholders identical on both sides.
EN: dict[str, str] = {
    # --- Chat notices and messages (QQ / WeChat) ---
    "【系统提示】Olivia 账户余额不足，这条消息没能回复。请在“回信服务 → Olivia 账户”充值后再聊天。":
        "[System notice] Your Olivia balance is too low, so this message couldn't be answered. Top up in “Reply service → Olivia account” and then chat again.",
    "【系统提示】这条消息暂时没能回复，用量仍在核对。请勿反复重发同一条；你可以发送新消息继续聊天。":
        "[System notice] This message couldn't be answered yet because usage is still being verified. Please don't resend the same message over and over; you can send a new one to keep chatting.",
    "【系统提示】服务暂时无法受理这条回复，请稍后再试。":
        "[System notice] The service can't take this reply right now. Please try again in a little while.",
    "【系统提示】这条消息的回复生成失败，暂时没能回复。可以稍后重新发一下。":
        "[System notice] We couldn't generate a reply to this message. You can send it again in a little while.",
    "照片这次没生成成功，没能发给你。Olivia 可用余额不足，请充值后重试。":
        "The photo didn't come out this time, so I couldn't send it to you. Your Olivia balance is too low; please top up and try again.",
    "照片这次没生成成功，没能发给你。稍后再试一下。":
        "The photo didn't come out this time, so I couldn't send it to you. Please try again in a little while.",
    "照片没能确认发送成功，先告诉你一声。":
        "I couldn't confirm the photo was delivered, so I'm letting you know.",
    "这次钢琴录音暂时没能完成，请稍后再试。":
        "The piano recording couldn't be finished this time. Please try again later.",
    "这段音频暂时没能生成成功，稿子已经保留，稍后可以重试。":
        "That audio couldn't be generated this time. The script is saved, so you can try again later.",
    "连接测试验证码：{code}\n请原样回复这 4 位数字。回复后设置页会显示端到端已验证。":
        "Connection test code: {code}\nPlease reply with these 4 digits exactly as shown. Once you do, Settings will show the connection as verified end to end.",
    "连接测试消息已提交。正式端到端验证请在 Olivia 设置页按提示完成。":
        "Connection test message received. To finish the real end-to-end check, follow the prompts in Olivia's Settings.",
    "连接测试成功。这是固定测试消息，还没有调用林离的记忆和世界。":
        "Connection test succeeded. This is a fixed test message; Linli's memories and world haven't been used yet.",
    "微信连接测试就绪；请在授权后的会话中发送 /连接测试。普通消息不会发送给模型或写入记忆。":
        "WeChat connection test is ready. In the authorized conversation, send /连接测试. Ordinary messages are not sent to the model or saved to memory.",
    "QQ 已连接协议端，等待登录状态；仅响应指定用户的 /连接测试。":
        "QQ is connected to the protocol endpoint and waiting for its login status. Only /连接测试 from the named user is answered.",
    "对了，我之前往信箱里放了封信，你有空的时候再看看就好。":
        "Oh, by the way, I left a letter in your mailbox earlier. Have a look whenever you have time.",
    "给你弹了一整首我自己写的《{title}》，完整的，录了 {seconds} 秒。":
        "I played you a whole piece I wrote myself, “{title}”, start to finish. It runs {seconds} seconds.",
    "给你弹了《{title}》的{section}，录了 {seconds} 秒。":
        "I played you the {section} of “{title}”, {seconds} seconds.",
    "开头": "beginning",
    "中段": "middle",
    "结尾": "ending",
    "这是{when}{time}那次洗澡后的样子。": "This is how I looked after my bath {when}at {time}.",
    "这是{when}{time}拍的。": "This was taken {when}at {time}.",
    "今天 ": "today ",
    "{date} ": "on {date} ",
    "钢琴小样": "Piano demo",
    "睡前音频": "Bedtime audio",
    "音频_": "Audio_",
    "想和你说说话": "Wanted to talk with you",
    "想和你聊两句": "Wanted to chat a little",
    "林离的原创歌曲": "Linli's original song",
    "林离的翻唱": "Linli's cover",
    "玩家": "Player",
    "独奏": "Solo",
    # --- Music, component and dependency panels ---
    "原创歌曲音色与曲风": "Original-song voice and style",
    "1000 步林离音色与单依纯钢琴曲风，配合 ACE 唱歌组件使用":
        "The 1000-step Linli voice and a Shan Yichun–style piano sound; used together with the ACE singing component",
    "媒体工具": "Media tools",
    "所有音频、视频和本地演奏导入共用": "Shared by all audio, video and local-performance imports",
    "说话语音 · 2250": "Speech voice · 2250",
    "生成说话音频，包含运行环境、模型与音色": "Generates spoken audio; includes the runtime, model and voice",
    "唱歌 · ACE": "Singing · ACE",
    "用上传音源生成唱歌音频，包含模型、运行环境与音色":
        "Generates singing audio from an uploaded source; includes the model, runtime and voice",
    "口型视频": "Lip-sync video",
    "把说话或唱歌音频制作成口型视频": "Turns spoken or sung audio into a lip-synced video",
    "视频场景": "Video scenes",
    "说话、演奏与转场素材，视频模式共用": "Speaking, performance and transition footage shared by the video modes",
    "唱歌视频人声分离": "Vocal separation for singing videos",
    "仅唱歌视频需要，纯音频无需安装": "Only needed for singing videos; audio-only needs no install",
    "歌词识别（可选）": "Lyrics recognition (optional)",
    "自动识别上传音源的歌词；手填歌词无需安装":
        "Detects the lyrics of an uploaded source automatically; not needed if you type the lyrics yourself",
    "说话语音 · 2250（AMD 实验）": "Speech voice · 2250 (AMD experimental)",
    "Windows 11 / ROCm 专用；仅语音，尚未完成 AMD 实机验收":
        "For Windows 11 / ROCm only; speech only, not yet verified on AMD hardware",
    "云端服务": "Cloud service",
    "本地素材组装": "Local asset assembly",
    "云端语音视频": "Cloud speech video",
    "云端音乐视频": "Cloud music video",
    "本地音视频工具": "Local audio and video tools",
    "由 OLIVIA_REPLY_VOICE_REFERENCE 显式配置": "Set explicitly by OLIVIA_REPLY_VOICE_REFERENCE",
    "由提供此私有版本的安装程序管理": "Managed by the installer that provides this private build",
    "国内：ModelScope 社区镜像；备用：GitHub / Hugging Face":
        "Mainland China: ModelScope community mirror; fallback: GitHub / Hugging Face",
    "国内：ModelScope；备用：Hugging Face": "Mainland China: ModelScope; fallback: Hugging Face",
    "国内源（ModelScope）": "Mainland China source (ModelScope)",
    "语音合成（Breeze TTS 2）": "Speech synthesis (Breeze TTS 2)",
    "国内：HF-Mirror；备用：Hugging Face。模型限研究与非商业用途，NVIDIA 10GB 及以上显存已实测；AMD 语音为实验支持，需专用离线包、Windows 11 和兼容 ROCm 的显卡，建议 12GB 以上显存，尚未完成 AMD 实机验收":
        "Mainland China: HF-Mirror; fallback: Hugging Face. The model is limited to research and non-commercial use. Tested on NVIDIA GPUs with 10 GB or more of VRAM. AMD speech is experimental: it needs a dedicated offline package, Windows 11 and a ROCm-compatible card (12 GB or more of VRAM recommended) and has not yet been verified on AMD hardware",
    "国内源（HF-Mirror）": "Mainland China source (HF-Mirror)",
    "官方源（Hugging Face）": "Official source (Hugging Face)",
    "口型视频（LatentSync）": "Lip-sync video (LatentSync)",
    "国内：ByteDance Gitee + HF-Mirror；备用：GitHub / Hugging Face":
        "Mainland China: ByteDance Gitee + HF-Mirror; fallback: GitHub / Hugging Face",
    "国内源（ByteDance Gitee）": "Mainland China source (ByteDance Gitee)",
    "官方源（GitHub）": "Official source (GitHub)",
    "音乐生成（MiniMax Music 3）": "Music generation (MiniMax Music 3)",
    "国内：HF-Mirror；备用：Hugging Face": "Mainland China: HF-Mirror; fallback: Hugging Face",
    "人声分离（RoFormer）": "Vocal separation (RoFormer)",
    "演唱音色转换（SoulX-Singer-SVC）": "Singing voice conversion (SoulX-Singer-SVC)",
    "使用本机 SoulX-Singer-SVC 运行环境与离线模型":
        "Uses the SoulX-Singer-SVC runtime and offline model on this computer",
    "Olivia 场景与转场素材": "Olivia scene and transition footage",
    "从用户本机正版 Olivia 导入，不联网下载":
        "Imported from the genuine copy of Olivia on this computer; nothing is downloaded",
    "Olivia 音乐视频与转场素材": "Olivia music video and transition footage",
    "媒体工具（FFmpeg）": "Media tools (FFmpeg)",
    "当前核心安装器未包含；可使用系统 FFmpeg 或后续受管包":
        "Not included in the current core installer; you can use the system FFmpeg or a managed package later",
    "官方 Python 包（PyPI）": "Official Python package (PyPI)",
    "媒体工作目录": "Media work folder",
    "由客户端自动创建，无需下载": "Created automatically by the app; nothing to download",
    "受管林离音色": "Managed Linli voice",
    "检查视频运行依赖": "Checking video dependencies",
    "解压运行环境": "Unpacking the runtime",
    "校验安装文件": "Verifying installed files",
    "校验安装文件 {done} / {total} MiB": "Verifying installed files {done} / {total} MiB",
    "安装本地运行依赖（无需联网）": "Installing local runtime dependencies (no internet needed)",
    "校验解压文件": "Verifying unpacked files",
    # --- Letter maintenance and sticker packs ---
    "恢复整理前的显示和时间": "Restore the display and times from before tidying",
    "从信箱收起这封失败信": "Fold away this failed letter from the mailbox",
    "收起旧工具导入信": "Fold away letters imported by the old tool",
    "保留右侧，收起左侧": "Keep the right one, fold away the left one",
    "保留左侧，收起右侧": "Keep the left one, fold away the right one",
    "按备份修复时间和顺序": "Fix the time and order from the backup",
    "手绘": "Hand-drawn",
    "静蓝": "Quiet blue",
    "唱片": "Vinyl",
    "戏剧漫画": "Dramatic manga",
    "奇想漫画": "Whimsical manga",
    "动图": "Animated",
    # --- Relationship text built into the client (used when the service sends none) ---
    "先不定义": "Not defined yet",
    "恋人": "Partner",
    "挚友": "Close friend",
    "家人": "Family",
    "最近她更熟悉你了。": "She's gotten to know you better lately.",
    "最近她更信任你了。": "She's trusted you more lately.",
    "最近她在你面前更自在了。": "She's felt more at ease around you lately.",
    "最近你们更亲近了。": "You two have grown closer lately.",
    "最近你们之间有些紧张。": "Things have been a little tense between you lately.",
    "初识": "Just met",
    "你们刚刚走进彼此的生活，正在一点点认识对方。":
        "You've only just stepped into each other's lives and are getting to know each other little by little.",
    "熟悉": "Getting familiar",
    "日常的分享让彼此不再陌生，聊天渐渐变得自在。":
        "Sharing everyday moments has made you less strange to each other, and chatting is getting easy.",
    "朋友": "Friends",
    "你们愿意分享生活里的小事，也开始在意彼此的心情。":
        "You like sharing the small things in life, and you've started to care how the other is feeling.",
    "信赖": "Trust",
    "在一次次倾听和回应里，你们有了可以安心依靠的信任。":
        "Through listening and answering again and again, you've built a trust you can lean on.",
    "心意相通": "In tune",
    "无话不说": "Nothing unsaid",
    "像家人一样": "Like family",
    "你们选择了适合彼此的相处方式，继续在真实的生活里陪伴对方。":
        "You've chosen a way of being together that suits you both, and you keep each other company in real life.",
    "共同生活": "Life together",
    "新的季节来到，你们继续分享平凡而珍贵的日子。":
        "A new season has come, and you keep sharing ordinary, precious days.",
    "冬天": "Winter",
    "春天": "Spring",
    "夏天": "Summer",
    "秋天": "Autumn",
    "她还在认识你。": "She's still getting to know you.",
    "她正在慢慢了解你的日常。": "She's slowly learning about your daily life.",
    "她已熟悉不少关于你的事。": "She knows quite a lot about you now.",
    "你们对彼此的生活很熟悉。": "You know each other's lives well.",
    "信任正在萌芽。": "Trust is just beginning to grow.",
    "她开始愿意向你敞开心扉。": "She's starting to open her heart to you.",
    "她愿意把重要的心事告诉你。": "She's willing to tell you what weighs on her heart.",
    "她很信任你，也珍惜这份信任。": "She trusts you deeply and treasures that trust.",
    "她还需要一点时间放松。": "She still needs a little time to relax.",
    "和你相处时，她渐渐自在。": "She's gradually feeling at ease with you.",
    "她在你面前可以安心做自己。": "She can be herself around you.",
    "你们的相处让她很安心。": "Being with you puts her at ease.",
    "你们正在寻找合适的距离。": "You're finding the right distance.",
    "她开始期待与你分享。": "She's starting to look forward to sharing with you.",
    "你已经在她心里占有重要的位置。": "You hold an important place in her heart.",
    "你们珍惜彼此的陪伴。": "You treasure each other's company.",
    "你们之间没有明显的紧张。": "There's no real tension between you.",
    "偶尔的小摩擦仍需要耐心。": "The occasional small friction still takes patience.",
    "有些事还需要好好谈谈。": "Some things still need a proper talk.",
    "她需要被尊重，也需要一点缓和的空间。": "She needs to be respected, and a little room to cool down.",
    "你让她多了解了一点自己": "You let her know a little more about yourself",
    "你跟她说了心里话": "You told her what was on your mind",
    "你认真地关心了她": "You really cared for her",
    "她接住了你的情绪": "She caught your feelings and held them",
    "你们定下了一个约定": "You made a plan together",
    "你们的约定有了后续": "Your plan had a follow-up",
    "你们一起完成了一件事": "You finished something together",
    "你表达了对她的在意": "You showed her you care",
    "你们尊重了彼此的边界": "You respected each other's boundaries",
    "你们化解了一次矛盾": "You settled a disagreement",
    "你们之间发生了一次冲突": "There was a conflict between you",
    "你们的关系，藏在每一次认真回应里。": "Your relationship lives in every sincere reply.",
    "有些小别扭还没说开，可以慢慢聊。": "There's a little awkwardness not yet talked through; you can take your time.",
    "聊聊那个还没完成的约定吧。": "Let's talk about that plan you haven't finished yet.",
    "分享一件今天真正触动你的小事，不必刻意找话题。":
        "Share one small thing that really touched you today; no need to hunt for a topic.",
    # --- Wardrobe styles built into the client ---
    "原版日常": "Original everyday",
    "黑色贴身长袖、黑色短裤与铆钉腰带，保留原版造型。":
        "A fitted black long-sleeve top, black shorts and a studded belt, keeping the original look.",
    "黑色罗纹贴身长袖 · 微波浪边": "Black ribbed fitted long sleeve · gently scalloped edge",
    "黑色牛仔短裤": "Black denim shorts",
    "铆钉腰带 · 深色长靴": "Studded belt · dark boots",
    "居家针织": "Cozy knit",
    "炭灰色宽松针织衫、柔软长裤，轻松的居家穿着。":
        "A loose charcoal knit sweater and soft trousers; relaxed at-home wear.",
    "炭灰色宽松针织衫": "Loose charcoal knit sweater",
    "深色柔软居家长裤": "Soft dark lounge trousers",
    "简洁室内拖鞋": "Simple house slippers",
    "城市休闲": "City casual",
    "深色短袖、蓝色直筒牛仔裤与轻便运动鞋。":
        "A dark short-sleeve top, straight-leg blue jeans and light sneakers.",
    "深色贴身短袖 T 恤": "Fitted dark short-sleeve T-shirt",
    "蓝色直筒牛仔裤": "Straight-leg blue jeans",
    "轻便低帮运动鞋": "Light low-top sneakers",
    "排练街头": "Rehearsal street",
    "黑色背心、宽松工装裤与薄外套，方便排练活动。":
        "A black tank top, loose cargo trousers and a thin jacket, easy to move in for rehearsals.",
    "黑色背心 · 炭灰薄外套": "Black tank top · thin charcoal jacket",
    "深色宽松工装长裤": "Loose dark cargo trousers",
    "黑色运动鞋": "Black sneakers",
    "学院文艺": "Campus literary",
    "米白衬衫、深棕针织马甲与黑色长裤，清爽低调。":
        "An ivory shirt, dark brown knitted vest and black trousers; clean and understated.",
    "米白长袖衬衫 · 深棕针织马甲": "Ivory long-sleeve shirt · dark brown knitted vest",
    "黑色剪裁长裤": "Tailored black trousers",
    "深色乐福鞋": "Dark loafers",
    "舞台演出": "On stage",
    "黑色缎面上衣、高腰长裤与简洁银饰，克制的舞台感。":
        "A black satin top, high-waisted trousers and simple silver jewelry; a restrained stage look.",
    "黑色缎面贴身长袖上衣": "Fitted black satin long-sleeve top",
    "黑色高腰剪裁长裤": "High-waisted tailored black trousers",
    "深色短靴 · 简洁银饰": "Dark ankle boots · simple silver jewelry",
    # --- Dialogs, launcher and server messages ---
    "选择 Olivia 记忆离线包": "Choose an Olivia memory offline package",
    "Olivia 记忆离线包 (*.zip)|*.zip": "Olivia memory offline package (*.zip)|*.zip",
    "选择 Olivia 视频运行时根目录": "Choose the Olivia video runtime root folder",
    "选择 Olivia 视频离线包": "Choose an Olivia video offline package",
    "Olivia 视频离线包 (*.zip)|*.zip": "Olivia video offline package (*.zip)|*.zip",
    "请重启客户端以更新原创收费确认。草稿已保留。":
        "Please restart the app to update the original-song charge confirmation. Your draft is kept.",
    "本地视频暂时无法播放。": "This local video can't be played right now.",
    "LLM_API_KEY_NOT_CONFIGURED: 尚未连接 Olivia 账户 Key，请在设置的“回信服务 → Olivia 账户”中获取或导入。":
        "LLM_API_KEY_NOT_CONFIGURED: No Olivia account Key is connected yet. In Settings, open “Reply service → Olivia account” to get or import one.",
    "Olivia 自动重启失败，请手动打开": "Olivia couldn't restart automatically. Please open it manually.",
}

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


def _fill(text: str, values: dict[str, object]) -> str:
    if not values:
        return text
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def tr_in(language: str | None, zh: str, /, **values: object) -> str:
    """``zh`` in ``language`` (``"en"`` gives English where an entry exists); placeholders filled either way."""

    text = zh
    if language == ui_language.ENGLISH and isinstance(zh, str):
        english = EN.get(zh)
        if english:
            text = english
    return _fill(text, values) if isinstance(text, str) else text


def tr(zh: str, /, **values: object) -> str:
    """``zh`` in the current interface language; Chinese unless English was chosen."""

    return tr_in(ui_language.current(), zh, **values)


__all__ = ["EN", "tr", "tr_in"]
