# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punch events, manual corrections, and MongoDB-backed attendance analytics. It implements the HROne assignment contract in `app/main.py`; MongoDB stores employee and attendance documents, and MongoDB aggregation pipelines calculate the analytics responses.

## Problem and objectives

The API records employee punch-in and punch-out events, supports audited corrections, and answers monthly and department-level attendance questions. The implementation follows the supplied OpenAPI contract and rules R1–R10, including IST shift dates, race-safe writes, stored derived values, exact pagination, and analytics calculated in MongoDB.

## Features

- Employee creation and filtered, paginated employee listing.
- Punch-in and punch-out with duplicate protection, overnight shifts, and calculated attendance values.
- Attendance listing and conditional manual regularization with an append-only change history.
- Employee monthly summary, department summary, late leaderboard, and gap-filled department trend.
- Explain endpoint for the five contract-defined query variants, using MongoDB `executionStats`.
- Idempotent startup index creation and a MongoDB readiness endpoint.

## Technology and prerequisites

- Python 3.11 or newer.
- MongoDB 6.0 or newer, reachable from the machine running the API.
- Dependencies listed in `requirements.txt`; tests also require `requirements-test.txt`.

No Dockerfile, database dump, `.env.example`, or database seeder is part of the committed submission. For local development, run MongoDB separately using your approved local installation or MongoDB service. The API does not call external services.

## Repository layout

```text
app/main.py             FastAPI application, models, MongoDB access, and pipelines
app/__init__.py         Python package marker
tests/test_phase1.py    Automated API/helper tests using an in-memory PyMongo test double
requirements.txt        Runtime dependencies
requirements-test.txt   Test dependencies, including runtime requirements
README.md               Setup, API, and verification notes
REVIEW.md               Starter-code review
DECISIONS.md            Five assignment design decisions
```

The assignment's reference specification, sample documents, and `sample_seed.py` are not included in the submitted commit. The candidate kit's seed script deletes all documents from both target collections before loading samples; do not run it against a database containing data you need.

## Install and configure

Run commands from the repository root. Create a virtual environment and install runtime requirements.

### Windows PowerShell

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

```powershell
python -m pip install -r requirements.txt
```

Configure a local MongoDB connection for the current PowerShell session. These are development examples; replace the URI only with a connection string you are authorized to use. Do not commit credentials or `.env` files.

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
```

```powershell
$env:MONGO_DB = "attendance_dev"
```

### macOS / Linux

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
python -m pip install -r requirements.txt
```

```bash
export MONGO_URI="mongodb://localhost:27017"
```

```bash
export MONGO_DB="attendance_dev"
```

The application reads `MONGO_URI` and `MONGO_DB`. If unset, the defaults are `mongodb://localhost:27017` and `attendance_db`. `python-dotenv` loads a local `.env` file when present; real process environment variables take precedence. No `.env.example` is required.

## Run the API

Start a MongoDB service first, then run from the repository root:

```powershell
uvicorn app.main:app --port 8000
```

The same command works in macOS/Linux shells. At startup, the service creates indexes idempotently. The health route pings MongoDB and returns `503` when the database is unavailable.

- API base URL: <http://127.0.0.1:8000>
- Health: <http://127.0.0.1:8000/health>
- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>

## API reference

All instants in request and response bodies use integer epoch milliseconds. Calendar dates use `YYYY-MM-DD`; months use `YYYY-MM`; shift times use 24-hour `HH:MM`. MongoDB `_id` is internal and is not returned.

