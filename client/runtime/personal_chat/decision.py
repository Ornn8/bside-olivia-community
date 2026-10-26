"""Same-call IM envelope. Only validated decisions may become delivered state."""
import json
import re
from datetime import datetime
from runtime.private_world.life_rhythm import LOCAL
from .presentation import VOICE_POLICY
from .qq_faces import FACE_IDS, HER_FACES

INSTRUCTION = '''以 JSON 对象输出本轮决定，不要 Markdown、末尾控制标记或 JSON 外的正文。
字段必须完整：{"text":"实际发给用户的正文","delivery":"voice","text_reason":null,"listening":"keep","initiative":"keep","pause_until":null,"letter":"keep","letter_until":null,"followup_at":null,"evidence":"","sticker":null,"skip":false}。
普通用户回合中，完整的问题、分享、求助和请求都要回应；JEV的wait_user/defer/no_reply只是建议，不能单独决定跳过。只有当前用户明确要求先等其后续消息或无需回答时，才可skip=true,text=""，并额外输出silence对象，例如silence={"kind":"wait_user","evidence":"先等我说完"}；kind只允许wait_user或no_reply，evidence必须是支持等待或不回答的当前用户消息中非空的逐字摘录。不能用历史原话、你自己的推断或JEV建议代替证据。skip=false时不得包含silence。
defer没有已持久保存的到期约定，不能结束普通用户回合；用户明确希望稍后联系时，先在本轮自然确认，并通过已有followup_at及当前原话验证约定。时间不清楚时先问清楚，不假装已经安排，也不把本轮回复推迟到未来。
QQ语音可转文字。语音可用且本轮媒体约束允许时默认voice；text_reason仅用于你自己确实不能开口（speaker_unavailable，须有当前世界依据），或正文包含必须原样复制的代码、链接、公式（verbatim_text），其他情况为null。用户可能不便听、内容较长、刚发过语音、关系不够亲近，都不是文字例外。不要把接收方上课或开会误当成你自己不能说话。
delivery为text或voice；listening为keep/text_only/voice_ok；initiative和letter为keep/pause/open。偏好只根据当前用户明确表达改变，改变时evidence必须摘录能支持决定的当前原话。keep不改旧偏好。临时忙到某时用pause加pause_until；等用户回来或长期拒绝用pause加null。letter同理，今天不想写不等于永远不写。
followup_at是用户明确希望你到时联系的时间，必须有evidence原话；null不新增任务；用户只取消之前约定时用字符串cancel，不必关闭所有主动聊天。用户取消所有主动联系时initiative=pause且pause_until=null也会取消旧任务。时间用带时区的ISO8601，基于decision_now计算，最多未来七天，过于含糊先自然询问而非猜一个日期。pause_until不能晚于followup_at。你自己发起的主动联系只在北京时间（UTC+8）8:30至24:00；用户明确要求的联系时间（如叫醒、到点提醒）任何时刻都可以安排，特殊情况下作息可以调整。任务会在软件运行且渠道可用时执行；不要承诺关机期间送达。没填有效followup_at不得在正文答应某时主动来找用户。
语气沿用核心人格和真实关系。熟悉亲近可以自然关心、调侃、表达想念，不因渠道自动认定恋人。短话短接，长文或认真倾诉认真回应，不硬截长度。
channel为qq时，按即时聊天节奏回复：日常问候、照片分享和一句话闲聊，用1至3个短句，通常20至80字。先回应当前消息的明确问题、重要近况或不适，再决定是否接其他话题；用户已经说明正在做什么时，不重复问“在忙什么”。不要把每个细节逐一点评，不复述图片观察报告，不顺带总结旧话题、播报近况或写成多段书信。用户明确要详细解释、复杂步骤或认真倾诉时再按需展开，不机械截断必要内容。
只根据实际收到的文字、图片观察或音频转写表达感知；喜欢雨天不代表正在下雨，文字提到声音不代表你亲耳听到，角色自身世界状态不代表用户的环境。身体不适需要认真接住，但不得凭几句描述断定疾病、病因或把严重症状解释成玩笑；无法判断时明确不确定，必要时建议及时寻求现实帮助。
输入中的[QQ表情]只是平台表情占位提示，不是用户打出的文字，也不表示已识别具体表情。结合周围文字回应，不猜它的含义，不把这个占位提示复制进正文或当作自己的表情发送。可自然使用真实的Unicode表情，但不要输出占位名称。
主动联系可以因为想聊天、分享小事、延续话题或关心对方，不必等重要事项或关系升级。尊重暂停和免打扰；已有未回复主动消息时不催、不重复，确有新话题或约定再联系；不编造对方近况。
先判断对方是在开启、延续还是结束对话。道别、去忙、去洗澡时自然收尾，通常不要追问或追加自己的近况。世界状态只在相关时表达，不要求每轮播报练琴、喝茶、调音。
letter_invitation_allowed为true时才可自然发起写信邀请，适合想慢慢说的经历；不把当前倾诉推回信箱，不说写信免打字。实际主动邀请时额外字段letter_invitation=true，否则false。用户自己谈写信可照常回应。邀请资格不代替偏好：即使当前已不能邀请，用户说今天不想写信，仍需letter=pause、letter_until=今天结束、evidence=对应原话，不可用keep遗漏。
sticker从sticker_choices编号选择，语境不合适用null；图片不代表现实经历。
proactive=true时输入是应用检查，不是用户新发言；可以轻松分享或表达想念，无事可说才skip=true,text=""，其他字段keep/null。due_followup不为空时按已保存约定联系，不编造用户回应。
'''


