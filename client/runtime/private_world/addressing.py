"""Conservative shape check after semantic address selection, also for legacy data."""
import re

# A short message with no punctuation is chosen whole ("早安呀饭饭", "外边下雨了小猫咪"); keep only the term of
# address at its end. Leading greetings go, and so does a clause that ends with a particle before 1-4 last characters.
_GREETING = re.compile(r'^(?:早安|晚安|午安|早上好|晚上好|中午好|下午好|你好|您好|嗨|哈喽|嘿)[呀啊哦哟啦~～]*')
_CLAUSE = re.compile(r'^.{2,}[了呀啊吧哦哟啦](?=[\w一-鿿]{1,4}$)')
_TRAILING = re.compile(r'[呀啊哦哟啦~～]+$')


def address_term(value):
    if isinstance(value, str):
        value = value.strip().rstrip('，,、！!。.~～')
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
