"""Turn the dates a user names in Chinese into local day ranges.

"8月26号那天", "八月底那次", "上上个月26号", "上周三", "前天" each name a day
or a few days. Recall reads those days' originals directly instead of hoping
a keyword search ranks them high enough.
"""
import re
from datetime import date, datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))
_DIGITS = {'〇': 0, '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_NUM = r'[0-9]{1,2}|[一二两三四五六七八九十]{1,3}'
_WEEKDAYS = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}


def _number(text):
    if text.isdigit():
        return int(text)
    if text == '十':
        return 10
    if text.startswith('十'):
        return 10 + _DIGITS.get(text[1:], 0)
    if '十' in text:
        tens, _, ones = text.partition('十')
        return _DIGITS.get(tens, 0) * 10 + (_DIGITS.get(ones, 0) if ones else 0)
    return _DIGITS.get(text, 0)


def _shift_month(year, month, delta):
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def _month_end(year, month):
    following = date(*_shift_month(year, month, 1), 1)
    return (following - timedelta(days=1)).day


def _past_year(month, day, today):
    """A month/day without a year is the most recent one not after today."""
    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if candidate > today:
        try:
            candidate = date(year - 1, month, day)
        except ValueError:
            return None
    return candidate


def day_ranges(text, as_of):
    """Inclusive (first_day, last_day) ranges named in the text, in mention order."""
    if not isinstance(text, str) or not text:
        return []
    today = as_of.astimezone(SHANGHAI).date() if isinstance(as_of, datetime) else as_of
    found = []

    def add(first, last=None, at=0):
        if first is not None and first <= today:
            found.append((at, first, min(last or first, today)))

    # Explicit year/month/day, month/day, and month parts (初/中/底).
    for match in re.finditer(rf'(?:(\d{{4}})年)?({_NUM})月(?:份)?(?:({_NUM})(?:号|日)|(初|上旬|中旬|中|下旬|底|末))?', text):
        month = _number(match.group(2))
        if not 1 <= month <= 12 or text[max(0, match.start() - 2):match.start()] in ('明年', '后年'):
            continue
        year = int(match.group(1)) if match.group(1) else None
        if match.group(3):
            day = _number(match.group(3))
            if year:
                try:
                    add(date(year, month, day), at=match.start())
                except ValueError:
                    pass
            else:
                add(_past_year(month, day, today), at=match.start())
            continue
        base = date(year, month, 1) if year else _past_year(month, 1, today)
        if base is None:
            continue
        end = _month_end(base.year, base.month)
        part = match.group(4)
        first, last = {None: (1, end), '初': (1, 10), '上旬': (1, 10), '中': (11, 20), '中旬': (11, 20),
                       '下旬': (21, end), '底': (21, end), '末': (21, end)}[part]
        add(base.replace(day=first), base.replace(day=last), match.start())

    # Relative months: 上个月26号 / 上上个月 / 这个月3号.
    for match in re.finditer(rf'(上上|上|这|本)个?月(?:({_NUM})(?:号|日)|(初|中旬|中|底|末))?', text):
        delta = {'上上': -2, '上': -1, '这': 0, '本': 0}[match.group(1)]
        year, month = _shift_month(today.year, today.month, delta)
        end = _month_end(year, month)
        if match.group(2):
            day = _number(match.group(2))
            if 1 <= day <= end:
                add(date(year, month, day), at=match.start())
            continue
        first, last = {None: (1, end), '初': (1, 10), '中': (11, 20), '中旬': (11, 20),
                       '底': (21, end), '末': (21, end)}[match.group(3)]
        add(date(year, month, first), date(year, month, last), match.start())

    # A bare day of the current or previous month: "26号那天".
    for match in re.finditer(rf'(?<![月\d一二两三四五六七八九十])({_NUM})(?:号|日)(?:那天|那次|晚上|早上|下午)', text):
        day = _number(match.group(1))
        year, month = today.year, today.month
        if day > today.day:
            year, month = _shift_month(year, month, -1)
        if 1 <= day <= _month_end(year, month):
            add(date(year, month, day), at=match.start())

    # Weeks and days relative to today.
    for match in re.finditer(r'(上上|上|这|本)(?:周|星期|礼拜)([一二三四五六日天])', text):
        weeks = {'上上': 2, '上': 1, '这': 0, '本': 0}[match.group(1)]
        monday = today - timedelta(days=today.weekday() + 7 * weeks)
        add(monday + timedelta(days=_WEEKDAYS[match.group(2)]), at=match.start())
    for word, days in (('大前天', 3), ('前天', 2), ('昨天', 1), ('昨晚', 1)):
        for match in re.finditer(word, text):
            if word == '前天' and text[max(0, match.start() - 1):match.start()] == '大':
                continue
            add(today - timedelta(days=days), at=match.start())
    for match in re.finditer(r'(上上|上)(?:周|星期|礼拜)(?![一二三四五六日天])', text):
        weeks = {'上上': 2, '上': 1}[match.group(1)]
        monday = today - timedelta(days=today.weekday() + 7 * weeks)
        add(monday, monday + timedelta(days=6), match.start())

    ranges = []
    for _, first, last in sorted(found):
        if (first, last) not in ranges:
            ranges.append((first, last))
    return ranges[:4]