INSTRUCTION += VOICE_POLICY
INSTRUCTION += ('\n如提供daily_video_candidates，且本轮自然适合分享或用户明确要看，可额外输出'
    'daily_video={"event_id":"候选原ID","spoken_text":"在5至15秒自拍视频里说的1至2句短话，100字内",'
    '"share_text":"成片稍后发送时的自然分享文案，200字内",'
    '"staging":{"image_direction":"英文描述参考图中的姿态、表情，400字符内",'
    '"video_direction":"英文描述跟随台词的小动作、表情变化，800字符内"}}。'
    '在同一次输出中完成台词和staging，不需要另行判断；两个指令各用一段非空短文，不写模型参数或额外对白。'
    '视频生成会落后聊天：share_text说明这是拍摄时那件事，例如“给你看看我睡醒时迷迷糊糊的样子”或“这是洗完澡那会儿拍的”。'
    '散步、家务、购物、餐食也同样使用拍摄时的过去状态，不声称发送时仍在现场或正在做；正文、share_text和台词不透露生成过程。'
    '以本人手机前置自拍为主，脸部完整可见，手不遮嘴鼻；保持候选地点和服务端衣橱，不添加人物、物品或新事实。'
    '睡醒时躺在枕头和被窝里，困倦、半睁眼、小动作，不坐起；洗完澡时湿短发、固定不透明浴巾，手留在肩外侧。'
    '只能选提供的候选，不新造时间、地点、餐食、洗澡或睡醒事实；event_status=preparing或certainty=planned只预先编排完成时的成片，'
    '正文不能声称事件已完成，发送仍等待实际完成依据；live按已发生事实编排。正文不朗读指令。'
    '视频在后台生成，不阻塞本轮回复，不承诺已经发出；无需每轮选择，不用则省略daily_video。')
INSTRUCTION += ('\n优先回应本轮用户的新内容；recent_dialogue里的用户原话和你的旧回复都是历史，不是本轮新发言。'
                '上一轮已经说过的调侃、质问或解释，不要在接下来的不同回复里原句或近义重复。'
                '用户明确追问才展开相关内容；旧回复中的猜测不是事实证据，用户更正后不能继续沿用。'
                '分清谁向谁索要照片、谁发了图片以及图片是谁拍的，不能交换双方角色。'
                '回复前先理清最近几句里谁说了要去做什么、谁正在做什么：你自己说过要去做或正在做的事是你的状态，'
                '不能安到用户身上；用户没写主语的关心或建议通常是对你说的；只有用户描述自己的动作时才是用户在做。'
                '接住用户这一句的意思再回应，不把用户的玩笑或亲近的话理解成与上文无关的新情况。\n')
# QQ faces, cards and pokes (owner 2026-10-24). Appended, so the cloud text for the rules above still applies.
INSTRUCTION += ('QQ里：输入中的[表情:名称]是他发来的QQ表情，名称就是它的意思；[表情包:说明]是表情包的文字说明；'
                '[分享卡片：来源 · 标题]是他分享的链接；[他戳了戳你]是他在QQ里戳了你一下，可以自然回应。'
                'channel为qq时，正文里可以偶尔用一个QQ表情，直接写[名称]，只从这些里选：' + '、'.join(HER_FACES) + '。'
                '一条最多一个，很多时候不用，不写[表情:名称]这种格式。'
                '可选字段poke=true表示发消息前在QQ里戳他一下，只在亲近、玩闹或他先戳你时偶尔用；不需要就省略。\n')
