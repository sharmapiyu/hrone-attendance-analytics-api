"""Focused API tests using an in-memory PyMongo test double.

These tests do not connect to MongoDB. Real database verification is separate
and must use an isolated, disposable database.
"""
import sys
import threading
import types
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from bson import ObjectId

import pytest
from fastapi.testclient import TestClient


class DuplicateKeyError(Exception):
    pass


class PyMongoError(Exception):
    pass


class Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec, direction=None):
        fields = spec if isinstance(spec, list) else [(spec, direction)]
        # Stable multi-key sort: apply the least-significant key first.
        for key, order in reversed(fields):
            self.docs.sort(key=lambda doc: doc.get(key), reverse=order == -1)
        return self

    def skip(self, count):
        self.docs = self.docs[count:]
        return self

    def limit(self, count):
        self.docs = self.docs[:count]
        return self

    def __iter__(self):
        return iter(self.docs)


def _matches(doc, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, branch) for branch in expected):
                return False
            continue
        actual = doc.get(key)
        if isinstance(expected, dict):
            if "$exists" in expected and (key in doc) != expected["$exists"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$gte" in expected and actual < expected["$gte"]:
                return False
            if "$lte" in expected and actual > expected["$lte"]:
                return False
            if "$gt" in expected and actual <= expected["$gt"]:
                return False
            if not any(operator in expected for operator in ("$exists", "$ne", "$gte", "$lte", "$gt")) and actual != expected:
                return False
        elif actual != expected:
            return False
    return True


class Collection:
    def __init__(self, unique=()):
        self.docs = []
        self.unique = unique
        self.lock = threading.Lock()
        self.indexes = []
        self.aggregate_result = []
        self.aggregate_pipeline = None

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def insert_one(self, doc):
        with self.lock:
            if any(all(old.get(key) == doc.get(key) for key in self.unique) for old in self.docs):
                raise DuplicateKeyError("duplicate")
            self.docs.append(copy.deepcopy(dict(doc, _id="internal")))

    def find_one(self, query, projection=None):
        with self.lock:
            for doc in self.docs:
                if _matches(doc, query):
                    return {k: copy.deepcopy(v) for k, v in doc.items() if not (projection or {}).get(k)}
        return None

    def find(self, query, projection=None):
        with self.lock:
            docs = [copy.deepcopy(doc) for doc in self.docs if _matches(doc, query)]
        if projection:
            docs = [{k: v for k, v in doc.items() if not projection.get(k)} for doc in docs]
        return Cursor(docs)

    def count_documents(self, query):
        with self.lock:
            return sum(_matches(doc, query) for doc in self.docs)

    def aggregate(self, pipeline):
        self.aggregate_pipeline = copy.deepcopy(pipeline)
        return iter(copy.deepcopy(self.aggregate_result))

    def find_one_and_update(self, query, update, projection=None, return_document=None):
        with self.lock:
            for stored in self.docs:
                if not _matches(stored, query):
                    continue
                for key, value in update.get("$set", {}).items():
                    stored[key] = copy.deepcopy(value)
                for key, value in update.get("$push", {}).items():
                    stored.setdefault(key, []).append(copy.deepcopy(value))
                result = copy.deepcopy(stored)
                return {k: v for k, v in result.items() if not (projection or {}).get(k)}
        return None


class FakeDB:
    def __init__(self):
        self.employees = Collection(unique=("emp_code",))
        self.attendance_logs = Collection(unique=("emp_code", "date"))
        self.explain_result = {"ok": 1.0, "executionStats": {"nReturned": 3}}
        self.command_calls = []

    def __getitem__(self, _):
        return self

    def command(self, name, operation, **kwargs):
        self.command_calls.append((name, copy.deepcopy(operation), copy.deepcopy(kwargs)))
        return copy.deepcopy(self.explain_result)


fake_db = FakeDB()


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.admin = types.SimpleNamespace(command=lambda _: {"ok": 1})

    def __getitem__(self, _):
        return fake_db


# Replace PyMongo only in this pytest process so unit tests remain isolated.
fake_pymongo = types.ModuleType("pymongo")
fake_pymongo.ASCENDING = 1
fake_pymongo.DESCENDING = -1
fake_pymongo.ReturnDocument = types.SimpleNamespace(AFTER=True, BEFORE=False)
fake_pymongo.MongoClient = FakeClient
fake_errors = types.ModuleType("pymongo.errors")
fake_errors.DuplicateKeyError = DuplicateKeyError
fake_errors.PyMongoError = PyMongoError
fake_pymongo.errors = fake_errors
sys.modules["pymongo"] = fake_pymongo
sys.modules["pymongo.errors"] = fake_errors

from app import main  # noqa: E402


@pytest.fixture(autouse=True)
def reset_database(monkeypatch):
    fake_db.employees = Collection(unique=("emp_code",))
    fake_db.attendance_logs = Collection(unique=("emp_code", "date"))
    monkeypatch.setattr(main, "db", fake_db)
    monkeypatch.setattr(main, "_indexes_initialized", False)


@pytest.fixture
def api():
    with TestClient(main.app) as test_client:
        yield test_client


def employee_body(emp_code="EMP0001", **overrides):
    data = {
        "emp_code": emp_code,
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    data.update(overrides)
    return data


def _epoch_ms(value):
    return int(value.timestamp() * 1000)


def test_employee_validation_and_duplicate(api):
    assert api.post("/employees", json=employee_body()).status_code == 201
    assert api.post("/employees", json=employee_body()).status_code == 409
    for body in (
        employee_body(emp_code="E1"),
        employee_body(email="not-an-email"),
        employee_body(joined_on="2026-02-30"),
        employee_body(shift_start="25:00"),
        employee_body(shift_start="09:30", shift_end="09:30"),
    ):
        assert api.post("/employees", json=body).status_code == 422


def test_employee_response_and_pagination(api):
    api.post("/employees", json=employee_body("EMP0002", department="Sales"))
    api.post("/employees", json=employee_body("EMP0001"))
    first = api.get("/employees", params={"page": 1, "page_size": 1})
    assert first.status_code == 200
    assert first.json()["items"][0]["emp_code"] == "EMP0001"
    assert first.json()["total"] == 2
    assert "_id" not in first.json()["items"][0] and "id" not in first.json()["items"][0]
    second = api.get("/employees", params={"page": 2, "page_size": 1}).json()
    assert second["items"][0]["emp_code"] == "EMP0002"
    assert api.get("/employees", params={"department": "sales"}).json()["total"] == 0
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_timestamp_validation_and_overnight(api):
    api.post("/employees", json=employee_body("EMP0005", shift_start="22:00", shift_end="06:00"))
    # 05:59:59 IST on July 7 belongs to the overnight shift that began July 6.
    local = datetime(2026, 7, 7, 5, 59, 59, tzinfo=main.IST)
    punch = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0005", "punched_at": _epoch_ms(local), "work_hours": 999,
    })
    assert punch.status_code == 201
    record = punch.json()
    assert record["date"] == "2026-07-06"
    assert record["punch_in"] == (_epoch_ms(local) // 1000) * 1000
    assert "_id" not in record and "id" not in record
    for invalid in (1783312500, 1783312500000.0, "1783312500000", 99_999_999_999, 4_102_444_800_001):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": invalid}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": None}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "missing"}).status_code == 404
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "status": "ABSENT"}).status_code == 422


