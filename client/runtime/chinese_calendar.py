"""Offline Chinese festival facts for a character living on Beijing time."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta

from ._chinese_calendar_data import LUNAR_MONTH_STARTS, QINGMING_DATES


_STARTS = tuple((date.fromisoformat(day), month, leap) for day, month, leap in LUNAR_MONTH_STARTS)
_START_DATES = tuple(row[0] for row in _STARTS)
_LUNAR_FESTIVALS = {
    (1, 1): "春节",
    (1, 15): "元宵节",
    (5, 5): "端午节",
    (7, 7): "七夕节",
    (8, 15): "中秋节",
    (9, 9): "重阳节",
    (12, 8): "腊八节",
}
_SOLAR_FESTIVALS = {
    (1, 1): "元旦",
    (5, 1): "劳动节",
    (10, 1): "国庆节",
}
_MONTH_NAMES = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")
_DAY_NAMES = (
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
)
_FIRST_YEAR = 2024
_LAST_YEAR = max(QINGMING_DATES)


def _lunar_date(day: date) -> tuple[int, int, bool] | None:
    if not _FIRST_YEAR <= day.year <= _LAST_YEAR:
        return None
    index = bisect_right(_START_DATES, day) - 1
    if index < 0:
        return None
    start, month, leap = _STARTS[index]
    lunar_day = (day - start).days + 1
    if not 1 <= lunar_day <= 30:
        return None
    return month, lunar_day, leap


def festivals_on(day: date) -> tuple[str, ...]:
    """Return festival names for one Chinese civil date, never vacation dates."""
    if not _FIRST_YEAR <= day.year <= _LAST_YEAR:
        return ()
    names: list[str] = []
    solar_name = _SOLAR_FESTIVALS.get((day.month, day.day))
    if solar_name:
        names.append(solar_name)
    if QINGMING_DATES[day.year] == day.isoformat():
        names.append("清明节")
    lunar = _lunar_date(day)
    if lunar:
        month, lunar_day, leap = lunar
        if not leap:
            name = _LUNAR_FESTIVALS.get((month, lunar_day))
            if name:
                names.append(name)
            tomorrow = _lunar_date(day + timedelta(days=1))
            if month == 12 and tomorrow == (1, 1, False):
                names.append("除夕")
    return tuple(names)


def calendar_context(day: date) -> dict[str, object] | None:
    """Bounded date grounding for a reply; omit unsupported years entirely."""
    lunar = _lunar_date(day)
    if lunar is None:
        return None
    month, lunar_day, leap = lunar
    upcoming = [
        {"date": future.isoformat(), "festival": name}
        for offset in range(1, 8)
        for future in (day + timedelta(days=offset),)
        for name in festivals_on(future)
    ]
    return {
        "date": day.isoformat(),
        "lunar_date": f"农历{'闰' if leap else ''}{_MONTH_NAMES[month - 1]}月{_DAY_NAMES[lunar_day - 1]}",
        "festivals_today": festivals_on(day),
        "upcoming_festivals": upcoming,
        "instruction": "仅按这里的北京时间日期判断近期中国节日；不据此推断放假安排。仅在当前话题相关时自然带过，已提过就不要机械重复。",
    }
