# Employee Attendance & Analytics API

FastAPI service backed by MongoDB. Requires Python 3.11+ and a running MongoDB 6.0+ instance. No `.env.example` file is needed; configure the environment variables directly. The app reads `MONGO_URI` and `MONGO_DB`; if unset, it defaults to `mongodb://localhost:27017` and database `attendance_db`.

## Setup and run

Run commands from the repository root. Start a local MongoDB service separately before starting the API.

### PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_dev"
uvicorn app.main:app --port 8000
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_dev"
uvicorn app.main:app --port 8000
```

The application creates its indexes at startup. Health check: <http://127.0.0.1:8000/health>. Interactive OpenAPI documentation: <http://127.0.0.1:8000/docs> (ReDoc: <http://127.0.0.1:8000/redoc>).

## Tests and database verification

Install test dependencies with `python -m pip install -r requirements-test.txt`. Run the automated suite with:

```bash
python -m pytest -q
python -m compileall -q app tests
```

The automated API tests use an in-memory PyMongo test double; they do not connect to MongoDB or generate the 100,000-record performance dataset. Real MongoDB integration and explain/performance checks are separate manual verification activities and require a dedicated disposable local database. The standard test command does not run them.

Do not point local verification at a production or personal database. This submission does not include a database seeding command; manual integration and performance checks require an isolated, disposable database.
