"""Same-call modality selection; canonical memory contains only spoken/written words."""
from contextvars import ContextVar
import re

CURRENT = ContextVar('personal_chat_presentation', default=None)
INSTRUCTION = (
    '这是熟人之间的日常聊天，亲昵程度沿用实际关系；关系高时自然关心、逗对方、表达想念，'
    '不必每次解释关系边界，也不凭渠道自动认定恋爱关系。'
    '短消息可以短回，长文、复杂经历或认真倾诉应认真接住需要回应的内容，不能为了口语化敷衍或截短。'
    '本轮正文末尾必须另起一行写完整控制标记 [[chat:text|keep|keep|keep|no]]。'
    '五项依次为：本次载体text/voice；听语音偏好keep/text_only/voice_ok；主动打扰偏好keep/pause/open；'
    '邀请写信偏好keep/pause/open；本轮有无主动邀请写信yes/no。偏好仅随用户明确表达改变，否则keep。'
    '语音适合用户主动想听、温柔陪伴或轻松亲昵的几句话；不必固定频率或每次跟随用户消息类型。'
    '长篇分析、步骤、地址、数字等需要反复查看时优先文字，不为发语音删减重要内容。'
    '用户在开会、上课或明确不便收听时用文字，直到用户表示方便；世界状态不适合说话时也用文字。'
    'voice_available为false时用文字。不要在正文承诺语音已经送达；标记由应用处理，不会发给用户。'
    '微信渠道只发文字与表情图片，不承诺发送语音文件或拨打电话；声音体验留在林离软件。'
    'sticker_choices非空时，可按本轮语境从中选一张合适的表情，在正文后、chat标记前附 [[sticker:编号]]；'
    '不合适或没有候选就不选。图片只是表情，不代表现实发生了画面中的事。'
    '用户明确说忙、别主动打扰时，第三项用pause；明确回来或恢复时用open。'
    '偶尔可以主动邀请对方写封信：适合有想慢慢讲的经历、值得留下的话，或很久没写信；'
    '这只是她想收到信的心意，不是催任务，不能把眼前的长文或倾诉推回信箱，仍须先认真回应。'
    '邀请写信要明确说写封信；写信也要打字，不能把它说成免打字、省时间的替代方式。'
    'letter_invitation_allowed为true才可自行发起邀请，false时不主动提，但用户主动谈写信仍可自然回应。'
    '确实主动邀请时第五项用yes；用户拒绝或暂时不想写时第四项用pause，明确愿意恢复时用open。'
    '两个偏好独立判断：用户两件都拒绝时示例 [[chat:text|keep|pause|pause|no]]。'
    '这些标记也由应用去除。没有合适契机就不邀请，不为了用功能而生造话题。'
    'proactive为true时，当前输入仅是应用唤醒提示，不是用户发言；结合真实记忆和世界状态判断是否有新鲜、自然的话想主动说。'
    '不要重新回复上次已回答的话，不编造用户刚说话、共同经历或承诺。不值得打扰时只输出 [[skip]]。'
)


def parse_social(text, row, allowed):
    combined = _chat(text)
    if combined:
        _, _, initiative, letter, invited = combined[-1]
        if initiative != 'keep':
            row['initiative_preference'] = initiative
        if letter != 'keep':
            row['letter_preference'] = letter
        row['letter_invitation'] = bool(allowed and invited == 'yes')
        return re.sub(r'\[\[chat:[^\]\r\n]*\]\]', '', text).strip()
    for key, pattern in [('initiative_preference', r'\[\[initiative:(pause|open)\]\]'),
                         ('letter_preference', r'\[\[letter:(pause|open)\]\]')]:
        matches = re.findall(pattern, text)
        if matches:
            row[key] = matches[-1]
    row['letter_invitation'] = bool(allowed and '[[letter:invite]]' in text)
    return re.sub(r'\[\[(?:initiative|letter):[^\]\r\n]*\]\]', '', text).strip()


def _chat(text):
    return re.findall(r'\[\[chat:(text|voice)\|(keep|text_only|voice_ok)\|(keep|pause|open)\|(keep|pause|open)\|(yes|no)\]\]', text)


def parse(text, previous='voice_ok'):
    marker = re.findall(r'\[\[delivery:(text|voice)\|(keep|text_only|voice_ok)\]\]', text)
    if _chat(text):
        marker.append(_chat(text)[-1][:2])
    clean = re.sub(r'\[\[delivery:[^\]\r\n]*\]\]', '', text).strip()
    clean = re.sub(r'\[\[chat:[^\]\r\n]*\]\]', '', clean).strip()
    mode, preference = marker[-1] if marker else ('text', 'keep')
    preference = previous if preference == 'keep' else preference
    return clean, 'text' if preference == 'text_only' else mode, preference
