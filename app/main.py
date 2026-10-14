"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml. Stored data: DATA_MODEL.md. Review of the original starter: REVIEW.md.
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Literal, Optional

from bson import Decimal128, json_util
from bson.json_util import RELAXED_JSON_OPTIONS
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)  # .env only fills gaps; real environment variables take priority

log = logging.getLogger("attendance")
if not logging.getLogger().handlers:  # do not override a logging config set by uvicorn or the host
    logging.basicConfig(level=logging.INFO, format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s")

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")

# tz_aware=True: datetimes come back as aware UTC instead of naive values that are easy to misread as local time.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata: fixed +05:30, no DST, so no tzdata dependency
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000
GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE = ["ABSENT", "LEAVE"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = StrictInt  # bounds are applied per field with Field(ge=..., le=...)


# --------------------------------------------------------------------------- #
# Startup and shutdown lifecycle; index creation
# --------------------------------------------------------------------------- #
_indexes_ready = False
_indexes_lock = threading.Lock()


def ensure_indexes() -> None:
    """Create every index the app relies on. create_index is idempotent, so this is safe on every start."""
    global _indexes_ready
    with _indexes_lock:
        if _indexes_ready:
            return
        # Uniqueness of the business key; also serves lookups by emp_code and the unfiltered employee list.
        employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
        # Department filter + emp_code sort (GET /employees), department-scoped analytics.
        employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
        employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
        # Headcount for all departments (summary without a department filter).
        employees.create_index([("joined_on", ASCENDING)], name="joined_on")
        # One record per employee per day: makes punch-in race-safe, serves per-employee queries and $lookups.
        logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
        # Attendance list default sort (date desc, emp_code asc) and month/date-range analytics.
        logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
        # Attendance list filtered by status, same sort.
        logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code"
        )
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # do not crash the process; /health reports 503 and retries
        log.warning("index creation deferred: %s", exc)
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: StrictStr = Field(pattern=r"^EMP\d{4,6}$")
    name: StrictStr = Field(min_length=1, max_length=100)
    email: StrictStr = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: StrictStr = Field(min_length=1, max_length=50)
    shift_start: StrictStr = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: StrictStr = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: StrictStr

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("expected a date YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def _reject_explicit_null(v: Any) -> Any:
    if v is None:
        raise ValueError("must be an integer (epoch milliseconds), not null")
    return v


class PunchInIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)
    status: PresenceT = "PRESENT"

    _no_null = field_validator("punched_at", mode="before")(_reject_explicit_null)


class PunchOutIn(BaseModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)

    _no_null = field_validator("punched_at", mode="before")(_reject_explicit_null)


class RegularizeIn(BaseModel):
    # openapi.yaml: derived fields "never appear in a request body. If a client sends one, ignore it."
    model_config = ConfigDict(extra="ignore")

    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)
    punch_out: Optional[EpochMs] = Field(None, ge=MIN_MS, le=MAX_MS)
    reason: StrictStr = Field(min_length=5, max_length=200)
    regularized_by: StrictStr = Field(min_length=1, max_length=50)

    _no_null = field_validator("status", "punch_in", "punch_out", mode="before")(_reject_explicit_null)


