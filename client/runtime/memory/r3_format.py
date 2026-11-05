"""How remembered conversation is shown to the writer: dates the program computes, never the model.

* Every recalled exchange and every recent letter carries its date, weekday and the calendar days since:
  ``2026-05-23 周六 21:10 · 3天前`` (Beijing time, days counted by calendar day, never by hours).
* Words that count in days from the day a line was said (今天 今晚 今早 明天 明晚 明早 后天 大后天 昨天 昨晚 前天
  大前天) get the resolved date after them: ``明天〔=7/15周三〕``. A line sent from 00:00 to 04:59 belongs to the
  day before. Nothing else is resolved (星期几, N号, 月底, 周末 stay with the calendar).
* The turn says what time it is now and carries an 8-week calendar (Monday first) beside six short principles for
  using memory.

Switch: see r3_recall.enabled (off by default).
Any failure in here leaves the former format.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

LOCAL = timezone(timedelta(hours=8))
WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
CALENDAR_WEEKS = 8

# Offsets are counted from the day the line was said.
RELDAY = {'大前天': -3, '前天': -2, '昨天': -1, '昨晚': -1, '今天': 0, '今晚': 0, '今早': 0,
          '明天': 1, '明晚': 1, '明早': 1, '后天': 2, '大后天': 3}
# Longest first so 大前天 is found before 前天; a word already followed by its mark is left alone.
DATE_RE = re.compile('(?:' + '|'.join(sorted(RELDAY, key=len, reverse=True)) + ')(?!〔=)')
_MARK = re.compile(r'〔=\d{1,2}/\d{1,2}周[一二三四五六日]〕')
_TAG = re.compile(r'\[\d{4}-\d{2}-\d{2} 周[一二三四五六日] \d{2}:\d{2}(?: · [^\[\]\n]{1,12})?\][ \t]*')

PRINCIPLES = '\n'.join([
    '记忆使用原则：',
    '- 只有对方这句话确实需要回忆时才用记忆：问起过去的事、提到你们共同的经历、或回答必须依赖某个已知信息。'
    '打招呼、聊新话题、独立的问题，不要主动翻出记忆里的个人事；记忆里有，不等于这句话需要。',
    '- 记忆里每段前面标了日期、星期和离现在多久；话里的今天/明天/昨天等后面〔=…〕是已经换算好的日期，直接使用。'
    '其他相对说法（下周三、周末、26号等）请对照日历，按那句话说出的日期换算；拿不准时说出原话和你的理解。',
    '- 计划和约定只代表当时的打算；之后没有提到结果的，就说不知道后来怎样，不要当作已经发生或没发生。',
    '- 用户明确改口（说了“改成…”“其实是…”“不…了”）的，以改口后的为准。',
    '- 前后说法不一致又没有解释的，说明有两种说法，不替对方下结论，也不猜原因或动机。',
    '- 你自己以前说过的推测或建议，不等于用户确认的事实。',
    '- 记忆里没有的细节不要补，直接说不记得或问一句。',
])


def moment(value) -> datetime | None:
    """A time (datetime, ISO text or epoch seconds) as Beijing time; None when missing or without a zone."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, LOCAL)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(LOCAL)
    return None


def stamp(when: datetime) -> str:
    """'2026-05-23 周六 21:10' (the weekday comes from the date)."""
    return f'{when:%Y-%m-%d} {WEEKDAYS[when.weekday()]} {when:%H:%M}'


def age_label(then: date, today: date) -> str:
    """Calendar days between then and today; '' for a day after today."""
    days = (today - then).days
    if days < 0:
        return ''
    if days == 0:
        return '今天'
    if days == 1:
        return '昨天'
    if days == 2:
        return '前天'
    if days <= 13:
        return f'{days}天前'
    if days <= 56:
        return f'{days // 7}周前'
    months = (today.year - then.year) * 12 + (today.month - then.month) - (1 if today.day < then.day else 0)
    return f'{max(1, months)}个月前'


def label(when: datetime, now: datetime) -> str:
    """'2026-05-23 周六 21:10 · 3天前'."""
    age = age_label(when.date(), now.astimezone(LOCAL).date())
    return stamp(when) + (' · ' + age if age else '')


