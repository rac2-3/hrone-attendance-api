"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Layout of this file (everything lives here on purpose, see README):
  1. configuration and database handles
  2. small utilities: errors, time conversion, rounding
  3. business rules R1-R5 (attendance date, late, work hours, overtime, half day)
  4. request models and response serialisers
  5. indexes
  6. employees and attendance endpoints
  7. aggregation pipelines + analytics endpoints
  8. explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Day
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones,
# so we never have to guess which zone a stored instant was in.
client = MongoClient(
    os.getenv("MONGO_URI") or "mongodb://localhost:27017",
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=8000,
)
db = client[os.getenv("MONGO_DB") or "attendance_db"]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60       # R2
MIN_OVERTIME_MINUTES = 30     # R3
HALF_DAY_HUNDREDTHS = 450     # R5: work_hours < 4.50
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out / regularize window
MAX_TREND_DAYS = 92

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
StatusT = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatusT = Literal["PRESENT", "WFH", "ON_DUTY"]

_indexes_ready = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    # A database outage is a 503, never a 500 with a stack trace.
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Utilities
# --------------------------------------------------------------------------- #
def fail(status: int, message: str, loc: tuple = ("body",)):
    """Raise an error. 422s mimic FastAPI's list-shaped `detail`; everything else is a string."""
    if status == 422:
        raise HTTPException(422, detail=[{"loc": list(loc), "msg": message, "type": "value_error"}])
    raise HTTPException(status, detail=message)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:  # defensive: treat naive values as UTC
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def utc_now() -> datetime:
    """Current instant, truncated to whole seconds (used for punches)."""
    return datetime.now(UTC).replace(microsecond=0)