def test_concurrent_duplicate_punch_ins(api):
    api.post("/employees", json=employee_body())
    stamp = _epoch_ms(datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc))
    def make_request(_):
        return api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": stamp}).status_code
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(make_request, range(8)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 7


def test_attendance_filters_sort_and_pagination(api):
    api.post("/employees", json=employee_body())
    dates = ["2026-07-06", "2026-07-07", "2026-07-08"]
    for day in dates:
        main.db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": day, "status": "PRESENT", "punch_in": None,
            "punch_out": None, "work_hours": None,
        })
    result = api.get("/attendance", params={"page": 1, "page_size": 2, "date_from": dates[0], "date_to": dates[2]})
    assert result.status_code == 200
    assert [item["date"] for item in result.json()["items"]] == [dates[2], dates[1]]
    assert result.json()["total"] == 3
    assert result.json()["items"][0]["history"] == []
    assert api.get("/attendance", params={"date_from": dates[2], "date_to": dates[0]}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert api.get("/attendance", params={"page_size": 101}).status_code == 422


def test_timestamp_conversion_and_lateness_boundaries():
    instant = datetime(2026, 7, 6, 4, 10, 0, 900_000, tzinfo=timezone.utc)
    millis = _epoch_ms(instant)
    stored = main.epoch_millis_to_datetime(millis)
    assert stored.microsecond == 0
    assert stored.tzinfo == timezone.utc
    assert main.datetime_to_epoch_millis(stored) == (millis // 1000) * 1000
    start_date = "2026-07-06"
    exact_grace = datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc)  # 09:40 IST
    after_grace = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert main.compute_late_minutes(exact_grace, "09:30", start_date) == 0
    assert main.compute_late_minutes(after_grace, "09:30", start_date) == 10
    sample_early = datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc)  # 21:55 IST
    assert main.compute_late_minutes(sample_early, "22:00", start_date) == 0
    next_morning = datetime(2026, 7, 7, 0, 29, tzinfo=timezone.utc)  # 05:59 IST
    assert main.compute_late_minutes(next_morning, "22:00", start_date) == 479


