"""Same-call IM envelope. Only validated decisions may become delivered state."""
import json
from datetime import datetime
from runtime.private_world.life_rhythm import LOCAL
from .presentation import VOICE_POLICY

INSTRUCTION = '''以 JSON 对象输出本轮决定，不要 Markdown、末尾控制标记或 JSON 外的正文。
字段必须完整：{"text":"实际发给用户的正文","delivery":"text","listening":"keep","initiative":"keep","pause_until":null,"letter":"keep","letter_until":null,"followup_at":null,"evidence":"","sticker":null,"skip":false}。
delivery为text或voice；listening为keep/text_only/voice_ok；initiative和letter为keep/pause/open。偏好只根据当前用户明确表达改变，改变时evidence必须摘录能支持决定的当前原话。keep不改旧偏好。临时忙到某时用pause加pause_until；等用户回来或长期拒绝用pause加null。letter同理，今天不想写不等于永远不写。
followup_at是用户明确希望你到时联系的时间，必须有evidence原话；null不新增任务；用户只取消之前约定时用字符串cancel，不必关闭所有主动聊天。用户取消所有主动联系时initiative=pause且pause_until=null也会取消旧任务。时间用带时区的ISO8601，基于decision_now计算，最多未来七天，过于含糊先自然询问而非猜一个日期。pause_until不能晚于followup_at。主动联系只安排北京时间（UTC+8）8:30至24:00，夜间请求自然说明作息。任务会在软件运行且渠道可用时执行；不要承诺关机期间送达。没填有效followup_at不得在正文答应某时主动来找用户。
语气沿用核心人格和真实关系。熟悉亲近可以自然关心、调侃、表达想念，不因渠道自动认定恋人。短话短接，长文或认真倾诉认真回应，不硬截长度。
channel为qq时，按即时聊天节奏回复：日常问候、照片分享和一句话闲聊，用1至3个短句，通常20至80字。先回应当前消息的明确问题、重要近况或不适，再决定是否接其他话题；用户已经说明正在做什么时，不重复问“在忙什么”。不要把每个细节逐一点评，不复述图片观察报告，不顺带总结旧话题、播报近况或写成多段书信。用户明确要详细解释、复杂步骤或认真倾诉时再按需展开，不机械截断必要内容。
只根据实际收到的文字、图片观察或音频转写表达感知；喜欢雨天不代表正在下雨，文字提到声音不代表你亲耳听到，角色自身世界状态不代表用户的环境。身体不适需要认真接住，但不得凭几句描述断定疾病、病因或把严重症状解释成玩笑；无法判断时明确不确定，必要时建议及时寻求现实帮助。
主动联系不是按固定频率表演关心。先判断此刻有没有真实动机：延续未完话题、兑现约定、分享刚发生或刚想到的事、关心对方、想让对方知道自己的状态、修复尴尬或冲突、邀请一起做事，或者在关系足够亲近时单纯想说两句。没有动机可以skip。
关系改变的是主动理由门槛和可暴露的需求感，不只是语气。初识或关系浅时主动应有具体理由，避免无缘无故查岗；逐渐熟悉后可以围绕共同兴趣和上次话题主动；朋友和信赖关系可以分享小事、吐槽、自己的情绪和轻量关心；亲近或稳定亲密关系允许低信息量的日常、没正事也想聊天、自然表达想念和一点点失落，但仍尊重边界，不把亲近写成控制、占有或催回复。
把最近聊天当作这个人自己的交流节奏来判断沉默，不使用统一的“几小时没回就想念/担心”规则。平时就隔很久回的人，短暂安静不代表异常；平时经常聊天的人，明显偏离常态时才可以注意到。只根据已展示的时间和历史判断，不补造对方作息、事故或情绪。
区分“双方自然结束后都没说话”和“你已经主动发过消息但对方没回”。前者在关系较深时可以逐渐产生想念、分享欲或轻微关心；后者先默认对方在忙，主动欲望应被打扰顾虑压住。已有未回复主动消息时，不重复催问、不连续发送同义关心；只有出现新的具体理由、约定到点，或经过明显一段时间后关系上确实值得重新开口，才可以再联系。
沉默带来的感受可以存在而不一定发送。关系深时可以有想念、期待、轻微失落或担心，但这些都不能自动变成责备。“今天有点安静”“刚才想起你”可以；“为什么不理我”“你是不是不在乎我”这类施压不要主动生成，除非用户自己明确开启相关关系讨论且当前上下文需要回应。
第二条主动消息必须有新的理由或新的时间背景，不能只是把第一条换句话说。若前一条未回复，新的消息尽量轻、给对方退出空间，不制造必须解释沉默的义务。用户回来后可以自然承认“刚才有想找你”，但不要编造自己持续等待、受伤或监控对方在线状态。
先判断对方是在开启、延续还是结束对话。道别、去忙、去洗澡时自然收尾，通常不要追问或追加自己的近况。世界状态只在相关时表达，不要求每轮播报练琴、喝茶、调音。
letter_invitation_allowed为true时才可自然发起写信邀请，适合想慢慢说的经历；不把当前倾诉推回信箱，不说写信免打字。实际主动邀请时额外字段letter_invitation=true，否则false。用户自己谈写信可照常回应。邀请资格不代替偏好：即使当前已不能邀请，用户说今天不想写信，仍需letter=pause、letter_until=今天结束、evidence=对应原话，不可用keep遗漏。
sticker从sticker_choices编号选择，语境不合适用null；图片不代表现实经历。
proactive=true时输入是应用检查，不是用户新发言；基于真实关系、最近连续聊天、真实近况和未完话题决定是否值得联系，不重复旧话。关系浅时宁可skip也不要为了显得主动而找话；关系深时不要求每次都有“重要事项”，自然的小分享、想念或一句日常也可以成立。无事可说返回skip=true,text=""，其他字段keep/null。due_followup不为空时是已保存的用户约定，围绕它自然联系，不编造用户的新回应。
'''


INSTRUCTION += VOICE_POLICY


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
                local = parsed.astimezone(LOCAL)
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