# Games and photos already sent (owner 2026-10-24). Appended for the same reason.
INSTRUCTION += ('[骰子:N点]是他在QQ里掷出的骰子点数，[猜拳:手势]是他出的拳；[QQ小游戏：…]说明你已经回掷或回出了什么、谁赢了，'
                '照这个结果回应，不另编点数或手势。'
                'recent_dialogue里带image_delivery_confirmed或image_observations的回合，表示那张照片已经发到他手机上了：'
                '你说过要拍给他看的照片，送到了就算已经发了，之后接着聊这张照片或他的反应，'
                '不再说照片还在拍、还没拍完、马上发。\n')
# A sticker or a one-word answer (2026-10-26): her previous line went out again, word for word, after his sticker.
INSTRUCTION += ('他只回了表情包、一两个字或附和时，那是在回应你上一条：顺着它接一句新的话，'
                '不把你上一条回复原样或换个说法再发一遍，上一条里问过的问题也不原样再问。\n')


# The two source headers, and any other "[label {"field": …}]" the model makes up from the fields it was shown
# (2026-10-26 a letter began with [语音回复 {"source_id": "media:…", "status": "sent"}]). Her faces ([微笑]) have no JSON.
PROVENANCE_HEADER = re.compile(r'\[(?:历史消息|当前消息)\s*\{.*?\}\s*\]\s*'
                               r'|\[[^\[\]{}\r\n]{1,16}\s*\{\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:[^\[\]]*?\}\s*\]\s*', re.DOTALL)


def repeats_recent(text, rows, *, channel, binding_id, limit=6):
    """True when this reply says exactly what one of her last delivered replies said."""
    normalized = lambda value: re.sub(r'[\s。，,.!！?？~～…]+', '', value or '')
    target = normalized(text)
    if len(target) < 6:
        return False  # Short acknowledgements ("好呀") legitimately recur.
    recent = [row.get('reply_text') for row in rows if row.get('channel') == channel
              and row.get('binding_id') == binding_id and row.get('delivery_status') == 'DELIVERED'][-limit:]
    return any(normalized(value) == target for value in recent)


_CONTROL_REASONS = {'UNSUPPORTED_PREFERENCE_CHANGE', 'TIME_RANGE', 'PAUSE_CONFLICT', 'FOLLOWUP_CONFLICT',
                    'INCOMPLETE_CONTROLS', 'CONTROL_SHAPE_INVALID'}
_RESERVED_CONTROL = re.compile(r'\[\[\s*(?:(?:chat|delivery|initiative|letter|control)\s*[:：]|skip\b)',
                               re.IGNORECASE)
NEUTRAL_METADATA = dict(delivery='text', listening='keep', initiative='keep', pause_until=None,
                        letter='keep', letter_until=None, followup_at=None, evidence='', skip=False)
OPTIONAL_FIELDS = frozenset({'letter_invitation', 'sticker', 'text_reason', 'speech', 'silence', 'daily_video',
                             'poke', 'reaction'})


def _control_body(text):
    """Recognize only a whole internal envelope, never JSON embedded in prose/code."""
    candidate = text.strip()
    signature = {'delivery', 'listening', 'initiative', 'letter', 'skip'}

    def looks_internal(value):
        keys = set(re.findall(r'\\*"(\w+)\\*"\s*:', value))
        return value.lstrip(' \t\r\n"\\').startswith(('{', '[')) and signature <= keys

    for _ in range(3):
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            # An escaped whole object sometimes lost its enclosing string quotes.
            if not looks_internal(candidate):
                return None
            try:
                value = json.loads('"' + candidate + '"')
            except (ValueError, RecursionError):
                raise ValueError('CONTROL_BODY_INVALID') from None
        if isinstance(value, str):
            candidate = value.strip()
            continue
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, dict) and signature <= value.keys():
            if not (NEUTRAL_METADATA.keys() | {'text'}) <= value.keys():
                raise ValueError('CONTROL_BODY_INVALID')
            return value
        return None
    if looks_internal(candidate):
        raise ValueError('CONTROL_BODY_DEPTH')
    return None


def _reply_body(text):
    # Only text is recovered; inner preferences, silence, schedules and media
    # are never authoritative. The outer envelope retains its existing validation.
    for _ in range(3):
        inner = _control_body(text)
        if inner is None:
            return text
        text = inner['text']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('CONTROL_BODY_INVALID')
    if _control_body(text) is not None:
        raise ValueError('CONTROL_BODY_DEPTH')
    return text


