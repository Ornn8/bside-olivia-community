"""Letter presentation metadata stays outside canonical spoken/memory text."""
import json
import re

from runtime import ui_language


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
    english = ui_language.is_english()
    signature = 'Linli' if english else '林离'
    marker = re.search(r'\[{1,2}\s*signature\s*:', text, re.IGNORECASE)
    if marker:
        footer = text[marker.start():].strip()
        match = re.fullmatch(r'\[\[signature:([^\r\n]*)\]\]', footer)
        if match and valid_signature(match[1]):
            signature = match[1]
        text = text[:marker.start()].rstrip()
    if english and signature == '林离':
        signature = 'Linli'  # the prompt's own default is the Chinese name
    # Strip only duplicate terminal signatures; preserve model-authored whitespace inside the body.
    suffix = (r'(?:\r?\n)+[ \t]*(?:[—－-]{1,2})?(?:' + re.escape(signature) + r'|林离' + ('|Linli' if english else '')
              + r')[ \t\r\n]*$')
    while re.search(suffix, text):
        text = re.sub(suffix, '', text)
    return text, signature


# Keys of a chat-style JSON object a model sometimes returns instead of letter text (2026-10-23: a letter showed
# {"delivery":"voice","text":...,"signature":...,"sticker":...} to its reader).
_ENVELOPE_KEYS = frozenset({'text', 'delivery', 'signature', 'sticker', 'title', 'listening', 'initiative', 'letter',
                            'skip', 'speech'})


def envelope_parts(text):
    """(body, signature, sticker) when a letter came back as a chat-style JSON object, else None."""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith('{') or '"text"' not in raw:
        return None
    try:
        value = json.loads(raw, strict=False)
    except ValueError:
        # Unescaped quotes inside the text break the object; take the text up to the next known key.
        match = re.match(r'\{\s*(?:"delivery"\s*:\s*"[^"]*"\s*,\s*)?"text"\s*:\s*"(.*?)"\s*,\s*"(?:signature|sticker|delivery)"',
                         raw, re.S)
        if not match:
            return None
        value = {'text': match.group(1).replace('\\n', '\n').replace('\\"', '"')}
        for key in ('signature', 'sticker'):
            found = re.search('"' + key + r'"\s*:\s*"([^"]*)"', raw[match.end(1):])
            if found:
                value[key] = found.group(1)
    if (not isinstance(value, dict) or not isinstance(value.get('text'), str) or not value['text'].strip()
            or not set(value) <= _ENVELOPE_KEYS):
        return None
    signature = value.get('signature')
    signature = signature.strip() if isinstance(signature, str) and valid_signature(signature.strip()) else None
    sticker = value.get('sticker')
    sticker = sticker if isinstance(sticker, str) and re.fullmatch(r'linli-\d{2,3}', sticker) else None
    return value['text'].strip(), signature, sticker


def unwrap_envelope(text):
    """Letter text with its sticker and signature markers again, when the model wrapped the letter in JSON."""
    parts = envelope_parts(text)
    if parts is None:
        return text
    body, signature, sticker = parts
    return body + (f'\n\n[[sticker:{sticker}]]' if sticker else '') + (f'\n[[signature:{signature}]]' if signature else '')


def display_letter(body, signature):
    if not body or not valid_signature(signature):
        return body
    return body.rstrip() + '\n\n' + signature
