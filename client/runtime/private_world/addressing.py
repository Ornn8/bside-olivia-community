"""Conservative shape check after semantic address selection, also for legacy data."""
import hashlib
import re

# Who calls whom. The user's own name (user_self) is not an address and keeps only the shape check.
JUDGED_KINDS = ('user_calls_linli', 'linli_calls_user')
# A quote of an address kind is kept as written and shown through the shape check only until the daily judgment
# (addressing_judgment) has run once: a two-character reply such as 好吧 passed the shape check (2026-10-19).
CLEAN_MAX = 0
# Beyond this a quote is a whole passage, not a name with some words around it: shape check only.
QUOTE_MAX = 40
_PUNCTUATION = '，,、！!。.~～'

# A short message with no punctuation is chosen whole ("早安呀饭饭", "外边下雨了小猫咪"); keep only the term of
# address at its end. Leading greetings go, and so does a clause that ends with a particle before 1-4 last characters.
_GREETING = re.compile(r'^(?:早安|晚安|午安|早上好|晚上好|中午好|下午好|你好|您好|嗨|哈喽|嘿)[呀啊哦哟啦~～]*')
_CLAUSE = re.compile(r'^.{2,}[了呀啊吧哦哟啦](?=[\w一-鿿]{1,4}$)')
_TRAILING = re.compile(r'[呀啊哦哟啦~～]+$')


def normalize(value):
    """The quote without surrounding spaces and closing punctuation; '' for anything but text."""
    return value.strip().rstrip(_PUNCTUATION).strip() if isinstance(value, str) else ''


def needs_judgment(kind, value):
    """A quote of an address kind: a greeting or a clause may surround the name, or there may be none at all."""
    return kind in JUDGED_KINDS and CLEAN_MAX < len(normalize(value)) <= QUOTE_MAX


def quote_key(kind, normalized):
    return hashlib.sha256(f'{kind}:{normalized}'.encode('utf-8')).hexdigest()[:32]


def address_term(value):
    if isinstance(value, str):
        value = value.strip().rstrip(_PUNCTUATION)
        value = _GREETING.sub('', value)
        value = _CLAUSE.sub('', value)
        value = _TRAILING.sub('', value)
    if not isinstance(value, str) or not re.fullmatch(r'[\w一-鿿]{1,8}', value):
        return None
    if value in {'我', '你', '他', '她', '我们', '你们', '好的', '谢谢', '晚安', '早安'}:
        return None
    if re.search(r'明天|今天|昨天|再说|已经|收到|算账|要不|哎呀|好呀|下雨|[吗呢嘛]|^(?:我|你|她|他)(?:的|会|要|是|想|在)', value):
        return None
    return value