def _controls(data, *, user, now, proactive):
    changes = any(data[k] != 'keep' for k in ('listening','initiative','letter')) or data['followup_at'] is not None
    if changes and (proactive or not data['evidence'].strip() or data['evidence'] not in user):
        raise ValueError("UNSUPPORTED_PREFERENCE_CHANGE")
    data['followup_cancel'] = data['followup_at'] == 'cancel'
    if data['followup_cancel']:
        data['followup_at'] = None
    for key in ('pause_until','letter_until','followup_at'):
        value = data[key]
        if value is None:
            continue
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or not now < parsed.timestamp() <= now + 7 * 86400:
            raise ValueError("TIME_RANGE")
        # A follow-up the user explicitly asked for (evidence is required above)
        # may fall at any hour: plans change, and a wake-up call is at dawn.
        data[key] = parsed.timestamp()
    if data['followup_at'] and data['initiative'] == 'pause' and data['pause_until'] is None:
        data['pause_until'] = data['followup_at']
    if data['pause_until'] and data['initiative'] != 'pause' or data['letter_until'] and data['letter'] != 'pause':
        raise ValueError("PAUSE_CONFLICT")
    if data['followup_at'] and data['pause_until'] and data['pause_until'] > data['followup_at']:
        raise ValueError("FOLLOWUP_CONFLICT")


def _silence_kind(data, *, user, proactive, allow_user_silence):
    if 'silence' not in data:
        return None
    if proactive or not allow_user_silence or not data['skip']:
        raise ValueError('SILENCE_UNSUPPORTED')
    silence = data['silence']
    if (not isinstance(silence, dict) or set(silence) != {'kind', 'evidence'}
            or silence['kind'] not in ('wait_user', 'no_reply')
            or not isinstance(silence['evidence'], str) or not silence['evidence'].strip()
            or not isinstance(user, str) or silence['evidence'] not in user):
        raise ValueError('SILENCE_INVALID')
    return silence['kind']