# --------------------------------------------------------------------------- #
# Helpers: errors, timestamps, calendar values, decimal rounding
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str) -> RequestValidationError:
    """A 422 in FastAPI's default shape ({"detail": [ ... ]})."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1). Integer maths, no float error."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:  # defensive: BSON datetimes are always UTC
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """Current instant at millisecond precision (what BSON can store)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def parse_date(value: str, loc: list) -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise unprocessable(loc, "expected a date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable(loc, "not a valid calendar date")


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def at_ist(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=IST)


def round_half_up(value: Any, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def count_working_days(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] inclusive."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Helpers: business rules R1 - R5 (instants are aware UTC datetimes truncated to seconds)
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of punch_in; an overnight-shift punch before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day


def shift_end_instant(day: date, emp: dict) -> datetime:
    end = at_ist(day, emp["shift_end"])
    return end + timedelta(days=1) if is_overnight(emp) else end


def compute_late_minutes(punch_in: datetime, emp: dict, day: date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    seconds = int((punch_in - at_ist(day, emp["shift_start"])).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (Decimal, so no banker's or binary-float rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, emp: dict, day: date) -> int:
    """R3: floor minutes after shift end (next day for overnight shifts), only if >= 30."""
    minutes = int((punch_out - shift_end_instant(day, emp)).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, day: date) -> dict:
    """The stored derived values for a record (table in DATA_MODEL.md section 3)."""
    if status in ABSENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp, day)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, emp, day),
        "half_day": Decimal(str(hours)) < HALF_DAY_HOURS,  # R5 on the rounded value
    }


# --------------------------------------------------------------------------- #
# Serialization (Mongo -> API)
# --------------------------------------------------------------------------- #
def _ms_if_dt(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_record(doc: dict) -> dict:
    history = []
    for entry in doc.get("history") or []:
        history.append({
            "at": dt_to_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": {
                field: {"from": _ms_if_dt(c.get("from")), "to": _ms_if_dt(c.get("to"))}
                for field, c in (entry.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Shared query builders (used by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
def attendance_list_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
    status: Optional[str], page: int, page_size: int,
) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    d_from = parse_date(date_from, ["query", "date_from"]) if date_from is not None else None
    d_to = parse_date(date_to, ["query", "date_to"]) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise unprocessable(["query", "date_from"], "date_from must be on or before date_to")
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def agg_round_half_up(expr: Any, places: int) -> dict:
    """Half-up rounding in the database. $round is half-to-even, so use Decimal128 and floor(x * 10^n + 0.5)."""
    scale = 10 ** places
    return {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}},
        scale,
    ]}}


def agg_is_weekday(date_field: str) -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}, 5]}


def agg_is_presence(status_field: str) -> dict:
    return {"$in": [{"$ifNull": [status_field, None]}, PRESENCE]}


def agg_present_weight(prefix: str = "$") -> dict:
    """R5/R6: 1 for a presence record, 0.5 for a half day, 0 otherwise."""
    return {"$cond": [agg_is_presence(f"{prefix}status"), {"$cond": [{"$ifNull": [f"{prefix}half_day", False]}, 0.5, 1]}, 0]}


def agg_late(prefix: str = "$") -> dict:
    return {"$ifNull": [f"{prefix}late_minutes", 0]}


def monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [agg_is_weekday("$date"), agg_present_weight(), 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [agg_late(), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": agg_late()},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
        {"$project": {"_id": 0}},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9: joined by the end of the month
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [agg_is_presence("$l.status"), {"$isNumber": "$l.work_hours"}]}
    return [
        # Start from employees so that people with zero logs still produce a row (headcount).
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "l",
        }},
        {"$unwind": {"path": "$l", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [agg_is_presence("$l.status"), agg_is_weekday("$l.date")]}, agg_present_weight("$l."), 0,
            ]}},
            "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$l.work_hours"}, 0]}},
            "wh_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [agg_late("$l."), 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [agg_late("$l."), 0]}, agg_late("$l."), 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$l.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$l.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": agg_round_half_up("$present_days", 2),
            # Mean over records (sum / count), not a mean of per-employee means.
            "avg_work_hours": {"$cond": [
                {"$gt": ["$wh_count", 0]}, agg_round_half_up({"$divide": ["$wh_sum", "$wh_count"]}, 2), None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    emp_stages: list = [{"$project": {"_id": 0, "name": 1, "department": 1}}]
    if department is not None:
        emp_stages.insert(0, {"$match": {"department": department}})
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "count": {"$sum": 1}}},
        # Inner-join to employees: drops logs of unknown employees and (if given) other departments before ranking.
        {"$lookup": {
            "from": "employees", "localField": "_id", "foreignField": "emp_code",
            "pipeline": emp_stages, "as": "emp",
        }},
        {"$unwind": "$emp"},
        # Standard competition ranking (1, 2, 2, 4); limit applies to the rank, not to the row count.
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": "$total",
            "late_count": "$count",
        }},
    ]


