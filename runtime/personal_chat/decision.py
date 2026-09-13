"""Same-call IM envelope. Only validated decisions may become delivered state."""
import json
from datetime import datetime

INSTRUCTION = '''以 JSON 对象输出本轮决定，不要 Markdown、末尾控制标记或 JSON 外的正文。
字段必须完整：{"text":"实际发给用户的正文","delivery":"text","listening":"keep","initiative":"keep","pause_until":null,"letter":"keep","letter_until":null,"followup_at":null,"evidence":"","sticker":null,"skip":false}。
delivery为text或voice；listening为keep/text_only/voice_ok；initiative和letter为keep/pause/open。偏好只根据当前用户明确表达改变，改变时evidence必须摘录能支持决定的当前原话。keep不改旧偏好。临时忙到某时用pause加pause_until；等用户回来或长期拒绝用pause加null。letter同理，今天不想写不等于永远不写。
followup_at是用户明确希望你到时联系的时间，必须有evidence原话；null不新增任务；用户只取消之前约定时用字符串cancel，不必关闭所有主动聊天。用户取消所有主动联系时initiative=pause且pause_until=null也会取消旧任务。时间用带时区的ISO8601，基于decision_now计算，最多未来七天，过于含糊先自然询问而非猜一个日期。pause_until不能晚于followup_at。主动联系只安排本地8:30至24:00，夜间请求自然说明作息。任务会在软件运行且渠道可用时执行；不要承诺关机期间送达。没填有效followup_at不得在正文答应某时主动来找用户。
语气沿用核心人格和真实关系。熟悉亲近可以自然关心、调侃、表达想念，不因渠道自动认定恋人。短话短接，长文或认真倾诉认真回应，不硬截长度。
先判断对方是在开启、延续还是结束对话。道别、去忙、去洗澡时自然收尾，通常不要追问或追加自己的近况。世界状态只在相关时表达，不要求每轮播报练琴、喝茶、调音。
微信只发文字和表情，不发音频文件或拨电话；voice_available为false必须text。QQ语音适合轻松陪伴或明确想听，复杂分析优先文字，不能为了语音删减回应；不便收听时listening=text_only，方便时voice_ok。
letter_invitation_allowed为true时才可自然发起写信邀请，适合想慢慢说的经历；不把当前倾诉推回信箱，不说写信免打字。实际主动邀请时额外字段letter_invitation=true，否则false。用户自己谈写信可照常回应。邀请资格不代替偏好：即使当前已不能邀请，用户说今天不想写信，仍需letter=pause、letter_until=今天结束、evidence=对应原话，不可用keep遗漏。
sticker从sticker_choices编号选择，语境不合适用null；图片不代表现实经历。
proactive=true时输入是应用检查，不是用户新发言；基于真实近况和未完话题决定是否值得联系，不重复旧话。无事可说返回skip=true,text=""，其他字段keep/null。due_followup不为空时是已保存的用户约定，围绕它自然联系，不编造用户的新回应。
'''


def decode(raw, *, user, now, proactive=False):
    try:
        data = json.loads(raw)
        required = {'text','delivery','listening','initiative','pause_until','letter','letter_until',
                    'followup_at','evidence','sticker','skip'}
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - {'letter_invitation'}:
            raise ValueError()
        if not isinstance(data['text'], str) or type(data['skip']) is not bool:
            raise ValueError()
        if data['delivery'] not in {'text','voice'} or data['listening'] not in {'keep','text_only','voice_ok'}:
            raise ValueError()
        if data['initiative'] not in {'keep','pause','open'} or data['letter'] not in {'keep','pause','open'}:
            raise ValueError()
        if type(data.get('letter_invitation', False)) is not bool or not isinstance(data['evidence'], str):
            raise ValueError()
        if data['sticker'] is not None and not isinstance(data['sticker'], str):
            raise ValueError()
        changes = any(data[k] != 'keep' for k in ('listening','initiative','letter')) or data['followup_at'] is not None
        if changes and (proactive or not data['evidence'].strip() or data['evidence'] not in user):
            raise ValueError()
        data['followup_cancel'] = data['followup_at'] == 'cancel'
        if data['followup_cancel']:
            data['followup_at'] = None
        for key in ('pause_until','letter_until','followup_at'):
            value = data[key]
            if value is None:
                continue
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or not now < parsed.timestamp() <= now + 7 * 86400:
                raise ValueError()
            data[key] = parsed.timestamp()
            if key == 'followup_at':
                local = parsed.astimezone()
                if local.hour * 60 + local.minute < 510:
                    raise ValueError()
        if data['followup_at'] and data['initiative'] == 'pause' and data['pause_until'] is None:
            data['pause_until'] = data['followup_at']
        if data['pause_until'] and data['initiative'] != 'pause' or data['letter_until'] and data['letter'] != 'pause':
            raise ValueError()
        if data['followup_at'] and data['pause_until'] and data['pause_until'] > data['followup_at']:
            raise ValueError()
        if data['skip'] and (not proactive or data['text'].strip()) or not data['skip'] and not data['text'].strip():
            raise ValueError()
        if '[[' in data['text'] or ']]' in data['text']:
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError('PERSONAL_CHAT_DECISION_INVALID') from exc
