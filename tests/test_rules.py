"""Unit tests of the R1-R5 helpers. No database needed. Expected values are worked out by hand from the rules."""
from datetime import date

import pytest

from app import main
from tests.conftest import utc

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


@pytest.mark.parametrize("punch, expected", [
    ("2026-07-06 09:30:00", 0),   # exactly at shift start
    ("2026-07-06 09:40:00", 0),   # exactly 10:00 late -> still within grace
    ("2026-07-06 09:40:01", 10),  # strictly more than 10:00 -> minutes since shift start
    ("2026-07-06 10:15:59", 45),
    ("2026-07-06 09:00:00", 0),   # early
])
def test_late_minutes_r2(punch, expected):
    assert main.compute_late_minutes(utc(punch), DAY, date(2026, 7, 6)) == expected


def test_truncation_then_late():
    # 09:40:00.900 counts as 09:40:00 (R1), so it is not late.
    ts = main.ms_to_dt(int(utc("2026-07-06 09:40:00").timestamp()) * 1000 + 900)
    assert ts == utc("2026-07-06 09:40:00")
    assert main.compute_late_minutes(ts, DAY, date(2026, 7, 6)) == 0


@pytest.mark.parametrize("punch, expected_day", [
    ("2026-07-06 21:55:00", date(2026, 7, 6)),  # before midnight: same day
    ("2026-07-07 00:30:00", date(2026, 7, 6)),  # after midnight, before shift_end: previous day's shift
    ("2026-07-07 05:59:59", date(2026, 7, 6)),
    ("2026-07-07 06:00:00", date(2026, 7, 7)),  # not earlier than shift_end
])
def test_attendance_date_overnight_r1(punch, expected_day):
    assert main.attendance_date(utc(punch), NIGHT) == expected_day


def test_attendance_date_uses_ist_not_utc():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th; the attendance date is the IST date.
    assert main.attendance_date(utc("2026-07-07 00:30:00"), DAY) == date(2026, 7, 7)


def test_overnight_late_after_midnight():
    assert main.compute_late_minutes(utc("2026-07-07 00:30:00"), NIGHT, date(2026, 7, 6)) == 150


@pytest.mark.parametrize("out, expected", [
    ("2026-07-06 18:59:59", 0),   # 29 min 59 s -> 29 -> below minimum
    ("2026-07-06 19:00:00", 30),  # exactly 30
    ("2026-07-06 19:45:30", 75),  # floor
    ("2026-07-06 17:00:00", 0),   # left early
])
def test_overtime_r3(out, expected):
    assert main.compute_overtime(utc(out), DAY, date(2026, 7, 6)) == expected


def test_overtime_overnight_ends_next_day():
    assert main.compute_overtime(utc("2026-07-07 06:40:00"), NIGHT, date(2026, 7, 6)) == 40


@pytest.mark.parametrize("seconds, expected", [
    (14418, 4.01),   # 4.005 h -> half-up 4.01 (float round() gives 4.0)
    (3618, 1.01),    # 1.005 h
    (32760, 9.1),
    (16182, 4.5),    # 4.495 -> 4.50
])
def test_work_hours_half_up_r4(seconds, expected):
    pin = utc("2026-07-06 09:00:00")
    from datetime import timedelta
    assert main.compute_work_hours(pin, pin + timedelta(seconds=seconds)) == expected


@pytest.mark.parametrize("seconds, half_day", [
    (16182, False),  # 4.495 h rounds to 4.50 -> not a half day
    (16164, True),   # 4.49 h
    (16200, False),  # 4.50 h
])
def test_half_day_uses_rounded_hours_r5(seconds, half_day):
    from datetime import timedelta
    pin = utc("2026-07-06 09:30:00")
    d = main.derived_fields("PRESENT", pin, pin + timedelta(seconds=seconds), DAY, date(2026, 7, 6))
    assert d["half_day"] is half_day


def test_derived_for_absence():
    assert main.derived_fields("LEAVE", None, None, DAY, date(2026, 7, 6)) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_working_days():
    assert main.count_working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert main.count_working_days(date(2026, 7, 15), date(2026, 7, 31)) == 13
    assert main.count_working_days(date(2026, 8, 1), date(2026, 7, 31)) == 0