def test_overtime_boundaries_and_half_up_rounding():
    day = "2026-07-06"
    before_threshold = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)  # 18:59:59 IST
    at_threshold = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
    assert main.compute_overtime(before_threshold, "18:30", day, "09:30") == 0
    assert main.compute_overtime(at_threshold, "18:30", day, "09:30") == 30
    overnight_out = datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc)  # 06:40 IST
    assert main.compute_overtime(overnight_out, "06:00", day, "22:00") == 40
    assert main.round_half_up("2.345", 2) == Decimal("2.35")


def test_health_returns_503_when_mongo_fails(api, monkeypatch):
    def fail(_):
        raise PyMongoError("offline")
    monkeypatch.setattr(main.client.admin, "command", fail)
    assert api.get("/health").status_code == 503


def test_startup_creates_required_indexes(api):
    assert api.get("/health").status_code == 200
    employee_indexes = {kwargs["name"]: kwargs for _, kwargs in fake_db.employees.indexes}
    attendance_indexes = {kwargs["name"]: kwargs for _, kwargs in fake_db.attendance_logs.indexes}
    assert employee_indexes["uq_employees_emp_code"]["unique"] is True
    assert "ix_employees_department_emp_code" in employee_indexes
    assert "ix_employees_department_joined_on" in employee_indexes
    assert any(
        keys == [("joined_on", 1)] and kwargs["name"] == "ix_employees_joined_on"
        for keys, kwargs in fake_db.employees.indexes
    )
    assert attendance_indexes["uq_attendance_emp_code_date"]["unique"] is True
    assert "ix_attendance_date_emp_code" in attendance_indexes


def add_employee(api, emp_code="EMP1001", shift_start="09:30", shift_end="18:30"):
    response = api.post("/employees", json=employee_body(
        emp_code, shift_start=shift_start, shift_end=shift_end,
    ))
    assert response.status_code == 201


def add_attendance(emp_code, day, punch_in, **overrides):
    record = {
        "emp_code": emp_code,
        "date": day,
        "status": "PRESENT",
        "punch_in": punch_in,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    record.update(overrides)
    main.db.attendance_logs.insert_one(record)


def test_punch_out_unknown_missing_and_invalid_boundaries(api):
    unknown = api.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert unknown.status_code == 404
    add_employee(api)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP1001"}).status_code == 404

    punch_in = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-01", punch_in)
    for timestamp in (punch_in, punch_in - timedelta(seconds=1)):
        response = api.post("/attendance/punch-out", json={
            "emp_code": "EMP1001", "punched_at": _epoch_ms(timestamp),
        })
        assert response.status_code == 422
    assert api.post("/attendance/punch-out", json={
        "emp_code": "EMP1001", "punched_at": _epoch_ms(punch_in + timedelta(hours=24, seconds=1)),
    }).status_code == 422
    exact_24 = punch_in + timedelta(hours=24)
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": _epoch_ms(exact_24)})
    assert response.status_code == 200
    assert response.json()["work_hours"] == 24.0
    assert response.json()["history"] == []
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": _epoch_ms(exact_24)}).status_code == 409


def test_punch_out_work_hours_half_day_and_overtime_boundaries(api):
    add_employee(api)
    start = datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)
    # A 4:29:41 work interval rounds to 4.49 hours and is half day.
    add_attendance("EMP1001", "2026-06-02", start)
    out = start + timedelta(seconds=16_181)
    half = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": _epoch_ms(out)}).json()
    assert half["work_hours"] == 4.49 and half["half_day"] is True
    assert half["overtime_minutes"] == 0

    # 4:29:42 rounds half-up to 4.50, which is not a half day.
    add_attendance("EMP1001", "2026-06-03", start + timedelta(days=1))
    start2 = start + timedelta(days=1)
    out2 = start2 + timedelta(seconds=16_182)
    full_boundary = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": _epoch_ms(out2)}).json()
    assert full_boundary["work_hours"] == 4.5 and full_boundary["half_day"] is False

    add_attendance("EMP1001", "2026-06-04", start + timedelta(days=2))
    next_start = start + timedelta(days=2)
    below = api.post("/attendance/punch-out", json={
        "emp_code": "EMP1001", "punched_at": _epoch_ms(next_start + timedelta(hours=9, minutes=29, seconds=59)),
    }).json()
    assert below["overtime_minutes"] == 0
    next_day_start = start + timedelta(days=3)
    add_attendance("EMP1001", "2026-06-05", next_day_start)
    exact_threshold = api.post("/attendance/punch-out", json={
        "emp_code": "EMP1001", "punched_at": _epoch_ms(next_day_start + timedelta(hours=9, minutes=30)),
    }).json()
    assert exact_threshold["overtime_minutes"] == 30


