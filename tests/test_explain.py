"""Explain plans on a ~100k-record synthetic dataset. The plan text comes from MongoDB itself."""
import json
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app import main

DEPTS = ["Eng", "Ops", "Sales", "HR", "Finance"]
N_EMP, N_DAYS = 500, 200


@pytest.fixture(scope="module")
def big(client):
    main.employees.delete_many({})
    main.logs.delete_many({})
    rnd = random.Random(7)
    main.employees.insert_many([
        dict(emp_code=f"EMP{i:04d}", name=f"n{i}", email=f"e{i}@x.com", department=DEPTS[i % 5], shift_start="09:30",
             shift_end="18:30", joined_on="2025-06-01", created_at=datetime.now(timezone.utc)) for i in range(N_EMP)])
    docs, start = [], date(2026, 1, 1)
    for i in range(N_EMP):
        for k in range(N_DAYS):
            st = rnd.choice(["PRESENT"] * 7 + ["WFH", "ON_DUTY", "LEAVE", "ABSENT"])
            docs.append(dict(emp_code=f"EMP{i:04d}", date=(start + timedelta(days=k)).isoformat(), status=st,
                             punch_in=None, punch_out=None, work_hours=8.0 if st in main.PRESENCE else None,
                             late_minutes=rnd.choice([0, 0, 0, 12, 30]), overtime_minutes=0, half_day=False, history=[]))
    main.logs.insert_many(docs)
    assert main.logs.count_documents({}) == N_EMP * N_DAYS
    yield client
    main.employees.delete_many({})
    main.logs.delete_many({})


CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"status": "LEAVE", "page": 3}),
    ("attendance_list", {"emp_code": "EMP0042"}),
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("employee_monthly", {"emp_code": "EMP0042", "month": "2026-03"}),
    ("department_summary", {"month": "2026-03"}),
    ("department_summary", {"month": "2026-03", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "department": "Ops", "limit": 5}),
    ("department_trend", {"department": "Eng", "from": "2026-03-01", "to": "2026-05-31"}),
]


@pytest.mark.parametrize("endpoint, params", CASES)
def test_explain_uses_index(big, endpoint, params):
    r = big.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    plan = json.dumps(body["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan
    assert "executionStats" in plan or "$cursor" in plan  # real executionStats output


def test_explain_validation(big):
    assert big.get("/admin/explain/unknown").status_code == 422
    assert big.get("/admin/explain/employee_monthly", params={"month": "2026-03"}).status_code == 422
    assert big.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert big.get("/admin/explain/late_leaderboard").status_code == 422


def test_results_match_at_scale(big):
    """The real endpoints answer on the big dataset; the leaderboard respects competition ranking."""
    items = big.get("/analytics/leaderboard/late", params={"month": "2026-03", "limit": 10}).json()["items"]
    totals = [i["total_late_minutes"] for i in items]
    assert totals == sorted(totals, reverse=True)
    for pos, item in enumerate(items):
        assert item["rank"] == 1 + sum(1 for t in totals if t > item["total_late_minutes"])
    assert all(i["rank"] <= 10 for i in items)
    rows = big.get("/analytics/departments/Eng/trend", params={"from": "2026-03-01", "to": "2026-05-31"}).json()["items"]
    assert len(rows) == 92
    summary = big.get("/analytics/departments/summary", params={"month": "2026-03"}).json()["items"]
    assert sum(i["headcount"] for i in summary) == N_EMP