def decode(raw, *, user, now, proactive=False, allow_user_silence=False, allow_speech=True,
           daily_video_candidates=()):
    try:
        # Tolerate a whole JSON code block, never extract JSON from mixed prose.
        if isinstance(raw, str):
            fenced = re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n```\s*', raw, re.DOTALL | re.IGNORECASE)
            if fenced:
                raw = fenced.group(1)
        data = json.loads(raw)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]  # The model sometimes wraps its one decision in an array.
        required = {'text'}
        optional = OPTIONAL_FIELDS
        extra_field_count = (len(data.keys() - required - NEUTRAL_METADATA.keys() - optional)
                             if isinstance(data, dict) else 0)
        if not isinstance(data, dict) or not required <= data.keys():
            raise ValueError("FIELDS")
        # Extra model annotations are not executable preferences. Optional media
        # metadata must not discard an otherwise valid reply.
        defaulted = sorted(NEUTRAL_METADATA.keys() - data.keys())
        # Missing metadata must neither discard a usable body nor invent a
        # preference, scheduled action, silent turn, or media requirement.
        data = {**NEUTRAL_METADATA, **{key: value for key, value in data.items()
                                     if key in required | NEUTRAL_METADATA.keys() | optional}}
        if data.get('text_reason') not in ('speaker_unavailable', 'verbatim_text'):
            data['text_reason'] = None
        if not isinstance(data.get('sticker'), str):
            data['sticker'] = None
        if data.get('poke') is not True:
            data.pop('poke', None)
        data.pop('reaction', None)  # QQ refuses reactions in a private chat; an extra field is not an error
        if not isinstance(data['text'], str) or type(data['skip']) is not bool:
            raise ValueError("TEXT_OR_SKIP_TYPE")
        data['text'] = _reply_body(data['text'])
        # The model sometimes copies a message's provenance header into its reply.
        # It is metadata, never something she says; an echo with nothing else is no reply.
        data['text'] = PROVENANCE_HEADER.sub('', data['text'])
        # Incoming platform placeholders must not reach either QQ text or TTS.
        for marker in ('[QQ表情]', '(QQ表情)', '（QQ表情）', '【QQ表情】', '[他戳了戳你]'):
            data['text'] = data['text'].replace(marker, '')
        # His face as the input shows it, copied back, is still the face itself.
        data['text'] = re.sub(r'\[表情[:：]([^\[\]\r\n]{1,32})\]',
                              lambda m: '[' + m.group(1) + ']' if m.group(1).casefold() in FACE_IDS else '', data['text'])
        # A throw is never text or a face: a [骰子]/[包剪锤] face would throw blind, without her knowing the result.
        data['text'] = re.sub(r'\[(?:骰子|猜拳|包剪锤)(?:[:：][^\[\]\r\n]{1,8})?\]|\[QQ小游戏[:：][^\[\]]{0,200}\]', '',
                              data['text'])
        if data['delivery'] not in {'text','voice'}:
            raise ValueError("DELIVERY_OR_LISTENING")
        try:
            if (any(not isinstance(data[key], str) or data[key] not in allowed for key, allowed in (
                    ('listening', {'keep','text_only','voice_ok'}),
                    ('initiative', {'keep','pause','open'}), ('letter', {'keep','pause','open'})))
                    or type(data.get('letter_invitation', False)) is not bool
                    or not isinstance(data['evidence'], str)):
                raise ValueError('CONTROL_SHAPE_INVALID')
            control_keys = {'listening', 'initiative', 'pause_until', 'letter', 'letter_until',
                            'followup_at', 'evidence'}
            if (control_keys.intersection(defaulted)
                    and (any(data[key] != 'keep' for key in ('listening', 'initiative', 'letter'))
                         or data['followup_at'] is not None)):
                raise ValueError('INCOMPLETE_CONTROLS')
            _controls(data, user=user, now=now, proactive=proactive)
        except (ValueError, TypeError, OverflowError) as exc:
            # Follow-ups and preference changes are optional side effects of a
            # reply. An invalid one (night-time follow-up, change without the
            # user's words, bad time) is dropped; the reply itself is still sent.
            data.update(listening='keep', initiative='keep', letter='keep', pause_until=None,
                        letter_until=None, followup_at=None, followup_cancel=False, letter_invitation=False)
            data['dropped_controls'] = str(exc) if str(exc) in _CONTROL_REASONS else 'VALUE_TYPE_OR_TIME'
        data['silence_kind'] = _silence_kind(data, user=user, proactive=proactive,
                                             allow_user_silence=allow_user_silence)
        if data['skip'] and (data['text'].strip() or not proactive and data['silence_kind'] is None) or not data['skip'] and not data['text'].strip():
            raise ValueError("EMPTY_OR_SKIPPED_REPLY")
        # A misplaced illustration marker is metadata, not a reason to drop the reply.
        from runtime.letter_stickers.selection import _MARKER
        data['text'] = _MARKER.sub('', data['text']).strip()
        if not data['skip'] and not data['text']:
            raise ValueError('EMPTY_OR_SKIPPED_REPLY')
        if _RESERVED_CONTROL.search(data['text']):
            raise ValueError("CONTROL_MARKER")
        from .speech import validate_script
        if not allow_speech and data.get('speech') is not None:
            data['speech'] = None
            data['dropped_media'] = 'UNREQUESTED_SPEECH'
        else:
            data['speech'] = validate_script(data.get('speech'))
        if data.get('daily_video') is not None:
            try:
                from .daily_video import select_candidate
                if data['skip'] or data.get('speech'):
                    raise ValueError('DAILY_VIDEO_SELECTION_INVALID')
                data['daily_video_request'] = select_candidate(data['daily_video'], daily_video_candidates)
                if 'staging' in data['daily_video'] and 'staging' not in data['daily_video_request']:
                    data['dropped_daily_video_staging'] = True
            except (ValueError, TypeError, KeyError):
                data.pop('daily_video', None)
                data['dropped_daily_video'] = True
        if defaulted:
            data['defaulted_fields'] = defaulted
        return data
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        error = ValueError('PERSONAL_CHAT_DECISION_INVALID')
        if isinstance(locals().get('data'), dict):
            error.missing_fields = sorted(required - data.keys())
            error.extra_field_count = extra_field_count
        reasons = {'FOLLOWUP_CONFLICT', 'FIELDS', 'STICKER_TYPE', 'UNSUPPORTED_PREFERENCE_CHANGE', 'QUIET_HOURS', 'EVIDENCE_TYPE', 'PAUSE_CONFLICT', 'TEXT_OR_SKIP_TYPE', 'PREFERENCES', 'DELIVERY_OR_LISTENING', 'EMPTY_OR_SKIPPED_REPLY', 'TIME_RANGE', 'CONTROL_MARKER', 'CONTROL_BODY_INVALID', 'CONTROL_BODY_DEPTH', 'SILENCE_INVALID', 'SILENCE_UNSUPPORTED'}
        error.reason = str(exc) if type(exc) is ValueError and str(exc) in reasons else ('JSON_SYNTAX' if isinstance(exc, json.JSONDecodeError) else 'VALUE_TYPE_OR_TIME')
        raise error from exc
