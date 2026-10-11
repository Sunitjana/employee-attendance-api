# Employee Attendance & Analytics API

## 1. Project Overview

The **Employee Attendance & Analytics API** is a backend REST API built using **Python, FastAPI, and MongoDB**. It provides employee management, attendance tracking, attendance correction, and analytics for employees and departments.

The system allows employees' attendance to be recorded through punch-in and punch-out operations. It automatically calculates working hours, late arrivals, overtime, and half-day attendance. Managers can correct attendance records through a regularization endpoint, which maintains an audit history of manual changes.

The API also provides monthly employee summaries, departmental attendance reports, a late-arrival leaderboard, and daily attendance trends. MongoDB aggregation pipelines are used for analytics calculations.

### Key Features

- Employee creation and listing with department filtering and pagination.
- Attendance punch-in and punch-out operations.
- Automatic calculation of work hours, late minutes, overtime, and half-day status.
- Attendance listing with employee, date-range, and status filters.
- Attendance regularization with an audit history.
- Employee monthly attendance analytics.
- Department-level attendance summaries.
- Late-arrival leaderboard with ranking and tie handling.
- Department attendance trends with a seven-day moving average.
- MongoDB index creation during application startup.
- Query-plan inspection through MongoDB's `explain` functionality.
- Request validation and structured HTTP error responses.
- Health-check endpoint for MongoDB connectivity.

## 2. Technology Stack

| Technology | Purpose |
|---|---|
| Python 3.11+ | Main programming language |
| FastAPI | REST API framework |
| Pydantic | Request validation and data modelling |
| PyMongo | MongoDB database access |
| MongoDB 6.0+ | Employee and attendance data storage |
| Uvicorn | ASGI server |
| python-dotenv | Loading local environment configuration |

## 3. Project Structure

```text
employee-attendance-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── tests/
│   └── test_api.py
├── test.py
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
└── .gitignore
```

The core application logic is contained in `app/main.py`, as required by the assignment.

- `app/main.py`: API routes, validation, attendance calculations, database operations, aggregation pipelines, and explain endpoint.
- `requirements.txt`: Python dependencies required to run the application.
- `test.py`: Local integration test script for checking API behaviour.
- `tests/test_api.py`: Additional test file, if included in the final repository.
- `REVIEW.md`: Review of defects identified and fixed in the starter implementation.
- `DECISIONS.md`: Engineering decisions and design explanations.
- `.gitignore`: Excludes local configuration, credentials, virtual environments, and generated files.

## 4. Prerequisites

Before running the application, ensure that the following are installed:

- Python 3.11 or later.
- MongoDB 6.0 or later, either through MongoDB Atlas or a local MongoDB instance.
- pip, the Python package installer.
- Git, if you plan to submit the project through a public repository.

You must have a valid MongoDB connection string and appropriate database permissions.

## 5. Installation and Setup


### Step 1: Create a virtual environment

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 2: Install dependencies

```bash
pip install -r requirements.txt
```

## 6. Environment Configuration

The application reads its MongoDB configuration from environment variables:

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB` | Database name; defaults to `attendance_db` |

For local development, create a `.env` file in the repository root:

```dotenv
MONGO_URI= "URI"
MONGO_DB=attendance_db
```

The values above are an example for a local MongoDB instance. For MongoDB Atlas, replace `MONGO_URI` with your own valid Atlas connection string.

The application loads `.env` using `python-dotenv`. Environment variables already provided by the running process take precedence over values in `.env`.

**Security requirements:**

- Never commit `.env` to the public repository.
- Do not hard-code database credentials in source code.
- Do not publish passwords or connection strings.
- Use a development database when running integration tests.

## 7. Running the Application

Start the API from the repository root:

```bash
uvicorn app.main:app --port 8000
```

For local development with automatic reloading:

```bash
uvicorn app.main:app --reload --port 8000
```

Once the server starts, the application is accessible at:

- API base URL: `http://127.0.0.1:8000`
- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`
- Health check: `http://127.0.0.1:8000/health`

MongoDB indexes are created automatically during application startup. No manual index-creation command is required.

The health endpoint returns HTTP 200 when MongoDB is reachable and HTTP 503 when the database is unavailable.

## 8. API Endpoints

### 8.1 System and Employee Management

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Returns the API welcome response |
| GET | `/health` | Checks MongoDB connectivity |
| POST | `/employees` | Creates an employee |
| GET | `/employees` | Lists employees with optional department filtering and pagination |

Employee creation validates fields such as employee code, email, department, joining date, and shift times. Duplicate employee codes return HTTP 409.

Employee listing supports `department`, `page`, and `page_size` query parameters.

### 8.2 Attendance Management

| Method | Endpoint | Description |
|---|---|---|
| POST | `/attendance/punch-in` | Records an employee's punch-in |
| POST | `/attendance/punch-out` | Records punch-out and calculates final attendance metrics |
| GET | `/attendance` | Lists and filters attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Corrects an existing attendance record |

