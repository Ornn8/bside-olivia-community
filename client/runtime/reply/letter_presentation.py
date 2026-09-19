"""Letter presentation metadata stays outside canonical spoken/memory text."""
import re


LETTER_PRESENTATION_INSTRUCTION = (
    '信件格式：按语意自然分段，换话题或情绪转折时另起一段，段间空一行；'
    '不要把每句话都拆成一段，不要整封挤成一段，不使用Markdown标题或项目列表。'
    '正文中不写落款，正文之后独立一行输出 [[signature:落款]]；若有插画编号，落款元数据放在它之前。'
    '落款默认林离。根据当前来信及已有上下文，只有能明确确认是用户对林离本人的称呼时才采用该别称；'
    '不要采用用户自己的名字、第三人的名字、引用里的人名，不凭空创造昵称或关系身份。'
    '不确定就用林离。只输出称呼本身，不加日期、祝语或解释。'
)


def valid_signature(value):
    return isinstance(value, str) and re.fullmatch(r'[\w\u00b7· -]{1,24}', value, re.UNICODE) is not None and value.strip() == value and bool(value.strip())


def split_signature(text):
    signature = '林离'
    marker = re.search(r'\[{1,2}\s*signature\s*:', text, re.IGNORECASE)
    if marker:
        footer = text[marker.start():].strip()
        match = re.fullmatch(r'\[\[signature:([^\r\n]*)\]\]', footer)
        if match and valid_signature(match[1]):
            signature = match[1]
        text = text[:marker.start()].rstrip()
    # Strip only duplicate terminal signatures; preserve model-authored whitespace inside the body.
    suffix = r'(?:\r?\n)+[ \t]*(?:[—－-]{1,2})?(?:' + re.escape(signature) + r'|林离)[ \t\r\n]*$'
    while re.search(suffix, text):
        text = re.sub(suffix, '', text)
    return text, signature


def display_letter(body, signature):
    if not body or not valid_signature(signature):
        return body
    return body.rstrip() + '\n\n' + signature