def test_punch_out_overnight_shift_anchor_and_overtime(api):
    add_employee(api, shift_start="22:00", shift_end="06:00")
    punch_in = datetime(2026, 6, 1, 16, 25, tzinfo=timezone.utc)  # 21:55 IST
    punch_out = datetime(2026, 6, 2, 1, 10, tzinfo=timezone.utc)  # 06:40 IST
    add_attendance("EMP1001", "2026-06-01", punch_in)
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": _epoch_ms(punch_out)})
    assert result.status_code == 200
    assert result.json()["date"] == "2026-06-01"
    assert result.json()["work_hours"] == 8.75
    assert result.json()["overtime_minutes"] == 40
    assert result.json()["history"] == []


def test_punch_out_chooses_most_recent_eligible_record(api):
    add_employee(api)
    older = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-01", older)
    add_attendance("EMP1001", "2026-06-02", newer)
    result = api.post("/attendance/punch-out", json={
        "emp_code": "EMP1001", "punched_at": _epoch_ms(older + timedelta(hours=9)),
    })
    assert result.status_code == 200
    assert result.json()["date"] == "2026-06-01"
    newer_record = main.db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-06-02"})
    assert newer_record["punch_out"] is None


def test_concurrent_punch_out_exactly_one_winner(api):
    from concurrent.futures import ThreadPoolExecutor
    add_employee(api)
    punch_in = datetime(2026, 6, 5, 4, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 6, 5, 13, 0, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-05", punch_in)
    payload = {"emp_code": "EMP1001", "punched_at": _epoch_ms(punch_out)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: api.post("/attendance/punch-out", json=payload).status_code, range(8)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 7
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-06-05"})
    assert stored["history"] == []


def test_regularize_validation_and_missing_records(api):
    body = {"reason": "valid correction", "regularized_by": "hr.admin"}
    assert api.patch("/attendance/EMP9999/2026-06-01", json=body).status_code == 404
    add_employee(api)
    assert api.patch("/attendance/EMP1001/2026-06-01", json=body).status_code == 404

    add_attendance("EMP1001", "2026-06-01", None, status="ABSENT", punch_out=None, work_hours=None)
    path = "/attendance/EMP1001/2026-06-01"
    for invalid_body in (
        {"reason": "tiny", "regularized_by": "hr.admin"},
        {"reason": "valid correction", "regularized_by": ""},
        {"reason": "valid correction", "regularized_by": "hr.admin", "status": "INVALID"},
        {"reason": "valid correction", "regularized_by": "hr.admin", "punch_in": "1783312500000"},
        {"reason": "valid correction", "regularized_by": "hr.admin", "punch_in": None},
    ):
        assert api.patch(path, json=invalid_body).status_code == 422
    assert api.patch("/attendance/EMP1001/2026-02-30", json=body).status_code == 422
    assert api.patch(path, json=body).status_code == 422  # no-op
    assert api.patch(path, json={**body, "status": "PRESENT"}).status_code == 422  # missing punch_in
    assert api.patch(path, json={**body, "status": "ABSENT", "punch_in": _epoch_ms(datetime(2026,6,1,tzinfo=timezone.utc))}).status_code == 422


def test_regularize_absent_leave_clears_times_and_audits_actual_changes(api):
    add_employee(api)
    punch_in = datetime(2026, 6, 1, 4, 35, tzinfo=timezone.utc)
    punch_out = datetime(2026, 6, 1, 13, 40, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-01", punch_in, punch_out=punch_out,
                   work_hours=9.08, late_minutes=35, overtime_minutes=40, half_day=False)
    body = {"status": "ABSENT", "reason": "approved correction", "regularized_by": "hr.admin"}
    result = api.patch("/attendance/EMP1001/2026-06-01", json=body)
    assert result.status_code == 200
    record = result.json()
    assert record["punch_in"] is None and record["punch_out"] is None
    assert record["work_hours"] is None and record["late_minutes"] == 0
    assert record["overtime_minutes"] == 0 and record["half_day"] is False
    changes = record["history"][-1]["changes"]
    assert set(changes) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
    assert changes["punch_in"]["from"] == _epoch_ms(punch_in)
    assert changes["punch_in"]["to"] is None
    next_result = api.patch("/attendance/EMP1001/2026-06-01", json={
        "status": "LEAVE", "reason": "change category", "regularized_by": "hr.admin",
    })
    assert next_result.status_code == 200
    assert next_result.json()["status"] == "LEAVE"
    assert set(next_result.json()["history"][-1]["changes"]) == {"status"}


def test_regularize_presence_recomputes_and_legacy_defaults(api):
    add_employee(api)
    original_in = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-01", original_in, history=None)
    # Simulate legacy shape: no history and no half_day fields.
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-06-01"})
    stored.pop("history", None)
    stored.pop("half_day", None)
    # Replace fake record directly to retain this precise legacy shape.
    main.db.attendance_logs.docs[0] = stored

    corrected_in = datetime(2026, 6, 1, 4, 10, 1, tzinfo=timezone.utc)
    result = api.patch("/attendance/EMP1001/2026-06-01", json={
        "punch_in": _epoch_ms(corrected_in), "reason": "biometric correction", "regularized_by": "hr.admin",
    })
    assert result.status_code == 200
    item = result.json()
    assert item["punch_in"] == _epoch_ms(corrected_in)
    assert item["late_minutes"] == 10 and item["half_day"] is False
    assert set(item["history"][-1]["changes"]) == {"punch_in", "late_minutes"}
    assert item["history"][-1]["by"] == "hr.admin"
    assert isinstance(item["history"][-1]["at"], int)


def test_regularize_invalid_punch_combinations_and_date(api):
    add_employee(api)
    punch_in = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-01", punch_in)
    path = "/attendance/EMP1001/2026-06-01"
    base = {"reason": "valid correction", "regularized_by": "hr.admin"}
    wrong_day = datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)
    assert api.patch(path, json={**base, "punch_in": _epoch_ms(wrong_day)}).status_code == 422
    assert api.patch(path, json={**base, "punch_out": _epoch_ms(punch_in)}).status_code == 422
    assert api.patch(path, json={**base, "punch_out": _epoch_ms(punch_in + timedelta(hours=24, seconds=1))}).status_code == 422
    assert api.patch(path, json={**base, "punch_out": _epoch_ms(punch_in + timedelta(hours=4))}).status_code == 200


def test_concurrent_regularizations_preserve_each_successful_history(api):
    from concurrent.futures import ThreadPoolExecutor
    add_employee(api)
    start = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    add_attendance("EMP1001", "2026-06-01", start)
    path = "/attendance/EMP1001/2026-06-01"
    requests = [
        {"punch_in": _epoch_ms(start + timedelta(minutes=n)),
         "reason": f"correction {n}", "regularized_by": f"hr{n}"}
        for n in (1, 2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda payload: api.patch(path, json=payload), requests))
    codes = [response.status_code for response in responses]
    assert all(code in (200, 409) for code in codes)
    record = main.db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-06-01"})
    assert len(record["history"]) == codes.count(200)
    assert len(record["history"]) >= 1


def test_employee_monthly_contract_validation_and_pipeline(api):
    main.db.employees.aggregate_result = [{
        "_id": "internal", "emp_code": "EMP2001", "month": "2026-07", "working_days": 22,
        "present_days": 1.5, "leave_days": 1, "late_count": 1, "total_late_minutes": 17,
        "total_overtime_minutes": 42, "attendance_pct": 6.82,
    }]
    result = api.get("/analytics/employees/EMP2001/monthly", params={"month": "2026-07"})
    assert result.status_code == 200
    item = result.json()
    assert set(item) == {"emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
                         "total_late_minutes", "total_overtime_minutes", "attendance_pct"}
    assert item["present_days"] == 1.5 and item["attendance_pct"] == 6.82
    pipeline = main.db.employees.aggregate_pipeline
    assert pipeline[0] == {"$match": {"emp_code": "EMP2001"}}
    assert pipeline[1]["$set"]["working_days"]["$cond"]
    lookup = pipeline[2]["$lookup"]["pipeline"]
    assert any("$group" in stage for stage in lookup)
    assert lookup[1]["$set"]["late_value"] == {"$ifNull": ["$late_minutes", 0]}
    main.db.employees.aggregate_result = []
    assert api.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    for month in ("2026-00", "2026-13", "2026-2", "2026-02-01", "0000-01"):
        assert api.get("/analytics/employees/EMP2001/monthly", params={"month": month}).status_code == 422


def test_department_summary_pipeline_and_monthly_filter(api):
    main.db.employees.aggregate_result = [{
        "department": "Engineering", "headcount": 2, "present_days": 1.5, "avg_work_hours": 8.35,
        "late_count": 1, "total_late_minutes": 11, "leave_count": 1, "on_duty_count": 0,
    }]
    result = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"})
    assert result.status_code == 200
    assert result.json() == {"month": "2026-07", "items": main.db.employees.aggregate_result}
    stages = main.db.employees.aggregate_pipeline
    assert stages[0]["$match"] == {"joined_on": {"$lte": "2026-07-31"}, "department": "Engineering"}
    assert stages[1]["$lookup"]["from"] == "attendance_logs"
    assert stages[2]["$set"]["metrics"]["$ifNull"]
    inner = stages[1]["$lookup"]["pipeline"]
    assert inner[-1]["$group"]["work_hours_sum"]["$sum"]["$cond"][1] == {"$toDecimal": "$work_hours"}
    assert stages[-2] == {"$sort": {"_id": 1}}
    assert api.get("/analytics/departments/summary", params={"month": "2026-13"}).status_code == 422


def test_late_leaderboard_competition_rank_pipeline_and_ties(api):
    main.db.attendance_logs.aggregate_result = [
        {"rank": 1, "emp_code": "EMP2001", "name": "A", "department": "Engineering", "total_late_minutes": 20, "late_count": 2},
        {"rank": 2, "emp_code": "EMP2002", "name": "B", "department": "Engineering", "total_late_minutes": 10, "late_count": 1},
        {"rank": 2, "emp_code": "EMP2003", "name": "C", "department": "Engineering", "total_late_minutes": 10, "late_count": 1},
    ]
    response = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2, "department": "Engineering"})
    assert response.status_code == 200 and len(response.json()["items"]) == 3
    pipeline = main.db.attendance_logs.aggregate_pipeline
    assert pipeline[0]["$match"] == {"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "late_minutes": {"$gt": 0}}
    assert pipeline[2]["$lookup"]["pipeline"][0]["$match"]["department"] == "Engineering"
    assert pipeline[4]["$setWindowFields"]["sortBy"] == {"total_late_minutes": -1}
    assert pipeline[5] == {"$match": {"rank": {"$lte": 2}}}
    assert pipeline[6]["$sort"] == {"total_late_minutes": -1, "_id": 1}
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_department_trend_gap_fill_date_validation_and_unknown(api):
    assert api.post("/employees", json=employee_body("EMP2001", department="Engineering")).status_code == 201
    main.db.employees.aggregate_result = [{
        "date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 0.5,
        "late_count": 1, "attendance_rate": 0.5, "moving_avg_7d": 0.5,
    }]
    response = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-06"})
    assert response.status_code == 200
    assert response.json() == {"department": "Engineering", "items": main.db.employees.aggregate_result}
    pipeline = main.db.employees.aggregate_pipeline
    assert pipeline[:3] == [
        {"$match": {"department": "Engineering"}}, {"$limit": 1}, {"$group": {"_id": None}},
    ]
    densify = next(stage["$densify"] for stage in pipeline if "$densify" in stage)
    assert densify["range"]["step"] == 1 and densify["range"]["unit"] == "day"
    assert densify["range"]["bounds"][1] - densify["range"]["bounds"][0] == timedelta(days=1)
    window = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
    assert window["output"]["moving_avg_raw"]["window"]["documents"] == [-6, 0]
    assert any("attendance_logs" == stage.get("$lookup", {}).get("from") for stage in pipeline)
    assert api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-07", "to": "2026-07-06"}).status_code == 422
    assert api.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422
    assert api.get("/analytics/departments/Engineering/trend", params={"from": "2026-02-30", "to": "2026-03-01"}).status_code == 422
    assert api.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).status_code == 404