def department_trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    upper = (date.fromisoformat(d_to) + timedelta(days=1)).isoformat()
    zero_event = {"date": d_from, "hc": 0, "present": 0, "late": 0}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": d_to}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "l",
        }},
        # Each employee emits one headcount event (+1 on the day they count from) plus one event per record.
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"date": {"$max": ["$joined_on", d_from]}, "hc": 1, "present": 0, "late": 0}],
            {"$map": {"input": "$l", "as": "r", "in": {
                "date": "$$r.date",
                "hc": 0,
                "present": agg_present_weight("$$r."),
                "late": {"$cond": [{"$gt": [agg_late("$$r."), 0]}, 1, 0]},
            }}},
        ]}}},
        {"$unwind": "$events"},
        {"$replaceRoot": {"newRoot": "$events"}},
        # Guarantees at least one document so $densify has something to fill from even with no data.
        {"$unionWith": {"pipeline": [{"$documents": [zero_event]}]}},
        {"$group": {
            "_id": "$date",
            "hc": {"$sum": "$hc"},
            "present": {"$sum": "$present"},
            "late": {"$sum": "$late"},
        }},
        {"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                      "hc": 1, "present": 1, "late": 1}},
        # Gap filling: one row per calendar day in [from, to].
        {"$densify": {"field": "d", "range": {
            "step": 1, "unit": "day",
            "bounds": [datetime.fromisoformat(d_from).replace(tzinfo=UTC),
                       datetime.fromisoformat(upper).replace(tzinfo=UTC)],
        }}},
        {"$addFields": {
            "hc": {"$ifNull": ["$hc", 0]},
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
        }},
        # Running headcount: employees whose joined_on <= this day (R9).
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$addFields": {"is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]}}},
        {"$addFields": {"rate_dec": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [
                {"$floor": {"$add": [
                    {"$multiply": [{"$divide": [{"$toDecimal": "$present"}, "$headcount"]}, 10000]},
                    Decimal128("0.5"),
                ]}},
                10000,
            ]},
            None,
        ]}}},
        # Window is bounded by the requested range because earlier days were never generated.
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "ma": {"$avg": "$rate_dec", "window": {"documents": [-6, "current"]}},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": "$present",
            "late_count": "$late",
            "attendance_rate": {"$toDouble": "$rate_dec"},
            "moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$ma", None]}, None]}, None, agg_round_half_up("$ma", 4)]},
        }},
    ]


