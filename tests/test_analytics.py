"""Analytics against hand-computed expectations.

Calendar used: July 2026. 1 Jul is a Wednesday, so 4-5 Jul and 11-12 Jul are weekends; July has 23 working days.
"""
import pytest

from tests.conftest import make_employee, seed_log


@pytest.fixture()
def world(api, db):
    make_employee(api, "EMP0001", "Eng")
    make_employee(api, "EMP0002", "Eng")
    make_employee(api, "EMP0003", "Eng")
    make_employee(api, "EMP0004", "Eng", joined_on="2026-07-15")   # mid-month joiner
    make_employee(api, "EMP0005", "Ops", shift_start="22:00", shift_end="06:00")
    make_employee(api, "EMP0006", "Ops")                           # no logs at all
    make_employee(api, "EMP0007", "Ops", joined_on="2026-08-01")   # joins after July
    make_employee(api, "EMP0008", "Empty", joined_on="2026-09-01") # department with headcount 0 in July

    seed_log(db, "EMP0001", "2026-07-01", late=30)
    seed_log(db, "EMP0001", "2026-07-02", work_hours=3.5, half_day=True)
    seed_log(db, "EMP0001", "2026-07-03", status="LEAVE")
    seed_log(db, "EMP0001", "2026-07-04", late=25, overtime=45)        # Saturday
    seed_log(db, "EMP0001", "2026-07-06", status="ON_DUTY", work_hours=7.0, legacy=True)
    seed_log(db, "EMP0002", "2026-07-01", late=50)
    seed_log(db, "EMP0003", "2026-07-01", late=50)
    seed_log(db, "EMP0003", "2026-07-02", status="ABSENT")
    seed_log(db, "EMP0004", "2026-07-15", late=10, work_hours=9.0)
    seed_log(db, "EMP0005", "2026-07-01", late=5, overtime=40)
    seed_log(db, "EMP9999", "2026-07-01", late=500)                     # orphan log: unknown employee
    seed_log(db, "EMP0007", "2026-07-01", late=1)                       # before joining: not in July headcount
    seed_log(db, "EMP0001", "2026-06-30", late=999)                     # other month
    return api


# --------------------------------------------------------------------------- employee monthly
def test_employee_monthly(world):
    r = world.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    # present: 1 Jul (1) + 2 Jul half (0.5) + 6 Jul ON_DUTY (1); Saturday record excluded; LEAVE not present.
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 2.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 55, "total_overtime_minutes": 45,
                 "attendance_pct": 10.87}


def test_employee_monthly_joiner_and_edge_cases(world):
    r = world.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()
    assert (r["working_days"], r["present_days"], r["attendance_pct"]) == (13, 1, 7.69)  # 1/13 = 7.6923
    r = world.get("/analytics/employees/EMP0006/monthly", params={"month": "2026-07"}).json()
    assert (r["working_days"], r["present_days"], r["late_count"], r["attendance_pct"]) == (23, 0, 0, 0.0)
    r = world.get("/analytics/employees/EMP0007/monthly", params={"month": "2026-06"}).json()
    assert (r["working_days"], r["attendance_pct"]) == (0, None)
    assert world.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    for bad in ["2026-13", "2026-7", "July"]:
        assert world.get("/analytics/employees/EMP0001/monthly", params={"month": bad}).status_code == 422
    assert world.get("/analytics/employees/EMP0001/monthly").status_code == 422


# --------------------------------------------------------------------------- department summary
def test_department_summary(world):
    r = world.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert r["month"] == "2026-07"
    assert [i["department"] for i in r["items"]] == ["Eng", "Ops"]  # "Empty" has headcount 0 -> omitted
    eng, ops = r["items"]
    # Eng present records with work_hours: 8, 3.5, 8 (Sat), 7, 8, 8, 9 -> 51.5 / 7 = 7.357 -> 7.36
    assert eng == {"department": "Eng", "headcount": 4, "present_days": 5.5, "avg_work_hours": 7.36,
                   "late_count": 5, "total_late_minutes": 165, "leave_count": 1, "on_duty_count": 1}
    # Ops: EMP0005 + EMP0006 (no logs) count; EMP0007 joins in August, so neither it nor its log is in July.
    assert ops == {"department": "Ops", "headcount": 2, "present_days": 1, "avg_work_hours": 8.0,
                   "late_count": 1, "total_late_minutes": 5, "leave_count": 0, "on_duty_count": 0}


def test_department_summary_filter_and_no_logs(world):
    r = world.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()
    assert [i["department"] for i in r["items"]] == ["Ops"]
    r = world.get("/analytics/departments/summary", params={"month": "2026-03"}).json()
    eng = r["items"][0]  # everyone except mid-month/late joiners, no records at all
    assert (eng["headcount"], eng["present_days"], eng["avg_work_hours"], eng["late_count"]) == (3, 0, None, 0)
    assert world.get("/analytics/departments/summary", params={"month": "2026-00"}).status_code == 422


# --------------------------------------------------------------------------- leaderboard
def _ranks(resp):
    return [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in resp.json()["items"]]