def test_explain_variants_use_actual_query_builders_and_serialize_raw_bson(api):
    oid = ObjectId()
    raw_date = datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc)
    fake_db.explain_result = {"ok": 1.0, "executionStats": {"nReturned": 3}, "probe": {"_id": oid, "at": raw_date}}
    examples = [
        ("attendance_list", {"emp_code": "EMP2001", "date_from": "2026-07-01", "date_to": "2026-07-31",
                             "status": "PRESENT", "page": 2, "page_size": 5}, "attendance_logs"),
        ("employee_monthly", {"emp_code": "EMP2001", "month": "2026-07"}, "employees"),
        ("department_summary", {"month": "2026-07", "department": "Engineering"}, "employees"),
        ("late_leaderboard", {"month": "2026-07", "limit": 4, "department": "Engineering"}, "attendance_logs"),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"}, "employees"),
    ]
    for endpoint, params, collection in examples:
        response = api.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert set(result) == {"endpoint", "collection", "explain"}
        assert result["endpoint"] == endpoint and result["collection"] == collection
        assert result["explain"]["probe"]["_id"]["$oid"] == str(oid)
        assert result["explain"]["probe"]["at"]["$date"]["$numberLong"] == str(int(raw_date.timestamp() * 1000))
        command_name, operation, options = fake_db.command_calls[-1]
        assert command_name == "explain" and options == {"verbosity": "executionStats"}
        assert operation["find"] == "attendance_logs" if endpoint == "attendance_list" else operation["aggregate"] == collection
        if endpoint == "attendance_list":
            assert operation["filter"] == {"emp_code": "EMP2001", "status": "PRESENT",
                                           "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}
            assert operation["sort"] == {"date": -1, "emp_code": 1}
            assert operation["skip"] == 5 and operation["limit"] == 5
        elif endpoint == "employee_monthly":
            assert operation["pipeline"] == main._employee_monthly_pipeline("EMP2001", "2026-07")
        elif endpoint == "department_summary":
            assert operation["pipeline"] == main._department_summary_pipeline("2026-07", "Engineering")
        elif endpoint == "late_leaderboard":
            assert operation["pipeline"] == main._late_leaderboard_pipeline("2026-07", 4, "Engineering")
        else:
            assert operation["pipeline"] == main._department_trend_pipeline(
                "Engineering", datetime(2026, 7, 6).date(), datetime(2026, 7, 12).date()
            )


def test_explain_validation_for_required_and_invalid_parameters(api):
    assert api.get("/admin/explain/not_supported").status_code == 422
    missing = [
        ("employee_monthly", {}),
        ("employee_monthly", {"emp_code": "EMP2001"}),
        ("department_summary", {}),
        ("late_leaderboard", {}),
        ("department_trend", {}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01"}),
    ]
    for endpoint, params in missing:
        response = api.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)

    invalid_requests = [
        ("employee_monthly", {"emp_code": "EMP2001", "month": "2026-13"}),
        ("department_summary", {"month": "2026-2"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 0}),
        ("late_leaderboard", {"month": "2026-07", "limit": 51}),
        ("attendance_list", {"page": 0}),
        ("attendance_list", {"page_size": 101}),
        ("attendance_list", {"status": "INVALID"}),
        ("attendance_list", {"date_from": "2026-02-30"}),
        ("attendance_list", {"date_from": "2026-07-31", "date_to": "2026-07-01"}),
        ("department_trend", {"department": "Engineering", "from": "2026-02-30", "to": "2026-03-01"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-02", "to": "2026-07-01"}),
        ("department_trend", {"department": "Engineering", "from": "2026-06-01", "to": "2026-09-01"}),
    ]
    for endpoint, params in invalid_requests:
        assert api.get(f"/admin/explain/{endpoint}", params=params).status_code == 422