The attendance-list endpoint supports filtering by employee code, date range, and attendance status. It also supports pagination.

The regularization endpoint validates the proposed record, recalculates derived fields, and appends a history entry for changes that are actually made.

### 8.3 Analytics

| Method | Endpoint | Description |
|---|---|---|
| GET | `/analytics/employees/{emp_code}/monthly` | Returns an employee's monthly attendance summary |
| GET | `/analytics/departments/summary` | Returns monthly departmental statistics |
| GET | `/analytics/leaderboard/late` | Ranks employees by total late minutes |
| GET | `/analytics/departments/{department}/trend` | Returns daily attendance statistics for a department |

The analytics endpoints use MongoDB aggregation pipelines to perform the calculations in the database.

### 8.4 Query-Plan Explanation

| Method | Endpoint | Description |
|---|---|---|
| GET | `/admin/explain/{endpoint}` | Returns MongoDB execution-plan information for a supported query or aggregation |

Supported values for `{endpoint}` are:

- `attendance_list`
- `employee_monthly`
- `department_summary`
- `late_leaderboard`
- `department_trend`

The endpoint uses MongoDB's `explain` command with execution statistics. It is intended to help inspect query plans and index usage.

## 9. Data Model

The application uses two MongoDB collections.

### Employees

The `employees` collection stores employee details, including:

- `emp_code`
- `name`
- `email`
- `department`
- `shift_start`
- `shift_end`
- `joined_on`
- `created_at`

The employee code is the business identifier used by the API.

### Attendance Logs

The `attendance_logs` collection stores attendance records, including:

- `emp_code`
- `date`
- `status`
- `punch_in`
- `punch_out`
- `work_hours`
- `late_minutes`
- `overtime_minutes`
- `half_day`
- `history`

Each attendance record is identified by the combination of employee code and attendance date.

The application creates a unique compound index on `(emp_code, date)` to enforce one attendance record per employee per attendance date.

## 10. Attendance Rules and Calculations

The application follows the attendance rules defined in the assignment.

### Time Zones and Timestamps

- API timestamps use integer epoch milliseconds.
- MongoDB stores instants as UTC BSON datetimes.
- Attendance dates and shift times use Indian Standard Time (IST).
- Timestamps are truncated to whole seconds before calculations and storage.

### Late Arrival

A 10-minute grace period applies. An employee is considered late only when the punch-in is strictly more than 10 minutes after the shift start.

When the employee is late, `late_minutes` records the whole minutes since the scheduled shift start.

### Work Hours

Work hours are calculated from the difference between punch-out and punch-in, in seconds divided by 3,600. The result is rounded to two decimal places using half-up rounding.

Work hours remain `null` until punch-out is recorded.

### Overtime

Overtime is calculated from the scheduled shift end to the punch-out time. It is recorded only when the elapsed time beyond shift end is at least 30 minutes.

### Half-Day Attendance

After punch-out, `half_day` is set to `true` when the rounded work hours are below 4.50. A half-day contributes 0.5 present days to applicable attendance summaries.

### Attendance Statuses

The API treats `PRESENT`, `WFH`, and `ON_DUTY` as presence statuses. `ABSENT` and `LEAVE` do not count as presence.

Working days are Monday through Friday, with no holiday calendar. The calculation accounts for the employee's joining date.

## 11. Pagination and Validation

List endpoints use one-based pagination:

- Default page: `1`
- Default page size: `20`
- Maximum page size: `100`

The `total` value reflects the active filters.

Request models and query parameters validate the inputs before database operations. Invalid request data typically returns HTTP 422, missing resources return HTTP 404, and conflicts return HTTP 409.

## 12. Testing

Run the API in one terminal:

```powershell
uvicorn app.main:app --reload --port 8000
```

Open a second terminal, activate the virtual environment, and execute the integration test script:

```powershell
python test.py
```

The integration script checks API endpoints, request validation, attendance calculations, filtering, pagination, regularization, analytics, and explain endpoint behaviour.

The local integration script inserts test employees and attendance records into the configured database. Use a disposable development database rather than production data.

The test suite previously reported 59 passing extended checks in the development environment. Results may differ when using another database or dataset, and this does not by itself establish performance against the assignment's hidden dataset.

## 13. Performance and Indexing

The application creates indexes at startup to support employee lookup, department filtering, attendance retrieval, date-based reporting, and punch-in queries.

The analytics operations are implemented as MongoDB aggregation pipelines rather than fetching all records into Python for manual aggregation.

The explain endpoint provides execution statistics to help assess the database query plans. Actual performance and index usage should be verified against representative data volumes, including the approximately 100,000 attendance records specified in the assignment.

## 14. Conclusion

The Employee Attendance & Analytics API provides employee record management, shift-aware attendance tracking, attendance corrections with an audit trail, and MongoDB-based analytics through REST endpoints.

The implementation combines FastAPI request validation, Python attendance calculations, MongoDB persistence, aggregation pipelines, and query-plan inspection to meet the functional requirements of the engineering assignment.
