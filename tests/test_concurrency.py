"""Race conditions. Requests are fired in parallel threads against the real app and database."""
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import make_employee, ms

N = 12


def _parallel(fn):
    with ThreadPoolExecutor(N) as ex:
        return list(ex.map(fn, range(N)))


def test_concurrent_employee_create(api):
    body = dict(emp_code="EMP0001", name="x", email="x@y.com", department="D", joined_on="2026-01-01")
    codes = _parallel(lambda _: api.post("/employees", json=body).status_code)
    assert sorted(codes) == [201] + [409] * (N - 1)


def test_concurrent_punch_in(api, db):
    make_employee(api, "EMP0001")
    codes = _parallel(lambda i: api.post("/attendance/punch-in", json=dict(
        emp_code="EMP0001", punched_at=ms("2026-07-06 09:30:00") + i * 1000)).status_code)
    assert sorted(codes) == [201] + [409] * (N - 1)
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_concurrent_punch_out(api):
    make_employee(api, "EMP0001")
    api.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms("2026-07-06 09:30:00")))
    codes = _parallel(lambda i: api.post("/attendance/punch-out", json=dict(
        emp_code="EMP0001", punched_at=ms("2026-07-06 18:00:00") + i * 60_000)).status_code)
    assert sorted(codes) == [200] + [409] * (N - 1)


def test_concurrent_regularizations_keep_every_history_entry(api):
    make_employee(api, "EMP0001")
    api.post("/attendance/punch-in", json=dict(emp_code="EMP0001", punched_at=ms("2026-07-06 09:30:00")))
    api.post("/attendance/punch-out", json=dict(emp_code="EMP0001", punched_at=ms("2026-07-06 18:00:00")))

    def fix(i):
        return api.patch("/attendance/EMP0001/2026-07-06", json=dict(
            punch_out=ms("2026-07-06 18:01:00") + i * 60_000, reason=f"correction {i}", regularized_by=f"hr{i}")).status_code

    codes = _parallel(fix)
    assert set(codes) <= {200, 409}
    record = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    history = record["history"]
    # Every 200 left exactly one entry; nothing was overwritten.
    assert len(history) == codes.count(200) >= 1
    assert len({h["reason"] for h in history}) == len(history)
    # The entries chain: each one's "from" is the previous one's "to".
    for prev, cur in zip(history, history[1:]):
        assert cur["changes"]["punch_out"]["from"] == prev["changes"]["punch_out"]["to"]
    assert record["punch_out"] == history[-1]["changes"]["punch_out"]["to"]
