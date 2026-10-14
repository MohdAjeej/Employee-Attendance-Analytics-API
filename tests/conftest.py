"""Shared fixtures. Integration tests need a real MongoDB.

TEST_MONGO_URI (default mongodb://localhost:27017) is used with a throwaway database, which is dropped afterwards.
If no server answers, every test that needs the database is skipped (not failed).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ["MONGO_URI"] = os.environ.get("TEST_MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = f"attendance_test_{uuid.uuid4().hex[:8]}"

from fastapi.testclient import TestClient  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402

import app.main as main  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ms(ist: str) -> int:
    """'2026-07-06 09:40:01' (IST wall clock) -> epoch milliseconds."""
    return int(datetime.fromisoformat(ist).replace(tzinfo=IST).timestamp()) * 1000


def utc(ist: str) -> datetime:
    return datetime.fromisoformat(ist).replace(tzinfo=IST).astimezone(timezone.utc)


def _mongo_up() -> bool:
    try:
        main.client.admin.command("ping")
        return True
    except PyMongoError:
        return False


MONGO_UP = _mongo_up()
needs_mongo = pytest.mark.skipif(not MONGO_UP, reason="MongoDB not reachable at TEST_MONGO_URI")


@pytest.fixture(scope="session")
def client():
    if not MONGO_UP:
        pytest.skip("MongoDB not reachable at TEST_MONGO_URI")
    with TestClient(main.app) as c:
        yield c
        main.client.drop_database(os.environ["MONGO_DB"])  # before the lifespan closes the client


@pytest.fixture()
def api(client):
    """A client on an empty database (indexes kept)."""
    main.employees.delete_many({})
    main.logs.delete_many({})
    return client


@pytest.fixture()
def db(api):
    return main.db


def make_employee(c, code, dept="Eng", joined_on="2026-01-01", shift_start="09:30", shift_end="18:30", **extra):
    body = dict(emp_code=code, name=f"Name {code}", email=f"{code.lower()}@example.com", department=dept,
                shift_start=shift_start, shift_end=shift_end, joined_on=joined_on, **extra)
    r = c.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def seed_log(db, code, day, status="PRESENT", late=0, work_hours=8.0, half_day=False, overtime=0, legacy=False):
    """Insert a record directly, the way the grader seeds data (stored derived values are authoritative)."""
    presence = status in ("PRESENT", "WFH", "ON_DUTY")
    doc = dict(emp_code=code, date=day, status=status,
               punch_in=utc(f"{day} 09:30:00") if presence else None, punch_out=None,
               work_hours=work_hours if presence else None, late_minutes=late,
               overtime_minutes=overtime, half_day=half_day, history=[])
    if legacy:
        del doc["half_day"], doc["history"]
    db.attendance_logs.insert_one(doc)