def utc_now_ms() -> datetime:
    """Current instant at millisecond precision (BSON's precision), for created_at / history.at."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond - now.microsecond % 1000)


def seconds_between(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86400 + delta.seconds


def half_up_div(numerator: int, denominator: int) -> int:
    """round-half-up(numerator / denominator) using integers only (no float surprises)."""
    return (2 * numerator + denominator) // (2 * denominator)


DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def parse_date(value: str, name: str) -> Day:
    if not DATE_RE.fullmatch(value):
        fail(422, f"{name} must be YYYY-MM-DD", ("query", name))
    try:
        return Day.fromisoformat(value)
    except ValueError:
        fail(422, f"{name} is not a real calendar date", ("query", name))


def parse_month(value: str) -> tuple[Day, Day]:
    """'2026-07' -> (first day, last day) of that month."""
    if not MONTH_RE.fullmatch(value):
        fail(422, "month must be YYYY-MM", ("query", "month"))
    year, month = int(value[:4]), int(value[5:])
    try:
        first = Day(year, month, 1)
        nxt = Day(year + 1, 1, 1) if month == 12 else Day(year, month + 1, 1)
    except ValueError:
        fail(422, "month is out of range", ("query", "month"))
    return first, nxt - timedelta(days=1)


def count_working_days(first: Day, last: Day, joined_on: str) -> int:
    """R7: Mon-Fri days in [first, last], counted from joined_on for mid-month joiners."""
    start = max(first, Day.fromisoformat(joined_on))
    if start > last:
        return 0
    total = (last - start).days + 1
    weeks, extra = divmod(total, 7)
    count = weeks * 5
    weekday = start.weekday()
    for i in range(extra):  # at most 6 iterations
        if (weekday + i) % 7 < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Business rules (R1 - R5). Pure functions, no database access.
# --------------------------------------------------------------------------- #
def to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return to_minutes(shift_end) <= to_minutes(shift_start)


def ist_datetime(day: Day, hhmm: str) -> datetime:
    hours, minutes = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(hours), int(minutes), tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> Day:
    """R1: IST calendar date of the punch-in; overnight shifts started the previous day
    when the punch-in's IST time is earlier than shift_end."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp["shift_start"], emp["shift_end"]):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < to_minutes(emp["shift_end"]) * 60:
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, day: Day, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    delta = seconds_between(punch_in, ist_datetime(day, shift_start))
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in integer hundredths of an hour, rounded half-up: round(secs / 3600 * 100)."""
    return half_up_div(seconds_between(punch_out, punch_in) * 100, 3600)


def compute_overtime(punch_out: datetime, day: Day, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, counted only when >= 30. Overnight shifts end the next day."""
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    minutes = seconds_between(punch_out, ist_datetime(end_day, shift_end)) // 60  # floor
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: Day, emp: dict) -> dict:
    """All four derived fields for a record state (the table in DATA_MODEL.md section 3)."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hundredths = compute_work_hundredths(punch_in, punch_out)
    return {
        "work_hours": hundredths / 100,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"]),
        "half_day": hundredths < HALF_DAY_HUNDREDTHS,  # R5 uses the rounded value
    }


# --------------------------------------------------------------------------- #
# 4. Models and serialisers
# --------------------------------------------------------------------------- #
# Epoch milliseconds. strict=True rejects floats, strings and booleans; the bounds reject values
# that look like seconds (< 1e11) or are absurdly far in the future.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, value: str) -> str:
        Day.fromisoformat(value)  # ValueError -> 422
        return value

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatusT = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


TIME_FIELDS = ("punch_in", "punch_out")
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_record(doc: dict) -> dict:
    """Stored attendance document -> API shape (epoch ms, defaults for legacy gaps, no _id)."""
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            convert = dt_to_ms if key in TIME_FIELDS else (lambda v: v)
            changes[key] = {"from": convert(change.get("from")), "to": convert(change.get("to"))}
        history.append(
            {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}
        )
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


# --------------------------------------------------------------------------- #
# 5. Indexes (idempotent: create_index is a no-op when the index already exists)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    specs = [
        # employees
        (employees, [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
        (employees, [("joined_on", ASCENDING)], {"name": "joined_on"}),
        (employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
        # attendance_logs
        (logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_date_unique"}),
        (logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        (logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        (logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ]
    ok = True
    for collection, keys, options in specs:
        try:
            collection.create_index(keys, **options)
        except PyMongoError as exc:
            ok = False
            log.error("could not create index %s: %s", options["name"], exc)
    _indexes_ready = ok


# --------------------------------------------------------------------------- #
# 6. Employees and attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        fail(503, "database unavailable")
    if not _indexes_ready:  # MongoDB was down at startup: catch up as soon as it answers
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_ms()
    try:
        # The unique index on emp_code is the guarantee; no check-then-insert.
        employees.insert_one(dict(doc))
    except DuplicateKeyError:
        fail(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    total = employees.count_documents(query)
    cursor = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        fail(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        # Race-safe: the unique (emp_code, date) index lets exactly one concurrent insert win.
        logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        fail(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else utc_now()

    # Most recent record whose punch_in is at or before the punch-out; works across midnight.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        fail(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        fail(409, "already punched out")
    elapsed = seconds_between(ts, rec["punch_in"])
    if elapsed <= 0:
        fail(422, "punched_at must be after punch_in", ("body", "punched_at"))
    if elapsed > MAX_SHIFT_SECONDS:
        fail(422, "punched_at is more than 24 hours after punch_in", ("body", "punched_at"))

    derived = derive(rec["status"], rec["punch_in"], ts, Day.fromisoformat(rec["date"]), emp)
    # Compare-and-set: only closes the record if it is still open and unchanged, so two
    # simultaneous punch-outs (or a punch-out racing a regularization) cannot both win.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, **{k: v for k, v in derived.items() if k != "late_minutes"}}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "record was modified or already punched out")
    return serialize_record(updated)


def build_attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    start = parse_date(date_from, "date_from") if date_from is not None else None
    end = parse_date(date_to, "date_to") if date_to is not None else None
    if start and end and start > end:
        fail(422, "date_from must not be after date_to", ("query", "date_from"))
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start.isoformat()
        if end:
            query["date"]["$lte"] = end.isoformat()
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    # Everything (filter, sort, skip, limit) is done by MongoDB, backed by the indexes above.
    total = logs.count_documents(query) if query else logs.estimated_document_count()
    cursor = logs.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date")
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        fail(404, "employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if rec is None:
        fail(404, "no attendance record for that date")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            fail(422, f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        if new_in is None:
            fail(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp) != day:
            fail(422, "punch_in must stay on the record's attendance date", ("body", "punch_in"))
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_out is not None:
            elapsed = seconds_between(new_out, new_in)
            if elapsed <= 0 or elapsed > MAX_SHIFT_SECONDS:
                fail(422, "punch_out must be after punch_in and within 24 hours of it", ("body", "punch_out"))

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        fail(422, "the request changes nothing")

    old = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(new_status, new_in, new_out, day, emp)}
    changes = {k: {"from": old[k], "to": new[k]} for k in CHANGE_FIELDS if old[k] != new[k]}
    entry = {"at": utc_now_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record still looks exactly like the one we
    # read (same inputs, same number of history entries). A concurrent edit makes this a 409 instead of
    # silently dropping somebody's history entry.
    updated = logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "record was modified concurrently, retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics: aggregation pipelines
# --------------------------------------------------------------------------- #
# Reusable expression fragments. They read the STORED derived fields (never recompute from punches).
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_DAY_OF_WEEK = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}  # 1 = Sunday ... 7 = Saturday
IS_WEEKDAY = {"$and": [{"$gte": [_DAY_OF_WEEK, 2]}, {"$lte": [_DAY_OF_WEEK, 6]}]}
IS_HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}  # missing -> false
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def log_stats() -> dict:
    """$group accumulators shared by the employee-monthly and department-summary pipelines."""
    return {
        # R6/R7: presence on a weekday, half day = 0.5
        "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, {"$cond": [IS_HALF_DAY, 0.5, 1]}, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": LATE_MINUTES},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        # work hours are summed as integer hundredths so the later average can be rounded exactly
        "hours_count": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
        "hours_hundredths": {"$sum": {"$cond": [HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
    }


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # (emp_code, date) index
        {"$group": {"_id": None, **log_stats()}},
    ]


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from `employees` so people with zero logs are still counted (R9); each employee's
    month of logs is summarised inside $lookup using the (emp_code, date) index."""
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$group": {"_id": None, **log_stats()}},
                ],
                "as": "s",
            }
        },
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # keep employees with no logs
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{k: {"$sum": {"$ifNull": [f"$s.{k}", 0]}} for k in log_stats()},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},  # (date, emp_code) index
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": LATE_MINUTES},
                "late_count": {"$sum": {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_context(department: str, to_day: Day) -> tuple[list, list]:
    """Employee codes of the department, and [{d: joined_on, n: how many}] for headcount (R9)."""
    rows = list(
        employees.aggregate(
            [
                {"$match": {"department": department}},
                {"$group": {"_id": "$joined_on", "codes": {"$push": "$emp_code"}}},
            ]
        )
    )
    codes = [c for r in rows for c in r["codes"]]
    joins = [{"d": r["_id"], "n": len(r["codes"])} for r in rows if r["_id"] <= to_day.isoformat()]
    return codes, joins


def trend_pipeline(codes: list, joins: list, d_from: Day, d_to: Day) -> list:
    lower = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    after_upper = lower + timedelta(days=(d_to - d_from).days + 1)  # densify's upper bound is exclusive
    last = after_upper - timedelta(days=1)
    seed = {"_id": d_from.isoformat(), "p2": 0, "late_count": 0}  # keeps $densify fed if there are no logs at all
    weekday_d = {"$and": [{"$gte": [{"$dayOfWeek": "$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$d"}, 6]}]}
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        # per-day totals; p2 = presence in half-days (1 per half day, 2 per full day) so everything stays an integer
        {
            "$facet": {
                "rows": [
                    {
                        "$group": {
                            "_id": "$date",
                            "p2": {"$sum": {"$cond": [IS_PRESENT, {"$cond": [IS_HALF_DAY, 1, 2]}, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}},
                        }
                    }
                ]
            }
        },
        {"$project": {"rows": {"$cond": [{"$eq": [{"$size": "$rows"}, 0]}, {"$literal": [seed]}, "$rows"]}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: one row for every calendar day in the range, weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, after_upper]}}},
        {"$match": {"d": {"$gte": lower, "$lte": last}}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "p2": {"$ifNull": ["$p2", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": weekday_d,
            }
        },
        {
            "$set": {
                "headcount": {
                    "$reduce": {
                        "input": {"$filter": {"input": {"$literal": joins}, "as": "j", "cond": {"$lte": ["$$j.d", "$date"]}}},
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.n"]},
                    }
                }
            }
        },
        {
            "$set": {
                # attendance_rate in ten-thousandths, rounded half-up with integer arithmetic:
                # floor((p2/2 / h) * 10000 + 0.5) = floor((p2 * 10000 + h) / (2h))
                "r4": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$toLong": {
                                "$floor": {
                                    "$divide": [
                                        {"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                                        {"$multiply": [2, "$headcount"]},
                                    ]
                                }
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$set": {"has_rate": {"$cond": [{"$isNumber": "$r4"}, 1, 0]}}},
        # 7-day moving average over the rows inside the requested range only (shorter window at the start)
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "ma_sum": {"$sum": "$r4", "window": window},
                    "ma_count": {"$sum": "$has_rate", "window": window},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$p2", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$isNumber": "$r4"}, {"$divide": ["$r4", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$ma_count", 0]},
                        {
                            "$divide": [
                                {
                                    "$toLong": {
                                        "$floor": {
                                            "$divide": [
                                                {"$add": [{"$multiply": [2, "$ma_sum"]}, "$ma_count"]},
                                                {"$multiply": [2, "$ma_count"]},
                                            ]
                                        }
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def parse_trend_range(from_: str, to: str) -> tuple[Day, Day]:
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        fail(422, "'to' must not be before 'from'", ("query", "to"))
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        fail(422, f"range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    return d_from, d_to


# --- endpoints -------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = parse_month(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        fail(404, "employee not found")
    row = next(iter(logs.aggregate(monthly_pipeline(emp_code, first.isoformat(), last.isoformat()))), {})
    present2 = int(round(float(row.get("present_days", 0)) * 2))  # half-days, exact integer
    working_days = count_working_days(first, last, emp["joined_on"])
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present2 / 2,
        "leave_days": int(row.get("leave_count", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        # present/working*100, two decimals half-up, as exact integer arithmetic
        "attendance_pct": half_up_div(present2 * 5000, working_days) / 100 if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    first, last = parse_month(month)
    items = []
    for row in employees.aggregate(summary_pipeline(first.isoformat(), last.isoformat(), department)):
        count, hundredths = int(row["hours_count"]), int(row["hours_hundredths"])
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row["headcount"]),
                "present_days": int(round(float(row["present_days"]) * 2)) / 2,
                # mean over RECORDS (not an average of per-employee averages), two decimals half-up
                "avg_work_hours": half_up_div(hundredths, count) / 100 if count else None,
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = parse_month(month)
    items = list(logs.aggregate(leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = parse_trend_range(from_, to)
    codes, joins = department_context(department, d_to)
    if not codes:
        fail(404, "unknown department")
    return {"department": department, "items": list(logs.aggregate(trend_pipeline(codes, joins, d_from, d_to)))}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
# --------------------------------------------------------------------------- #
def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def require(value: Optional[Any], name: str) -> Any:
    if value is None:
        fail(422, f"{name} is required for this endpoint", ("query", name))
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explains the endpoint's main query, built by the very same functions the endpoint uses."""
    if endpoint == "attendance_list":
        query = build_attendance_query(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        first, last = parse_month(require(month, "month"))
        pipeline = monthly_pipeline(require(emp_code, "emp_code"), first.isoformat(), last.isoformat())
        command, collection = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "attendance_logs"
    elif endpoint == "department_summary":
        first, last = parse_month(require(month, "month"))
        pipeline = summary_pipeline(first.isoformat(), last.isoformat(), department)
        command, collection = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, "employees"
    elif endpoint == "late_leaderboard":
        first, last = parse_month(require(month, "month"))
        pipeline = leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        command, collection = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "attendance_logs"
    else:  # department_trend
        d_from, d_to = parse_trend_range(require(from_, "from"), require(to, "to"))
        codes, joins = department_context(require(department, "department"), d_to)
        if not codes:
            fail(404, "unknown department")
        pipeline = trend_pipeline(codes, joins, d_from, d_to)
        command, collection = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
