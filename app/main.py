"""Employee Attendance & Analytics API.

Run from this directory with ``uvicorn app.main:app --port 8000``.
"""
import logging
import os
import json
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson.decimal128 import Decimal128
from bson.json_util import CANONICAL_JSON_OPTIONS, dumps as bson_dumps

load_dotenv()

logger = logging.getLogger(__name__)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MILLIS = 100_000_000_000
MAX_EPOCH_MILLIS = 4_102_444_800_000
_indexes_initialized = False

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes() -> None:
    """Create the required identity and list-query indexes idempotently."""
    global _indexes_initialized
    if _indexes_initialized:
        return
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined_on"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employees_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_code_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp_code"
    )
    _indexes_initialized = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    # A Mongo outage should not prevent the process from serving a 503 readiness
    # response. Index creation is retried by /health after MongoDB recovers.
    try:
        ensure_indexes()
    except PyMongoError:
        logger.exception("MongoDB unavailable while creating indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Shared time and calculation helpers
# --------------------------------------------------------------------------- #
def epoch_millis_to_datetime(value: int) -> datetime:
    """Convert validated epoch milliseconds to a whole-second UTC datetime."""
    if type(value) is not int or not MIN_EPOCH_MILLIS <= value <= MAX_EPOCH_MILLIS:
        raise ValueError("timestamp must be epoch milliseconds in the supported range")
    return EPOCH + timedelta(seconds=value // 1000)


def datetime_to_epoch_millis(value: datetime | None) -> int | None:
    """Serialize a BSON datetime as epoch milliseconds after whole-second truncation."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    value = value.replace(microsecond=0)
    delta = value - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000


def truncate_to_seconds(value: datetime) -> datetime:
    """Return an instant at whole-second precision, preserving its timezone."""
    return value.replace(microsecond=0)


def _utc_whole_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return truncate_to_seconds(value)


def ist_calendar_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """Return the R1 attendance day for a punch-in instant."""
    local = punch_in.astimezone(IST)
    attendance_day = local.date()
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < time.fromisoformat(shift_end):
        attendance_day -= timedelta(days=1)
    return attendance_day


def shift_boundary(attendance_day: date | str, shift_time: str, *, overnight_end: bool = False) -> datetime:
    """Build a UTC instant for an IST shift boundary on the attendance day."""
    day = date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    if overnight_end:
        day += timedelta(days=1)
    hour, minute = map(int, shift_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date | str | None = None) -> int:
    """Apply the R2 10-minute grace and floor elapsed minutes from shift start."""
    punch_in = truncate_to_seconds(punch_in.astimezone(UTC))
    day = attendance_day or punch_in.astimezone(IST).date()
    start = shift_boundary(day, shift_start)
    elapsed_seconds = (punch_in - start).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: date | str, shift_start: str) -> int:
    """Apply R3, including an overnight shift end on the following day."""
    end = shift_boundary(attendance_day, shift_end, overnight_end=shift_end <= shift_start)
    elapsed_seconds = (truncate_to_seconds(punch_out.astimezone(UTC)) - end).total_seconds()
    minutes = int(elapsed_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """Return elapsed work hours rounded to two decimals using half-up rounding."""
    punch_in = _utc_whole_seconds(punch_in)
    punch_out = _utc_whole_seconds(punch_out)
    delta = punch_out - punch_in
    seconds = delta.days * 86_400 + delta.seconds
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def round_half_up(value: Decimal | int | float, places: int = 2) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def _record_for_api(record: dict) -> dict:
    """Remove Mongo identity and convert all contract instants to epoch millis."""
    result = {key: value for key, value in record.items() if key != "_id"}
    for key in ("punch_in", "punch_out"):
        result[key] = datetime_to_epoch_millis(result.get(key))
    result.setdefault("work_hours", None)
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    for entry in result["history"]:
        if "at" in entry:
            entry["at"] = datetime_to_epoch_millis(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = datetime_to_epoch_millis(change[side])
    return result


def _employee_for_api(employee: dict) -> dict:
    result = {key: value for key, value in employee.items() if key != "_id"}
    result["created_at"] = datetime_to_epoch_millis(result["created_at"])
    return result


# --------------------------------------------------------------------------- #
# Strict request models
# --------------------------------------------------------------------------- #
EmployeeCode = Annotated[str, StringConstraints(pattern=r"^EMP\d{4,6}$")]
ShiftTime = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
StatusIn = Literal["PRESENT", "WFH", "ON_DUTY"]


def _validate_calendar_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("must be a valid YYYY-MM-DD date")
    return value


DateString = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    AfterValidator(_validate_calendar_date),
]


def _validate_month(value: str) -> str:
    try:
        year, month = map(int, value.split("-"))
        date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError("must be a valid YYYY-MM month") from exc
    return value


MonthString = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    AfterValidator(_validate_month),
]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: EmployeeCode
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def validate_dates_and_shift(self):
        _validate_calendar_date(self.joined_on)
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)
    status: StatusIn = "PRESENT"

    @model_validator(mode="after")
    def explicit_timestamp_cannot_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an integer epoch-millisecond value when supplied")
        return self


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)

    @model_validator(mode="after")
    def explicit_timestamp_cannot_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an integer epoch-millisecond value when supplied")
        return self


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)
    punch_out: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def supplied_timestamps_and_status_must_be_non_null(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null when supplied")
        return self


# --------------------------------------------------------------------------- #
# Basic endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_initialized:
            ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _employee_for_api(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [_employee_for_api(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    instant = epoch_millis_to_datetime(body.punched_at) if body.punched_at is not None else truncate_to_seconds(datetime.now(UTC))
    attendance_day = ist_calendar_date(instant, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": instant,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(instant, employee["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return _record_for_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    instant = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    candidates = (
        db.attendance_logs.find({
            "emp_code": body.emp_code,
            "punch_in": {"$lte": instant, "$ne": None},
        })
        .sort("punch_in", DESCENDING)
        .limit(1)
    )
    record = next(iter(candidates), None)
    if record is None:
        future_punch_in = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$gt": instant, "$ne": None},
        })
        if future_punch_in is not None:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
        raise HTTPException(status_code=404, detail="no eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record is already punched out")

    punch_in_time = _utc_whole_seconds(record["punch_in"])
    if instant <= punch_in_time:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if instant - punch_in_time > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    attendance_day = date.fromisoformat(record["date"])
    work_hours = compute_work_hours(punch_in_time, instant)
    overtime_minutes = compute_overtime(
        instant, employee["shift_end"], attendance_day, employee["shift_start"]
    )
    update = {
        "$set": {
            "punch_out": instant,
            "work_hours": float(work_hours),
            "overtime_minutes": overtime_minutes,
            "half_day": work_hours < Decimal("4.50"),
        }
    }
    # This predicate is the concurrency guard: only the first request can
    # transition the selected open record from null/missing punch_out to closed.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_in": record["punch_in"], "punch_out": None},
        update,
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record is already punched out")
    return _record_for_api(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateString, body: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    status = body.status if "status" in supplied else record.get("status")
    punch_in_time = (
        epoch_millis_to_datetime(body.punch_in)
        if "punch_in" in supplied
        else (_utc_whole_seconds(record["punch_in"]) if record.get("punch_in") is not None else None)
    )
    punch_out_time = (
        epoch_millis_to_datetime(body.punch_out)
        if "punch_out" in supplied
        else (_utc_whole_seconds(record["punch_out"]) if record.get("punch_out") is not None else None)
    )

    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot be supplied with punch times")
        punch_in_time = None
        punch_out_time = None
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    else:
        if status not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise HTTPException(status_code=422, detail="invalid attendance status")
        if punch_in_time is None:
            raise HTTPException(status_code=422, detail="presence status requires a punch_in")
        attendance_day = datetime.fromisoformat(date).date()
        if ist_calendar_date(punch_in_time, employee["shift_start"], employee["shift_end"]) != attendance_day:
            raise HTTPException(status_code=422, detail="punch_in must fall on the record attendance date")

        late_minutes = compute_late_minutes(punch_in_time, employee["shift_start"], attendance_day)
        if punch_out_time is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            if punch_out_time <= punch_in_time:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if punch_out_time - punch_in_time > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
            rounded_work_hours = compute_work_hours(punch_in_time, punch_out_time)
            work_hours = float(rounded_work_hours)
            overtime_minutes = compute_overtime(
                punch_out_time, employee["shift_end"], attendance_day, employee["shift_start"]
            )
            half_day = rounded_work_hours < Decimal("4.50")

    new_values = {
        "status": status,
        "punch_in": punch_in_time,
        "punch_out": punch_out_time,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    defaults = {
        "status": None,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    changes = {}
    for field, new_value in new_values.items():
        old_value = record.get(field, defaults[field])
        if isinstance(old_value, datetime):
            old_value = _utc_whole_seconds(old_value)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="regularization does not change the record")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    guard = {"_id": record["_id"], "emp_code": emp_code, "date": date}
    for field in new_values:
        if field in record:
            guard[field] = record[field]
        else:
            guard[field] = {"$exists": False}
    if "history" in record:
        guard["history"] = record["history"]
    else:
        guard["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new_values, "$push": {"history": history_entry}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    return _record_for_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    _validate_attendance_date_range(date_from, date_to)

    plan = _attendance_list_plan(emp_code, date_from, date_to, status, page, page_size)
    query = plan["filter"]
    total = db.attendance_logs.count_documents(query)
    docs = (
        db.attendance_logs.find(query, plan["projection"])
        .sort(plan["sort"])
        .skip(plan["skip"])
        .limit(plan["limit"])
    )
    return {
        "items": [_record_for_api(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _validate_attendance_date_range(date_from: str | None, date_to: str | None) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        message = "date_from must be on or before date_to"
        raise RequestValidationError([{
            "type": "value_error",
            "loc": ("query", "date_from"),
            "msg": f"Value error, {message}",
            "input": date_from,
            "ctx": {"error": message},
        }])


def _attendance_list_plan(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> dict:
    """Return the actual find filter and cursor options used by GET /attendance."""
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to

    return {
        "filter": query,
        "projection": {"_id": 0},
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics. All document-level calculations are performed by MongoDB.
# --------------------------------------------------------------------------- #
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
D0 = Decimal128("0")
D_HALF = Decimal128("0.5")
D_ONE = Decimal128("1")


def _mongo_half_up(expression: object, places: int) -> dict:
    """Mongo expression for non-negative half-up rounding (R8)."""
    factor = Decimal128(str(10**places))
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, factor]}, D_HALF]}},
            factor,
        ]
    }


def _api_numbers(value):
    """Make Decimal128 aggregation results JSON serializable without _id."""
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, list):
        return [_api_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _api_numbers(item) for key, item in value.items() if key != "_id"}
    return value


def _month_bounds(month: str) -> tuple[str, str]:
    year, number = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def _monthly_log_metrics(emp_variable: str, start: str, end: str) -> list[dict]:
    """Lookup pipeline: aggregate stored metrics for one employee and month."""
    present = {"$in": ["$status", PRESENCE_STATUSES]}
    weekday = {"$lte": [{"$isoDayOfWeek": "$date_value"}, 5]}
    return [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", emp_variable]},
            {"$gte": ["$date", start]}, {"$lte": ["$date", end]},
        ]}}},
        {"$set": {
            "date_value": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "half_value": {"$ifNull": ["$half_day", False]},
            "late_value": {"$ifNull": ["$late_minutes", 0]},
            "overtime_value": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [weekday, present]},
                {"$cond": ["$half_value", D_HALF, D_ONE]}, D0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_value", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_value"},
            "total_overtime_minutes": {"$sum": "$overtime_value"},
        }},
    ]


def _working_days_expr(start_month: str, end_month: str) -> dict:
    """Count weekdays in the eligible joined_on..month_end interval in MongoDB."""
    return {"$cond": [
        {"$gt": ["$joined_on", end_month]}, 0,
        {"$let": {
            "vars": {"first": {"$dateFromString": {"dateString": {"$max": ["$joined_on", start_month]}, "format": "%Y-%m-%d", "timezone": "UTC"}},
                     "after_last": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": end_month, "format": "%Y-%m-%d", "timezone": "UTC"}}, "unit": "day", "amount": 1}}},
            "in": {"$sum": {"$map": {
                "input": {"$range": [0, {"$dateDiff": {"startDate": "$$first", "endDate": "$$after_last", "unit": "day"}}]},
                "as": "offset",
                "in": {"$cond": [{"$lte": [{"$isoDayOfWeek": {"$dateAdd": {"startDate": "$$first", "unit": "day", "amount": "$$offset"}}}, 5]}, 1, 0]},
            }}}
        }}
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    result = next(db.employees.aggregate(_employee_monthly_pipeline(emp_code, month)), None)
    if result is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return _api_numbers(result)


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {"working_days": _working_days_expr(start, end)}},
        {"$lookup": {"from": "attendance_logs", "let": {"employee_code": "$emp_code"},
                      "pipeline": _monthly_log_metrics("$$employee_code", start, end), "as": "metrics"}},
        {"$set": {"metrics": {"$ifNull": [{"$arrayElemAt": ["$metrics", 0]}, {
            "present_days": D0, "leave_days": 0, "late_count": 0,
            "total_late_minutes": 0, "total_overtime_minutes": 0,
        }]}}},
        {"$set": {"attendance_pct": {"$cond": [
            {"$eq": ["$working_days", 0]}, None,
            _mongo_half_up({"$multiply": [
                {"$divide": ["$metrics.present_days", {"$toDecimal": "$working_days"}]}, Decimal128("100")
            ]}, 2),
        ]}}},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1,
                      "present_days": "$metrics.present_days", "leave_days": "$metrics.leave_days",
                      "late_count": "$metrics.late_count", "total_late_minutes": "$metrics.total_late_minutes",
                      "total_overtime_minutes": "$metrics.total_overtime_minutes", "attendance_pct": 1,
                      "month": {"$literal": month}}},
    ]


def _department_metrics_lookup(start: str, end: str) -> list[dict]:
    present = {"$in": ["$status", PRESENCE_STATUSES]}
    weekday = {"$lte": [{"$isoDayOfWeek": "$date_value"}, 5]}
    qualifies_work = {"$and": [present, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$employee_code"]},
            {"$gte": ["$date", start]}, {"$lte": ["$date", end]},
        ]}}},
        {"$set": {"date_value": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                   "half_value": {"$ifNull": ["$half_day", False]}, "late_value": {"$ifNull": ["$late_minutes", 0]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [weekday, present]}, {"$cond": ["$half_value", D_HALF, D_ONE]}, D0]}},
            "work_hours_sum": {"$sum": {"$cond": [qualifies_work, {"$toDecimal": "$work_hours"}, D0]}},
            "work_hours_count": {"$sum": {"$cond": [qualifies_work, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_value", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_value"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    items = [_api_numbers(item) for item in db.employees.aggregate(_department_summary_pipeline(month, department))]
    return {"month": month, "items": items}


def _department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    start, end = _month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    empty = {"present_days": D0, "work_hours_sum": D0, "work_hours_count": 0,
             "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"employee_code": "$emp_code"},
                      "pipeline": _department_metrics_lookup(start, end), "as": "metrics"}},
        {"$set": {"metrics": {"$ifNull": [{"$arrayElemAt": ["$metrics", 0]}, empty]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$metrics.present_days"},
            "work_hours_sum": {"$sum": "$metrics.work_hours_sum"},
            "work_hours_count": {"$sum": "$metrics.work_hours_count"},
            "late_count": {"$sum": "$metrics.late_count"},
            "total_late_minutes": {"$sum": "$metrics.total_late_minutes"},
            "leave_count": {"$sum": "$metrics.leave_count"},
            "on_duty_count": {"$sum": "$metrics.on_duty_count"},
        }},
        {"$set": {"avg_work_hours": {"$cond": [
            {"$eq": ["$work_hours_count", 0]}, None,
            _mongo_half_up({"$divide": ["$work_hours_sum", {"$toDecimal": "$work_hours_count"}]}, 2),
        ]}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
                       "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1,
                       "leave_count": 1, "on_duty_count": 1}},
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthString,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
):
    items = [_api_numbers(item) for item in db.attendance_logs.aggregate(_late_leaderboard_pipeline(month, limit, department))]
    return {"month": month, "items": items}


def _late_leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict]:
    start, end = _month_bounds(month)
    employee_match: dict = {"$expr": {"$eq": ["$emp_code", "$$employee_code"]}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "let": {"employee_code": "$_id"}, "pipeline": [
            {"$match": employee_match}, {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1}},
        ], "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$employee.emp_code", "name": "$employee.name",
                       "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]


def _trend_daily_lookup(department: str) -> list[dict]:
    presence = {"$in": ["$status", PRESENCE_STATUSES]}
    employee_filter = [{"$match": {"$expr": {"$and": [
        {"$eq": ["$emp_code", "$$log_employee"]}, {"$eq": ["$department", department]},
    ]}}}, {"$project": {"_id": 0, "emp_code": 1}}]
    return [
        {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
        {"$lookup": {"from": "employees", "let": {"log_employee": "$emp_code"}, "pipeline": employee_filter, "as": "department_employee"}},
        {"$match": {"$expr": {"$gt": [{"$size": "$department_employee"}, 0]}}},
        {"$group": {"_id": None,
            "present_count": {"$sum": {"$cond": [presence, {"$cond": [{"$ifNull": ["$half_day", False]}, D_HALF, D_ONE]}, D0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: DateString = Query(alias="from"), to_date: DateString = Query(alias="to")):
    start_day, end_day = _validated_trend_range(from_date, to_date)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")

    items = [_api_numbers(item) for item in db.employees.aggregate(_department_trend_pipeline(department, start_day, end_day))]
    return {"department": department, "items": items}


def _validated_trend_range(from_date: str, to_date: str) -> tuple[date, date]:
    try:
        _validate_calendar_date(from_date)
        _validate_calendar_date(to_date)
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="from and to must be valid YYYY-MM-DD dates") from exc
    inclusive_days = (end_day - start_day).days + 1
    if inclusive_days < 1 or inclusive_days > 92:
        raise HTTPException(status_code=422, detail="date range must be 1 to 92 days inclusive")
    return start_day, end_day


EXPLAIN_ENDPOINTS = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _explain_missing_parameters(names: list[str]) -> None:
    if names:
        raise RequestValidationError([{
            "type": "missing", "loc": ("query", name), "msg": "Field required", "input": None,
        } for name in names])


def _raw_explain_json(result: dict) -> dict:
    """Use canonical Extended JSON so BSON values retain their BSON meaning."""
    return json.loads(bson_dumps(result, json_options=CANONICAL_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EXPLAIN_ENDPOINTS,
    emp_code: str | None = None,
    month: MonthString | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: DateString | None = None,
    date_to: DateString | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    from_date: DateString | None = Query(default=None, alias="from"),
    to_date: DateString | None = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Explain the same database operation as one supported endpoint."""
    if endpoint == "attendance_list":
        _validate_attendance_date_range(date_from, date_to)
        plan = _attendance_list_plan(emp_code, date_from, date_to, status, page, page_size)
        operation = {
            "find": "attendance_logs", "filter": plan["filter"], "projection": plan["projection"],
            "sort": dict(plan["sort"]), "skip": plan["skip"], "limit": plan["limit"],
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        _explain_missing_parameters([key for key, value in (("emp_code", emp_code), ("month", month)) if value is None])
        operation = {"aggregate": "employees", "pipeline": _employee_monthly_pipeline(emp_code, month), "cursor": {}}
        collection = "employees"
    elif endpoint == "department_summary":
        _explain_missing_parameters(["month"] if month is None else [])
        operation = {"aggregate": "employees", "pipeline": _department_summary_pipeline(month, department), "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        _explain_missing_parameters(["month"] if month is None else [])
        operation = {"aggregate": "attendance_logs", "pipeline": _late_leaderboard_pipeline(month, limit, department), "cursor": {}}
        collection = "attendance_logs"
    else:  # department_trend
        _explain_missing_parameters([key for key, value in (("department", department), ("from", from_date), ("to", to_date)) if value is None])
        start_day, end_day = _validated_trend_range(from_date, to_date)
        operation = {"aggregate": "employees", "pipeline": _department_trend_pipeline(department, start_day, end_day), "cursor": {}}
        collection = "employees"

    # Keep the endpoint identifier outside the Mongo command itself.
    return _explain_response(endpoint, collection, operation)


def _explain_response(endpoint: str, collection: str, operation: dict) -> dict:
    result = db.command("explain", operation, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": _raw_explain_json(result)}


def _department_trend_pipeline(department: str, start_day: date, end_day: date) -> list[dict]:
    lower = datetime.combine(start_day, time.min, tzinfo=UTC)
    upper = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        # This first lookup only needs a seed row. The subsequent lookups fetch
        # the full department population for every date in the requested range.
        {"$limit": 1},
        {"$group": {"_id": None}},
        {"$set": {"series_day": lower}},
        {"$densify": {"field": "series_day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$set": {"day_string": {"$dateToString": {"date": "$series_day", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {"from": "employees", "let": {"day_string": "$day_string"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day_string"]}}},
            {"$project": {"_id": 0, "emp_code": 1}},
        ], "as": "day_employees"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day_string": "$day_string"},
                      "pipeline": _trend_daily_lookup(department), "as": "daily_metrics"}},
        {"$set": {"daily_metrics": {"$ifNull": [{"$arrayElemAt": ["$daily_metrics", 0]}, {"present_count": D0, "late_count": 0}]},
                   "headcount": {"$size": "$day_employees"},
                   "iso_day": {"$isoDayOfWeek": "$series_day"}}},
        {"$set": {"is_working_day": {"$lte": ["$iso_day", 5]}, "present_count": "$daily_metrics.present_count",
                   "late_count": "$daily_metrics.late_count"}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _mongo_half_up({"$divide": ["$present_count", {"$toDecimal": "$headcount"}]}, 4), None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"series_day": 1}, "output": {
            "moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$set": {"moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_raw", None]}, None, _mongo_half_up("$moving_avg_raw", 4)]}}},
        {"$sort": {"series_day": 1}},
        {"$project": {"_id": 0, "date": "$day_string", "is_working_day": 1, "headcount": 1,
                       "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]
