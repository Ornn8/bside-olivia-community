"""Checks for the offline, Beijing-date holiday facts used in replies."""

from datetime import date

from runtime.chinese_calendar import calendar_context, festivals_on


def test_mid_autumn_is_the_lunar_date_not_the_previous_solar_day():
    assert festivals_on(date(2026, 9, 24)) == ()
    assert festivals_on(date(2026, 9, 25)) == ("中秋节",)
    assert calendar_context(date(2026, 9, 24))["upcoming_festivals"][0] == {
        "date": "2026-09-25", "festival": "中秋节",
    }


def test_spring_festival_eve_and_new_year_cross_the_year_boundary():
    assert festivals_on(date(2025, 1, 28)) == ("除夕",)
    assert festivals_on(date(2025, 1, 29)) == ("春节",)
    assert festivals_on(date(2026, 2, 16)) == ("除夕",)
    assert festivals_on(date(2026, 2, 17)) == ("春节",)


def test_leap_lunar_month_does_not_repeat_a_festival():
    assert festivals_on(date(2028, 6, 27)) == ()  # Leap fifth month, fifth day.
    assert calendar_context(date(2028, 6, 27))["lunar_date"] == "农历闰五月初五"


def test_qingming_and_fixed_dates_are_not_treated_as_vacation_schedules():
    assert festivals_on(date(2026, 4, 5)) == ("清明节",)
    assert festivals_on(date(2026, 10, 1)) == ("国庆节",)
    assert "放假安排" in calendar_context(date(2026, 10, 1))["instruction"]


def test_unsupported_year_does_not_make_up_lunar_dates():
    assert calendar_context(date(2051, 9, 25)) is None
    assert festivals_on(date(2051, 9, 25)) == ()