def turn_day(when: datetime) -> date:
    """The day 今天 / 明天 / 昨天 in a line refer to: a line sent 00:00-04:59 belongs to the previous day."""
    return when.date() - timedelta(days=1) if when.hour < 5 else when.date()


def day_mark(day: date) -> str:
    return f'〔={day.month}/{day.day}{WEEKDAYS[day.weekday()]}〕'


def annotate(text, day: date):
    """The text with 〔=M/D周X〕 after each day-offset word, counted from ``day``; a non-text as it is."""
    if not isinstance(text, str) or not text:
        return text
    return DATE_RE.sub(lambda match: match.group(0) + day_mark(day + timedelta(days=RELDAY[match.group(0)])), text)


def strip_marks(text):
    """Words without a day mark or date tag the writer copied from what it read."""
    if not isinstance(text, str) or ('〔=' not in text and '[' not in text):
        return text
    return _TAG.sub('', _MARK.sub('', text))


def calendar_lines(now: datetime) -> list[str]:
    """Eight weeks ending with the week of today, Monday first."""
    today = now.astimezone(LOCAL).date()
    monday = today - timedelta(days=today.weekday())
    lines = []
    for back in range(CALENDAR_WEEKS - 1, -1, -1):
        start = monday - timedelta(weeks=back)
        days = [start + timedelta(days=k) for k in range(7)]
        lines.append('  '.join(f'{d:%m-%d}{WEEKDAYS[k]}' for k, d in enumerate(days)))
    return lines


def now_note(now: datetime) -> str:
    """The system note of a turn: the principles, 「现在是」 and the calendar."""
    now = now.astimezone(LOCAL)
    return '\n'.join([PRINCIPLES, f'现在是 {stamp(now)}', f'最近{CALENDAR_WEEKS}周日历（每行一周，周一在前）：',
                      *calendar_lines(now)])


def compact_record(record, now: datetime):
    """One recalled original in the dated form; a record without a usable time, or not a dict, as it is.

    The writer reads ``when`` instead of ``occurred_at`` + ``ago``; the provenance is reduced to the source id the
    later stages match on (domain, kind and the like repeat on every record and say nothing the writer uses)."""
    if not isinstance(record, dict):
        return record
    when = moment(record.get('occurred_at'))
    if when is None:
        return record
    shaped = {}
    for key, value in record.items():
        if key in ('occurred_at', 'ago'):
            continue
        if key == 'provenance' and isinstance(value, dict):
            source = value.get('source_record_id')
            shaped[key] = {'source_record_id': source} if source else {}
        elif key == 'text':
            shaped[key] = annotate(value, turn_day(when))
        else:
            shaped[key] = value
    shaped['when'] = label(when, now)
    return shaped


def compact_group(group, now: datetime):
    """A recalled exchange in the dated form. The second and later records of the same source say nothing
    their first record has not said (same source, same moment), so they carry only citation, speaker, scope and text."""
    if not isinstance(group, list):
        return group
    shaped, seen = [], set()
    for record in group:
        record = compact_record(record, now)
        source = (record.get('provenance') or {}).get('source_record_id') if isinstance(record, dict) else None
        if source and source in seen:
            record = {key: value for key, value in record.items() if key not in ('provenance', 'when')}
        elif source:
            seen.add(source)
        shaped.append(record)
    return shaped


def annotate_recent(packet, now: datetime):
    """A ``recent_dialogue`` packet with each letter's ``when`` and its texts' day marks."""
    if not isinstance(packet, dict) or not isinstance(packet.get('letters'), list):
        return packet
    letters = []
    for letter in packet['letters']:
        said = moment(letter.get('received_at')) or moment(letter.get('sent_at')) if isinstance(letter, dict) else None
        if said is None:
            letters.append(letter)
            continue
        replied = moment(letter.get('replied_at')) or said
        shaped = dict(letter)
        for key, when in (('user_letter', said), ('linli_reply', replied)):
            shaped[key] = annotate(shaped.get(key), turn_day(when))
        shaped['when'] = label(said, now)
        letters.append(shaped)
    return {**packet, 'letters': letters}
