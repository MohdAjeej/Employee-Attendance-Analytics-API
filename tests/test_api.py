"""Integration tests: employees, punch-in/out, list, regularization, health. Needs MongoDB."""
from pymongo.errors import ServerSelectionTimeoutError

from app import main
from tests.conftest import make_employee, ms, seed_log


def punch_in(c, code, at, **kw):
    return c.post("/attendance/punch-in", json=dict(emp_code=code, punched_at=at, **kw))


def punch_out(c, code, at):
    return c.post("/attendance/punch-out", json=dict(emp_code=code, punched_at=at))


def regularize(c, code, day, **body):
    return c.patch(f"/attendance/{code}/{day}", json=dict(reason="biometric glitch", regularized_by="hr.admin", **body))


# --------------------------------------------------------------------------- health
def test_health_ok(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_health_503_when_db_down(api, monkeypatch):
    def down(*a, **k):
        raise ServerSelectionTimeoutError("down")
    monkeypatch.setattr(main.db, "command", down)
    r = api.get("/health")
    assert r.status_code == 503 and "mongodb://" not in r.text


# --------------------------------------------------------------------------- employees
def test_create_employee(api):
    e = make_employee(api, "EMP0001")
    assert set(e) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(e["created_at"], int) and e["created_at"] > 100_000_000_000


def test_duplicate_employee_409(api):
    make_employee(api, "EMP0001")
    r = api.post("/employees", json=dict(emp_code="EMP0001", name="x", email="x@y.com", department="D", joined_on="2026-01-01"))
    assert r.status_code == 409


def test_employee_validation(api):
    base = dict(emp_code="EMP0002", name="x", email="x@y.com", department="D", joined_on="2026-01-01")
    for bad in [dict(emp_code="E1"), dict(email="nope"), dict(joined_on="2026-02-30"), dict(shift_start="24:00"),
                dict(shift_start="09:00", shift_end="09:00"), dict(name=""), dict(name=5)]:
        assert api.post("/employees", json={**base, **bad}).status_code == 422, bad


def test_list_employees_filter_sort_paginate(api):
    for code, dept in [("EMP0003", "Eng"), ("EMP0001", "Eng"), ("EMP0002", "Ops"), ("EMP0004", "Eng")]:
        make_employee(api, code, dept)
    r = api.get("/employees", params={"department": "Eng", "page": 2, "page_size": 2}).json()
    assert r["total"] == 3 and r["page"] == 2 and r["page_size"] == 2
    assert [i["emp_code"] for i in r["items"]] == ["EMP0004"]
    assert "_id" not in r["items"][0]
    first = api.get("/employees").json()
    assert [i["emp_code"] for i in first["items"]] == ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]
    assert api.get("/employees", params={"department": "eng"}).json()["total"] == 0  # case-sensitive
    for bad in [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}]:
        assert api.get("/employees", params=bad).status_code == 422


# --------------------------------------------------------------------------- punch-in
def test_punch_in_record_shape(api):
    make_employee(api, "EMP0001")
    r = punch_in(api, "EMP0001", ms("2026-07-06 10:15:59") + 999)
    assert r.status_code == 201
    assert r.json() == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                        "punch_in": ms("2026-07-06 10:15:59"), "punch_out": None, "work_hours": None,
                        "late_minutes": 45, "overtime_minutes": 0, "half_day": False, "history": []}


def test_punch_in_errors(api):
    make_employee(api, "EMP0001")
    assert punch_in(api, "EMP9999", ms("2026-07-06 09:00:00")).status_code == 404
    for bad in [1783312500, 1783312500000.0, "1783312500000", True, None, 99_999_999_999_999]:
        assert punch_in(api, "EMP0001", bad).status_code == 422, bad
    assert punch_in(api, "EMP0001", ms("2026-07-06 09:00:00"), status="ABSENT").status_code == 422
    assert api.post("/attendance/punch-in", json={}).status_code == 422
    assert punch_in(api, "EMP0001", ms("2026-07-06 09:00:00"), status="WFH").status_code == 201
    assert punch_in(api, "EMP0001", ms("2026-07-06 12:00:00")).status_code == 409


