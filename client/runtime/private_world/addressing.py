"""Conservative shape check after semantic address selection, also for legacy data."""
import re


def address_term(value):
    if isinstance(value, str):
        value = value.rstrip('，,、')
    if not isinstance(value, str) or not re.fullmatch(r'[\w\u4e00-\u9fff]{1,8}', value):
        return None
    if value in {'我', '你', '他', '她', '我们', '你们', '好的', '谢谢', '晚安', '早安'}:
        return None
    if re.search(r'明天|今天|昨天|再说|已经|收到|算账|要不|哎呀|好呀|[吗呢嘛]|^(?:我|你|她|他)(?:的|会|要|是|想|在)', value):
        return None
    return value