| Method and path | Purpose and inputs | Success and notable errors |
|---|---|---|
| `GET /health` | MongoDB readiness check. | `200` when MongoDB responds; `503` otherwise. |
| `POST /employees` | Create an employee. Required: `emp_code`, `name`, `email`, `department`, `joined_on`. Optional shift defaults: `09:30`–`18:30`. | `201`; `409` duplicate code; `422` invalid input. |
| `GET /employees` | Optional exact, case-sensitive `department`; `page` (default 1), `page_size` (default 20, max 100). Sorted by `emp_code` ascending. | `200` page with `items`, filtered `total`, `page`, and `page_size`; `422` invalid pagination. |
| `POST /attendance/punch-in` | Required `emp_code`; optional `punched_at` and `status` (`PRESENT`, `WFH`, `ON_DUTY`; default `PRESENT`). | `201`; `404` unknown employee; `409` duplicate attendance date; `422` invalid body/time. |
| `POST /attendance/punch-out` | Required `emp_code`; optional `punched_at` (defaults to current time). | `200`; `404` unknown employee or no eligible punch-in; `409` already closed; `422` invalid punch interval. |
| `GET /attendance` | Optional `emp_code`, inclusive `date_from` / `date_to`, `status`, `page`, `page_size`. Sorted by date descending, then employee code ascending. | `200` page and filtered total; `422` invalid date range or pagination. |
| `PATCH /attendance/{emp_code}/{date}` | Manual correction. Required `reason` and `regularized_by`; optional `status`, `punch_in`, `punch_out`. Omitted editable fields remain unchanged. | `200`; `404` missing employee/record; `409` concurrent edit; `422` invalid or no-op correction. |
| `GET /analytics/employees/{emp_code}/monthly` | Required `month=YYYY-MM`. | `200` monthly metrics; `404` unknown employee; `422` invalid month. |
| `GET /analytics/departments/summary` | Required `month`; optional exact `department`. | `200` department metrics; `422` invalid month. |
| `GET /analytics/leaderboard/late` | Required `month`; optional `limit` (1–50, default 10) and exact `department`. | `200` ranked employees; `422` invalid month or limit. |
| `GET /analytics/departments/{department}/trend` | Required inclusive `from=YYYY-MM-DD` and `to=YYYY-MM-DD` query parameters; maximum 92 days. | `200` daily series; `404` unknown department; `422` invalid or oversized range. |
| `GET /admin/explain/{endpoint}` | `endpoint` is one of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`. Pass the parameters used by that endpoint (trend uses `department`, `from`, `to`). List pagination and filters are accepted for `attendance_list`. | `200` with `endpoint`, base `collection`, and raw MongoDB explain output using `executionStats`; `422` unsupported endpoint, missing required parameters, or invalid values. |

Response shapes follow the OpenAPI contract. Employee objects contain `emp_code`, `name`, `email`, `department`, `shift_start`, `shift_end`, `joined_on`, and `created_at`. Employee and attendance list responses wrap `items` with `total`, `page`, and `page_size`. Attendance records contain `emp_code`, `date`, `status`, both punch values, `work_hours`, `late_minutes`, `overtime_minutes`, `half_day`, and `history`. The monthly response contains `emp_code`, `month`, `working_days`, `present_days`, `leave_days`, `late_count`, `total_late_minutes`, `total_overtime_minutes`, and `attendance_pct`. Department summary rows contain `department`, `headcount`, `present_days`, `avg_work_hours`, `late_count`, `total_late_minutes`, `leave_count`, and `on_duty_count`. Leaderboard rows contain `rank`, employee identity/name/department, `total_late_minutes`, and `late_count`. Trend rows contain `date`, `is_working_day`, `headcount`, `present_count`, `late_count`, `attendance_rate`, and `moving_avg_7d`.

Example employee request:

```json
{
  "emp_code": "EMP0001",
  "name": "Asha Rao",
  "email": "asha@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05"
}
```

Example punch-in request:

```json
{
  "emp_code": "EMP0001",
  "punched_at": 1783312500000,
  "status": "PRESENT"
}
```

The punch-in response is an attendance record with `emp_code`, `date`, `status`, `punch_in`, `punch_out`, `work_hours`, `late_minutes`, `overtime_minutes`, `half_day`, and `history`. Timestamp fields are milliseconds; unset `punch_out` and `work_hours` are `null`.

## Attendance and analytics rules

- Shifts and attendance dates use IST (UTC+05:30). An overnight punch-in earlier than the shift end belongs to the previous day's shift. Instants are truncated to whole seconds before storing or calculating.
- A punch-in is late only when it is strictly more than 10 minutes after shift start; late minutes are floored from shift start. Overtime is floored after shift end and counted only at 30 minutes or more.
- Work hours are elapsed hours rounded to two decimals using decimal half-up rounding. After punch-out, `half_day` is true when the rounded value is below `4.50` hours.
- `PRESENT`, `WFH`, and `ON_DUTY` count as presence. `ABSENT` and `LEAVE` do not. Monthly working days are weekdays from the later of month start and `joined_on`; weekend records still contribute to late and overtime totals.
- Analytics read stored derived fields. Monthly and department presence is weekday-only; department average work hours is record-weighted over present records with a work-hours value. Headcount includes employees joined by period end, including employees without logs.
- Leaderboard ties use competition ranking (for example `1, 2, 2, 4`), with the limit applied after ranking. Trend returns every date in the inclusive range, fills gaps, and uses the current date plus up to six previous dates for its moving average. Attendance rate is null on non-working days or when headcount is zero.
- Successful manual corrections append one history entry containing only actual changed fields. Punch-in and punch-out do not write correction history. Analytics percentage rates use four-decimal half-up rounding; other reported calculated values use two decimals where applicable.

## MongoDB collections and indexes

The app uses `employees` and `attendance_logs`. Employee identity is `emp_code`. An attendance row is keyed by (`emp_code`, `date`); `date` and `joined_on` are calendar-date strings. Instants such as `created_at`, punch times, and audit times are BSON UTC datetimes. Missing legacy `history` and `half_day` values are treated as `[]` and `false`.

Indexes created by `ensure_indexes()` at startup:

| Collection | Index | Purpose |
|---|---|---|
| `employees` | unique `emp_code` (`uq_employees_emp_code`) | Employee identity and duplicate protection. |
| `employees` | `department, emp_code` (`ix_employees_department_emp_code`) | Department employee filtering and trend seed query. |
| `employees` | `department, joined_on` (`ix_employees_department_joined_on`) | Department headcount eligibility. |
| `employees` | `joined_on` (`ix_employees_joined_on`) | Unfiltered department summary eligibility scan. |
| `attendance_logs` | unique `emp_code, date` (`uq_attendance_emp_code_date`) | One attendance row per employee and date; punch-in race protection. |
| `attendance_logs` | `date` descending, `emp_code` ascending (`ix_attendance_date_emp_code`) | Date-filtered attendance listing and date-range queries. |

## Tests and database verification

Install test dependencies:

```powershell
python -m pip install -r requirements-test.txt
```

Run the deterministic automated suite and bytecode compilation checks:

```powershell
python -m pytest -q
```

```powershell
python -m compileall -q app tests
```

The automated suite replaces PyMongo with an in-memory test double. It checks API behavior, helper boundaries, concurrency responses in the double, pipeline construction, and explain command construction. It does not contact MongoDB, prove server-side concurrency, or test MongoDB's planner. Real MongoDB integration and explain checks are separate activities; use only a fresh, disposable database and never point them at personal or production data. The committed repository contains no performance-data generator, so the 100,000-record explain audit is not reproduced by the standard test command.

For a MongoDB-backed manual explain check, start the app with a disposable database configured in `MONGO_DB`, then call the five explain variants through `/docs` or the API. Required parameters are: `attendance_list` (any desired attendance filters and pagination), `employee_monthly` (`emp_code`, `month`), `department_summary` (`month`, optional `department`), `late_leaderboard` (`month`, optional `limit` and `department`), and `department_trend` (`department`, `from`, `to`). Inspect the returned raw explain document for execution statistics and plan stages. This procedure inspects the current database; it does not generate a representative 100,000-record dataset.

## Design decisions and limitations

The project-specific trade-offs are recorded in [`DECISIONS.md`](DECISIONS.md); the starter review and verification caveats are in [`REVIEW.md`](REVIEW.md). The API has no authentication layer because none is defined in the assignment contract. The in-memory tests do not substitute for production-scale integration testing. A separate local explain audit during final review used MongoDB 7.0.43, a fresh isolated database, 2,500 employees, and 100,000 attendance rows; the five representative plans included `IXSCAN` and no `COLLSCAN`. That one-off dataset generator was not retained, so the run is historical evidence rather than a reproducible repository command.