def test_punch_in_default_now(api):
    make_employee(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert r.status_code == 201 and r.json()["punch_in"] % 1000 == 0


def test_overnight_punch_in_and_out(api):
    make_employee(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = punch_in(api, "EMP0005", ms("2026-07-06 21:55:00"))
    assert (r.json()["date"], r.json()["late_minutes"]) == ("2026-07-06", 0)
    r = punch_out(api, "EMP0005", ms("2026-07-07 06:40:00"))
    assert r.status_code == 200
    j = r.json()
    assert (j["date"], j["work_hours"], j["overtime_minutes"], j["half_day"]) == ("2026-07-06", 8.75, 40, False)
    # 00:30 on the 8th is earlier than shift_end 06:00, so it belongs to the shift that started on the 7th (R1).
    r = punch_in(api, "EMP0005", ms("2026-07-08 00:30:00"))
    assert (r.json()["date"], r.json()["late_minutes"]) == ("2026-07-07", 150)


# --------------------------------------------------------------------------- punch-out
def test_punch_out_rules(api):
    make_employee(api, "EMP0001")
    punch_in(api, "EMP0001", ms("2026-07-06 09:40:00"))
    r = punch_out(api, "EMP0001", ms("2026-07-06 18:59:59") + 500)
    j = r.json()
    assert r.status_code == 200
    assert j["punch_out"] == ms("2026-07-06 18:59:59")
    assert (j["work_hours"], j["overtime_minutes"], j["half_day"], j["late_minutes"]) == (9.33, 0, False, 0)
    assert j["history"] == []
    assert punch_out(api, "EMP0001", ms("2026-07-06 20:00:00")).status_code == 409


def test_punch_out_overtime_and_half_day(api):
    make_employee(api, "EMP0001")
    make_employee(api, "EMP0002")
    punch_in(api, "EMP0001", ms("2026-07-06 09:30:00"))
    assert punch_out(api, "EMP0001", ms("2026-07-06 19:00:00")).json()["overtime_minutes"] == 30
    punch_in(api, "EMP0002", ms("2026-07-06 09:30:00"))
    j = punch_out(api, "EMP0002", ms("2026-07-06 13:59:24")).json()  # 4.49 h
    assert (j["work_hours"], j["half_day"]) == (4.49, True)


def test_punch_out_errors(api):
    make_employee(api, "EMP0001")
    assert punch_out(api, "EMP9999", ms("2026-07-06 18:00:00")).status_code == 404
    assert punch_out(api, "EMP0001", ms("2026-07-06 18:00:00")).status_code == 404  # no punch-in
    punch_in(api, "EMP0001", ms("2026-07-06 09:00:00"))
    assert punch_out(api, "EMP0001", ms("2026-07-06 08:00:00")).status_code == 404  # nothing at or before
    assert punch_out(api, "EMP0001", ms("2026-07-06 09:00:00")).status_code == 422  # == punch_in
    assert punch_out(api, "EMP0001", ms("2026-07-07 09:00:01")).status_code == 422  # > 24 h
    assert punch_out(api, "EMP0001", 1783312500).status_code == 422


# --------------------------------------------------------------------------- list
def test_list_attendance(api, db):
    for code in ["EMP0001", "EMP0002"]:
        make_employee(api, code)
    seed_log(db, "EMP0002", "2026-07-06")
    seed_log(db, "EMP0001", "2026-07-06", status="LEAVE")
    seed_log(db, "EMP0001", "2026-07-07", legacy=True)
    r = api.get("/attendance").json()
    assert r["total"] == 3
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    legacy = r["items"][0]
    assert legacy["history"] == [] and legacy["half_day"] is False and "_id" not in legacy and "id" not in legacy
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    assert api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07"}).json()["total"] == 1
    assert api.get("/attendance", params={"date_to": "2026-07-06", "page_size": 1, "page": 2}).json()["items"][0]["emp_code"] == "EMP0002"
    for bad in [{"date_from": "2026-07-09", "date_to": "2026-07-01"}, {"date_from": "07/06/2026"},
                {"status": "LATE"}, {"page_size": 101}, {"date_to": "2026-02-30"}]:
        assert api.get("/attendance", params=bad).status_code == 422, bad


# --------------------------------------------------------------------------- regularize
def test_regularize_punch_in(api):
    make_employee(api, "EMP0001")
    punch_in(api, "EMP0001", ms("2026-07-07 10:05:00"))
    punch_out(api, "EMP0001", ms("2026-07-07 18:30:00"))
    r = regularize(api, "EMP0001", "2026-07-07", punch_in=ms("2026-07-07 09:28:00"), work_hours=1.0)  # derived ignored
    assert r.status_code == 200
    j = r.json()
    assert (j["late_minutes"], j["work_hours"]) == (0, 9.03)
    [entry] = j["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ms("2026-07-07 10:05:00"), "to": ms("2026-07-07 09:28:00")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }


def test_regularize_to_leave_and_back(api):
    make_employee(api, "EMP0001")
    punch_in(api, "EMP0001", ms("2026-07-07 09:50:00"))
    j = regularize(api, "EMP0001", "2026-07-07", status="LEAVE").json()
    assert (j["status"], j["punch_in"], j["late_minutes"]) == ("LEAVE", None, 0)
    assert set(j["history"][0]["changes"]) == {"status", "punch_in", "late_minutes"}
    assert regularize(api, "EMP0001", "2026-07-07", status="PRESENT").status_code == 422  # needs a punch_in
    j = regularize(api, "EMP0001", "2026-07-07", status="WFH", punch_in=ms("2026-07-07 09:30:00"),
                   punch_out=ms("2026-07-07 13:00:00")).json()
    assert (j["work_hours"], j["half_day"], len(j["history"])) == (3.5, True, 2)


def test_regularize_errors(api, db):
    make_employee(api, "EMP0001")
    seed_log(db, "EMP0001", "2026-07-07", status="ABSENT")
    assert regularize(api, "EMP9999", "2026-07-07", status="LEAVE").status_code == 404
    assert regularize(api, "EMP0001", "2026-07-08", status="LEAVE").status_code == 404
    assert regularize(api, "EMP0001", "2026-7-7", status="LEAVE").status_code == 422
    assert regularize(api, "EMP0001", "2026-07-07", status="ABSENT").status_code == 422  # changes nothing
    assert regularize(api, "EMP0001", "2026-07-07", status="LEAVE", punch_in=ms("2026-07-07 09:00:00")).status_code == 422
    assert regularize(api, "EMP0001", "2026-07-07", status="PRESENT", punch_in=ms("2026-07-08 09:00:00")).status_code == 422
    assert regularize(api, "EMP0001", "2026-07-07", status="PRESENT", punch_in=ms("2026-07-07 09:00:00"),
                      punch_out=ms("2026-07-07 08:00:00")).status_code == 422
    assert regularize(api, "EMP0001", "2026-07-07", status="PRESENT", punch_in=1783312500).status_code == 422
    r = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "reason": "abc", "regularized_by": "x"})
    assert r.status_code == 422  # reason too short
    r = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "reason": "valid reason"})
    assert r.status_code == 422  # regularized_by missing