def trend_range(d_from: str, d_to: str) -> None:
    f = parse_date(d_from, ["query", "from"])
    t = parse_date(d_to, ["query", "to"])
    if t < f:
        raise unprocessable(["query", "to"], "to must be on or after from")
    if (t - f).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], f"range exceeds {MAX_TREND_DAYS} days")


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else ms_to_dt(dt_to_ms(now_utc()))
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, emp, day),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date) index: exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else ms_to_dt(dt_to_ms(now_utc()))
    # `date` is monotonic in punch_in (R1), so the latest date with punch_in <= ts is the most recent punch-in.
    record = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = record["punch_in"]
    if ts <= pin or ts - pin > MAX_SHIFT:
        raise unprocessable(["body", "punched_at"], "punch-out must be after punch-in and within 24 hours")
    day = date.fromisoformat(record["date"])
    derived = derived_fields(record["status"], pin, ts, emp, day)
    derived.pop("late_minutes")  # fixed at punch-in
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # atomic: only one concurrent punch-out matches
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(spec["filter"])
    cursor = (
        logs.find(spec["filter"], {"_id": 0})
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


FIELDS_TRACKED = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


REGULARIZE_ATTEMPTS = 5


def plan_regularization(body: "RegularizeIn", record: dict, emp: dict, day: date) -> tuple[dict, dict]:
    """Validate the correction against the stored record; return (new field values, changes for history)."""
    old = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    sent = body.model_fields_set
    status = body.status if "status" in sent else old["status"]

    if status in ABSENCE:
        if "punch_in" in sent or "punch_out" in sent:
            raise unprocessable(["body"], f"status {status} clears punch times; do not send punch_in/punch_out")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if "punch_in" in sent else old["punch_in"]
        pout = ms_to_dt(body.punch_out) if "punch_out" in sent else old["punch_out"]
        if pin is None:
            raise unprocessable(["body", "punch_in"], f"status {status} requires a punch_in")
        if attendance_date(pin, emp) != day:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if pout is not None and (pout <= pin or pout - pin > MAX_SHIFT):
            raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": pin, "punch_out": pout, **derived_fields(status, pin, pout, emp, day)}
    changes = {}
    for f in FIELDS_TRACKED:
        before, after = old[f], new[f]
        if isinstance(before, datetime) or isinstance(after, datetime):
            if dt_to_ms(before) != dt_to_ms(after):
                changes[f] = {"from": before, "to": after}
        elif before != after:
            changes[f] = {"from": before, "to": after}
    if not changes:
        raise unprocessable(["body"], "the request changes nothing")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, date_str: str = Path(alias="date")):
    day = parse_date(date_str, ["path", "date"])
    emp = get_employee_or_404(emp_code)
    for _ in range(REGULARIZE_ATTEMPTS):
        record = logs.find_one({"emp_code": emp_code, "date": date_str})
        if record is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(body, record, emp, day)
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        # Optimistic concurrency: the write only applies if the record is still the version we planned from
        # (same editable fields and same history length). $set and $push happen in one atomic update.
        guard = {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
        }
        updated = logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return serialize_record(updated)
        # Someone else won the race: re-read and re-plan against their result (changes are relative to it).
    raise HTTPException(409, "record is being modified concurrently; retry")


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(date.fromisoformat(start), joined), date.fromisoformat(end))

    rows = list(logs.aggregate(monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": (
            round_half_up(Decimal(str(present)) * 100 / working_days, 2) if working_days else None
        ),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = list(employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    d_from: str = Query(..., alias="from"),
    d_to: str = Query(..., alias="to"),
):
    trend_range(d_from, d_to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(employees.aggregate(department_trend_pipeline(department, d_from, d_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


def _require(value: Optional[str], name: str) -> str:
    if value is None:
        raise unprocessable(["query", name], "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    d_from: Optional[str] = Query(None, alias="from"),
    d_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint is ExplainTarget.attendance_list:
        spec = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": spec["filter"], "sort": spec["sort"], "skip": spec["skip"],
               "limit": spec["limit"], "projection": {"_id": 0}}
    else:
        if endpoint is ExplainTarget.employee_monthly:
            coll, pipeline = "attendance_logs", monthly_pipeline(_require(emp_code, "emp_code"), _require(month, "month"))
        elif endpoint is ExplainTarget.department_summary:
            coll, pipeline = "employees", department_summary_pipeline(_require(month, "month"), department)
        elif endpoint is ExplainTarget.late_leaderboard:
            coll, pipeline = "attendance_logs", late_leaderboard_pipeline(_require(month, "month"), limit, department)
        else:
            _require(department, "department")
            trend_range(_require(d_from, "from"), _require(d_to, "to"))
            coll, pipeline = "employees", department_trend_pipeline(department, d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    as_json = json.loads(json_util.dumps(raw, json_options=RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint.value, "collection": coll, "explain": as_json}


# --------------------------------------------------------------------------- #
# Error handling and health check
# --------------------------------------------------------------------------- #
@app.exception_handler(PyMongoError)
async def mongo_error_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    """Database outages become 503 with a generic message; details go to the log, never to the client."""
    log.error("event=db_error method=%s path=%s error_type=%s error=%r",
              request.method, request.url.path, type(exc).__name__, str(exc)[:200])
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}