def test_leaderboard_ties_and_cutoff(world):
    # Totals: EMP0001 55, EMP0002 50, EMP0003 50, EMP0004 10, EMP0005 5. Orphan EMP9999 ignored.
    r = world.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2})
    assert _ranks(r) == [(1, "EMP0001", 55), (2, "EMP0002", 50), (2, "EMP0003", 50)]  # tie at cutoff: both kept
    r = world.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3})
    assert _ranks(r) == [(1, "EMP0001", 55), (2, "EMP0002", 50), (2, "EMP0003", 50)]  # next rank is 4, not 3
    r = world.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4})
    assert _ranks(r)[-1] == (4, "EMP0004", 10)
    item = r.json()["items"][0]
    assert item == {"rank": 1, "emp_code": "EMP0001", "name": "Name EMP0001", "department": "Eng",
                    "total_late_minutes": 55, "late_count": 2}


def test_leaderboard_department_and_validation(world):
    r = world.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Ops"})
    # Ranked inside Ops only. EMP0007 exists (only unknown employees are ignored by the contract), so its log counts.
    assert _ranks(r) == [(1, "EMP0005", 5), (2, "EMP0007", 1)]
    assert world.get("/analytics/leaderboard/late", params={"month": "2026-05"}).json()["items"] == []
    for bad in [{"month": "2026-07", "limit": 0}, {"month": "2026-07", "limit": 51}, {}]:
        assert world.get("/analytics/leaderboard/late", params=bad).status_code == 422


def test_leaderboard_all_tied(api, db):
    for code in ["EMP0003", "EMP0001", "EMP0002"]:
        make_employee(api, code)
        seed_log(db, code, "2026-07-01", late=20)
    r = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
    assert _ranks(r) == [(1, "EMP0001", 20), (1, "EMP0002", 20), (1, "EMP0003", 20)]


# --------------------------------------------------------------------------- trend
def test_trend_series(world):
    r = world.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-16"}).json()
    rows = {i["date"]: i for i in r["items"]}
    assert r["department"] == "Eng"
    assert list(rows) == [f"2026-07-{d:02d}" for d in range(1, 17)]  # every day, weekends and empty days included
    # Headcount 3 until EMP0004 joins on the 15th.
    assert [rows[d]["headcount"] for d in ("2026-07-01", "2026-07-14", "2026-07-15")] == [3, 3, 4]
    assert rows["2026-07-01"] | {} == {"date": "2026-07-01", "is_working_day": True, "headcount": 3,
                                       "present_count": 3, "late_count": 3, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert (rows["2026-07-02"]["present_count"], rows["2026-07-02"]["attendance_rate"]) == (0.5, 0.1667)
    assert rows["2026-07-02"]["moving_avg_7d"] == 0.5834  # (1 + 0.1667) / 2 = 0.58335 -> half-up
    # Saturday: present record counted, rate null, average carries the working days of the window.
    sat = rows["2026-07-04"]
    assert (sat["is_working_day"], sat["present_count"], sat["late_count"], sat["attendance_rate"]) == (False, 1, 1, None)
    assert sat["moving_avg_7d"] == 0.3889  # (1 + 0.1667 + 0) / 3
    # 3 Jul: only a LEAVE record -> working day with rate 0.0 (records exist but nobody present).
    assert rows["2026-07-03"]["attendance_rate"] == 0.0
    # 8 Jul: no records at all -> still a row with zeros.
    assert (rows["2026-07-08"]["present_count"], rows["2026-07-08"]["attendance_rate"]) == (0, 0.0)
    # 15 Jul: 1 present out of 4.
    assert rows["2026-07-15"]["attendance_rate"] == 0.25


def test_trend_short_window_does_not_look_before_from(world):
    r = world.get("/analytics/departments/Eng/trend", params={"from": "2026-07-04", "to": "2026-07-06"}).json()
    # 4th and 5th are weekend -> no rate in window -> null; 6th: one ON_DUTY of 3 -> 0.3333.
    assert [(i["attendance_rate"], i["moving_avg_7d"]) for i in r["items"]] == [(None, None), (None, None), (0.3333, 0.3333)]


def test_trend_department_without_records_and_errors(world):
    r = world.get("/analytics/departments/Ops/trend", params={"from": "2026-05-01", "to": "2026-05-03"}).json()
    assert [(i["headcount"], i["present_count"], i["attendance_rate"]) for i in r["items"]] == [(2, 0, 0.0), (2, 0, None), (2, 0, None)]
    r = world.get("/analytics/departments/Empty/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).json()
    assert [(i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in r["items"]] == [(0, None, None)] * 2
    one = world.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-01"}).json()
    assert len(one["items"]) == 1
    assert world.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert world.get("/analytics/departments/eng/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    for bad in [{"from": "2026-07-03", "to": "2026-07-02"}, {"from": "2026-07-01", "to": "2026-10-01"},
                {"from": "2026-07-01"}, {"from": "2026-07-01", "to": "2026-07-32"}]:
        assert world.get("/analytics/departments/Eng/trend", params=bad).status_code == 422, bad
    assert world.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200
