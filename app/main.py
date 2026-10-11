from __future__ import annotations
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional
from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError



load_dotenv()


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=4000)

db = client[MONGO_DB]

employees = db["employees"]

attendance_logs = db["attendance_logs"]


UTC = timezone.utc

IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

MIN_EPOCH_MS = 100_000_000_000

MAX_EPOCH_MS = 4_102_444_800_000

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

logger = logging.getLogger(__name__)


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.get("/", tags=["Health"])

def root() -> dict:

    """Return a simple API welcome message."""

    return {

        "message": "Employee Attendance & Analytics API",

        "status": "running",

        "docs": "/docs",

        "health": "/health",

    }


@app.get("/favicon.ico", include_in_schema=False)

def favicon() -> Response:

    """Return no content when the browser requests a favicon."""

    return Response(status_code=204)




def ensure_indexes() -> None:

    """Create required indexes idempotently."""

    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")

    employees.create_index(

        [("department", ASCENDING), ("emp_code", ASCENDING)],

        name="idx_employee_department_code",

    )

    employees.create_index(

        [("department", ASCENDING), ("joined_on", ASCENDING)],

        name="idx_employee_department_joined",

    )

    employees.create_index([("joined_on", ASCENDING)], name="idx_employee_joined_on")


    attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", ASCENDING)],

        unique=True,

        name="uq_attendance_emp_date",

    )

    attendance_logs.create_index(

        [("date", DESCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)],

        name="idx_attendance_date_emp_status",

    )

    attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", DESCENDING)],

        name="idx_attendance_emp_date",

    )

    attendance_logs.create_index(

        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],

        name="idx_attendance_emp_punch_in",

    )

    attendance_logs.create_index(

        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],

        name="idx_attendance_date_late_emp",

    )

    attendance_logs.create_index(

        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],

        name="idx_attendance_status_date_emp",

    )


@app.on_event("startup")

def on_startup() -> None:


    try:

        ensure_indexes()

    except PyMongoError:

        logger.exception("Could not create MongoDB indexes during startup")




def validate_iso_date(value: str, field_name: str = "date") -> date:

    if not isinstance(value, str) or not DATE_RE.fullmatch(value):

        raise ValueError(f"{field_name} must use YYYY-MM-DD format")

    try:

        parsed = date.fromisoformat(value)

    except ValueError as exc:

        raise ValueError(f"{field_name} must be a valid calendar date") from exc

    if parsed.isoformat() != value:

        raise ValueError(f"{field_name} must use YYYY-MM-DD format")

    return parsed


def validate_month(value: str) -> tuple[str, str, str]:

    """Return (inclusive start, exclusive next month start, inclusive month end)."""

    if not MONTH_RE.fullmatch(value):

        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")

    try:

        start = date.fromisoformat(f"{value}-01")

        if start.isoformat() != f"{value}-01":

            raise ValueError

    except ValueError as exc:

        raise HTTPException(status_code=422, detail="month must be a valid calendar month") from exc

    if start.month == 12:

        next_start = date(start.year + 1, 1, 1)

    else:

        next_start = date(start.year, start.month + 1, 1)

    last_day = next_start - timedelta(days=1)

    return start.isoformat(), next_start.isoformat(), last_day.isoformat()


def current_utc_second() -> datetime:

    return datetime.now(UTC).replace(microsecond=0)


def normalize_utc(value: datetime) -> datetime:

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    else:

        value = value.astimezone(UTC)

    return value.replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:

    
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:

    if value is None:

        return None

    value = normalize_utc(value)

    return int(value.timestamp()) * 1000


def valid_epoch_ms(value: Optional[int]) -> Optional[int]:

    if value is None:

        return None

    if isinstance(value, bool) or not isinstance(value, int):

        raise ValueError("timestamp must be an integer epoch millisecond value")

    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:

        raise ValueError("timestamp is outside the supported epoch-millisecond range")

    return (value // 1000) * 1000


def round_half_up(value: Optional[float | int], places: int = 2) -> Optional[float]:

    if value is None:

        return None

    quantum = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _parse_hhmm(value: str) -> time:

    return time.fromisoformat(value)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:

    """Find the shift-start date for a punch-in (R1), with the instant in IST."""

    local = normalize_utc(punch_in).astimezone(IST)

    local_date = local.date()

    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:

        return local_date - timedelta(days=1)

    return local_date


def shift_start_datetime(attendance_day: date, shift_start: str) -> datetime:

    return datetime.combine(attendance_day, _parse_hhmm(shift_start), tzinfo=IST)


def shift_end_datetime(attendance_day: date, shift_start: str, shift_end: str) -> datetime:

    end_day = attendance_day

    if shift_end <= shift_start:

        end_day += timedelta(days=1)

    return datetime.combine(end_day, _parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: Optional[str] = None,

                         shift_end: Optional[str] = None) -> int:

    punch_in = normalize_utc(punch_in)

    if attendance_day is None:

        
        local_day = punch_in.astimezone(IST).date()

    else:

        local_day = date.fromisoformat(attendance_day)

    start = shift_start_datetime(local_day, shift_start).astimezone(UTC)

    delta_seconds = int((punch_in - start).total_seconds())

    return delta_seconds // 60 if delta_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:

    seconds = int((normalize_utc(punch_out) - normalize_utc(punch_in)).total_seconds())

    hours = Decimal(seconds) / Decimal(3600)

    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str,

                     attendance_day: str) -> int:

    end = shift_end_datetime(date.fromisoformat(attendance_day), shift_start, shift_end).astimezone(UTC)

    seconds = int((normalize_utc(punch_out) - end).total_seconds())

    minutes = seconds // 60

    return minutes if seconds >= 30 * 60 else 0


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],

                   employee: dict, attendance_day: str) -> dict:

    if status in ("ABSENT", "LEAVE"):

        return {"punch_in": None, "punch_out": None, "work_hours": None,

                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    if punch_in is None:

        raise HTTPException(status_code=422, detail="a presence status requires punch_in")

    punch_in = normalize_utc(punch_in)

    if punch_out is not None:

        punch_out = normalize_utc(punch_out)

        elapsed = (punch_out - punch_in).total_seconds()

        if elapsed <= 0:

            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")

        if elapsed > 24 * 60 * 60:

            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    late = compute_late_minutes(punch_in, employee["shift_start"], attendance_day, employee["shift_end"])

    if punch_out is None:

        return {"punch_in": punch_in, "punch_out": None, "work_hours": None,

                "late_minutes": late, "overtime_minutes": 0, "half_day": False}

    work_hours = compute_work_hours(punch_in, punch_out)

    overtime = compute_overtime(punch_out, employee["shift_start"], employee["shift_end"], attendance_day)

    return {"punch_in": punch_in, "punch_out": punch_out, "work_hours": work_hours,

            "late_minutes": late, "overtime_minutes": overtime, "half_day": work_hours < 4.50}


def serialize_history(history: Optional[list]) -> list:

    result = []

    for entry in history or []:

        changes_out = {}

        for field_name, pair in (entry.get("changes") or {}).items():

            old_value = pair.get("from")

            new_value = pair.get("to")

            if field_name in ("punch_in", "punch_out"):

                old_value = datetime_to_epoch_ms(old_value) if isinstance(old_value, datetime) else old_value

                new_value = datetime_to_epoch_ms(new_value) if isinstance(new_value, datetime) else new_value

            changes_out[field_name] = {"from": old_value, "to": new_value}

        at_value = entry.get("at")

        result.append({

            "at": datetime_to_epoch_ms(at_value) if isinstance(at_value, datetime) else at_value,

            "by": entry.get("by", ""),

            "reason": entry.get("reason", ""),

            "changes": changes_out,

        })

    return result


def serialize_employee(doc: dict) -> dict:

    return {

        "emp_code": doc["emp_code"],

        "name": doc["name"],

        "email": doc["email"],

        "department": doc["department"],

        "shift_start": doc["shift_start"],

        "shift_end": doc["shift_end"],

        "joined_on": doc["joined_on"],

        "created_at": datetime_to_epoch_ms(doc["created_at"]),

    }


def serialize_attendance(doc: dict) -> dict:

    punch_in = doc.get("punch_in")

    punch_out = doc.get("punch_out")

    return {

        "emp_code": doc["emp_code"],

        "date": doc["date"],

        "status": doc["status"],

        "punch_in": datetime_to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else None,

        "punch_out": datetime_to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else None,

        "work_hours": doc.get("work_hours"),

        "late_minutes": int(doc.get("late_minutes", 0) or 0),

        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),

        "half_day": bool(doc.get("half_day", False)),

        "history": serialize_history(doc.get("history", [])),

    }


def require_employee(emp_code: str) -> dict:

    employee = employees.find_one({"emp_code": emp_code})

    if employee is None:

        raise HTTPException(status_code=404, detail="employee not found")

    return employee


def working_weekday_expression(date_expression: str) -> dict:

    """MongoDB expression: true for Monday-Friday (Mongo $dayOfWeek uses Sun=1)."""

    return {"$in": [{"$dayOfWeek": {"$toDate": date_expression}}, [2, 3, 4, 5, 6]]}


def attendance_list_filter(emp_code: Optional[str], date_from: Optional[date],

                           date_to: Optional[date], status: Optional[str]) -> dict:

    query: dict = {}

    if emp_code:

        query["emp_code"] = emp_code

    if date_from or date_to:

        date_filter = {}

        if date_from:

            date_filter["$gte"] = date_from.isoformat()

        if date_to:

            date_filter["$lte"] = date_to.isoformat()

        query["date"] = date_filter

    if status:

        query["status"] = status

    if date_from and date_to and date_from > date_to:

        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    return query



class EmployeeCreate(BaseModel):

    model_config = ConfigDict(extra="ignore")


    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


    @field_validator("joined_on")

    @classmethod

    def valid_join_date(cls, value: str) -> str:

        validate_iso_date(value, "joined_on")

        return value


    @model_validator(mode="after")

    def validate_employee(self) -> "EmployeeCreate":

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        if not self.name.strip() or not self.department.strip():

            raise ValueError("name and department must not be blank")

        return self


class PunchInRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")


    emp_code: str = Field(min_length=1)

    punched_at: Optional[StrictInt] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


    @field_validator("punched_at")

    @classmethod

    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:

        return valid_epoch_ms(value)


    @model_validator(mode="after")

    def disallow_null_timestamp(self) -> "PunchInRequest":

        if "punched_at" in self.model_fields_set and self.punched_at is None:

            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")

        return self


class PunchOutRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")


    emp_code: str = Field(min_length=1)

    punched_at: Optional[StrictInt] = None


    @field_validator("punched_at")

    @classmethod

    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:

        return valid_epoch_ms(value)


    @model_validator(mode="after")

    def disallow_null_timestamp(self) -> "PunchOutRequest":

        if "punched_at" in self.model_fields_set and self.punched_at is None:

            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")

        return self


class RegularizeRequest(BaseModel):

    model_config = ConfigDict(extra="ignore")


    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None

    punch_in: Optional[StrictInt] = None

    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)


    @field_validator("punch_in", "punch_out")

    @classmethod

    def validate_timestamp(cls, value: Optional[int]) -> Optional[int]:

        return valid_epoch_ms(value)


    @model_validator(mode="after")

    def validate_fields(self) -> "RegularizeRequest":

        if "status" in self.model_fields_set and self.status is None:

            raise ValueError("status must be a valid attendance status when supplied")

        for field_name in ("punch_in", "punch_out"):

            if field_name in self.model_fields_set and getattr(self, field_name) is None:

                raise ValueError(f"{field_name} must be an epoch-millisecond integer when supplied")

        if self.status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & self.model_fields_set):

            raise ValueError("punch times must not be supplied with ABSENT or LEAVE status")

        if not self.regularized_by.strip():

            raise ValueError("regularized_by must not be blank")

        return self




def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:

    month_start, next_month, _month_end = validate_month(month)

    start_dt = datetime.combine(date.fromisoformat(month_start), time.min, tzinfo=UTC)

    end_dt = datetime.combine(date.fromisoformat(next_month), time.min, tzinfo=UTC)

    month_logs_lookup = {

        "$lookup": {

            "from": "attendance_logs",

            "let": {"code": "$emp_code"},

            "pipeline": [{"$match": {"$expr": {"$and": [

                {"$eq": ["$emp_code", "$$code"]},

                {"$gte": ["$date", month_start]},

                {"$lt": ["$date", next_month]},

            ]}}}],

            "as": "logs",

        }

    }

    return [

        {"$match": {"emp_code": emp_code}},

        month_logs_lookup,

        {"$set": {

            "_working_start": {"$max": [{"$toDate": "$joined_on"}, {"$literal": start_dt}]},

            "_working_end": {"$literal": end_dt},

        }},

        {"$set": {

            "working_days": {"$size": {"$filter": {

                "input": {"$map": {

                    "input": {"$range": [0, {"$max": [0, {"$dateDiff": {

                        "startDate": "$_working_start", "endDate": "$_working_end", "unit": "day"

                    }}]}]},

                    "as": "offset",

                    "in": {"$dateAdd": {"startDate": "$_working_start", "unit": "day", "amount": "$$offset"}},

                }},

                "as": "workday",

                "cond": {"$in": [{"$dayOfWeek": "$$workday"}, [2, 3, 4, 5, 6]]},

            }}},

            "present_days": {"$sum": {"$map": {

                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE_STATUSES)]}}},

                "as": "l",

                "in": {"$cond": [working_weekday_expression("$$l.date"),

                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}, 0]},

            }}},

            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},

            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},

            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},

            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}},

        }},

        {"$project": {

            "_id": 0,

            "emp_code": 1,

            "month": {"$literal": month},

            "working_days": 1,

            "present_days": 1,

            "leave_days": 1,

            "late_count": 1,

            "total_late_minutes": 1,

            "total_overtime_minutes": 1,

            "attendance_pct": {"$cond": [

                {"$gt": ["$working_days", 0]},

                {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]},

                None,

            ]},

        }},

    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:

    month_start, next_month, month_end = validate_month(month)

    emp_match: dict = {"joined_on": {"$lte": month_end}}

    if department is not None:

        emp_match["department"] = department

    return [

        {"$match": emp_match},

        {"$lookup": {

            "from": "attendance_logs",

            "let": {"code": "$emp_code"},

            "pipeline": [{"$match": {"$expr": {"$and": [

                {"$eq": ["$emp_code", "$$code"]},

                {"$gte": ["$date", month_start]},

                {"$lt": ["$date", next_month]},

            ]}}}],

            "as": "logs",

        }},

        {"$set": {

            "_presence_logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE_STATUSES)]}}},

            "_late_logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}},

            "_leave_logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}},

            "_duty_logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}},

            "_workhour_logs": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [

                {"$in": ["$$l.status", list(PRESENCE_STATUSES)]},

                {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]},

            ]}}},

        }},

        {"$set": {

            "_person_present_days": {"$sum": {"$map": {"input": "$_presence_logs", "as": "l", "in": {

                "$cond": [working_weekday_expression("$$l.date"), {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}, 0]

            }}}},

            "_person_hours_sum": {"$sum": {"$map": {"input": "$_workhour_logs", "as": "l", "in": "$$l.work_hours"}}},

            "_person_hours_count": {"$size": "$_workhour_logs"},

            "_person_late_count": {"$size": "$_late_logs"},

            "_person_late_total": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},

            "_person_leave_count": {"$size": "$_leave_logs"},

            "_person_duty_count": {"$size": "$_duty_logs"},

        }},

        {"$group": {

            "_id": "$department",

            "headcount": {"$sum": 1},

            "present_days": {"$sum": "$_person_present_days"},

            "hours_sum": {"$sum": "$_person_hours_sum"},

            "hours_count": {"$sum": "$_person_hours_count"},

            "late_count": {"$sum": "$_person_late_count"},

            "total_late_minutes": {"$sum": "$_person_late_total"},

            "leave_count": {"$sum": "$_person_leave_count"},

            "on_duty_count": {"$sum": "$_person_duty_count"},

        }},

        {"$project": {

            "_id": 0,

            "department": "$_id",

            "headcount": 1,

            "present_days": 1,

            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, {"$divide": ["$hours_sum", "$hours_count"]}, None]},

            "late_count": 1,

            "total_late_minutes": 1,

            "leave_count": 1,

            "on_duty_count": 1,

        }},

        {"$sort": {"department": 1}},

    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10,

                                    department: Optional[str] = None) -> list[dict]:

    month_start, next_month, _month_end = validate_month(month)

    pipeline: list[dict] = [

        {"$match": {"date": {"$gte": month_start, "$lt": next_month}, "late_minutes": {"$gt": 0}}},

        {"$group": {

            "_id": "$emp_code",

            "total_late_minutes": {"$sum": "$late_minutes"},

            "late_count": {"$sum": 1},

        }},

        {"$lookup": {

            "from": "employees",

            "localField": "_id",

            "foreignField": "emp_code",

            "as": "employee",

        }},

        {"$unwind": "$employee"},

    ]

    if department is not None:

        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([

        {"$set": {

            "emp_code": "$_id",

            "name": "$employee.name",

            "department": "$employee.department",

        }},

        {"$setWindowFields": {

            "sortBy": {"total_late_minutes": -1},

            "output": {"rank": {"$rank": {}}},

        }},

        {"$match": {"rank": {"$lte": limit}}},

        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},

        {"$project": {

            "_id": 0, "rank": 1, "emp_code": 1, "name": 1,

            "department": 1, "total_late_minutes": 1, "late_count": 1,

        }},

    ])

    return pipeline


def build_department_trend_pipeline(department: str, from_day: date, to_day: date) -> list[dict]:

    day_count = (to_day - from_day).days + 1

    from_dt = datetime.combine(from_day, time.min, tzinfo=UTC)

    return [

        {"$match": {"department": department}},

        {"$group": {

            "_id": None,

            "people": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},

            "employee_codes": {"$addToSet": "$emp_code"},

        }},


        {"$project": {

            "_id": 0,

            "people": 1,

            "employee_codes": 1,

            "days": {"$map": {

                "input": {"$range": [0, day_count]},

                "as": "offset",

                "in": {"$dateToString": {

                    "date": {"$dateAdd": {"startDate": {"$literal": from_dt}, "unit": "day", "amount": "$$offset"}},

                    "format": "%Y-%m-%d",

                    "timezone": "UTC",

                }},

            }},

        }},

        {"$unwind": "$days"},

        {"$lookup": {

            "from": "attendance_logs",

            "let": {"day": "$days", "codes": "$employee_codes"},

            "pipeline": [{"$match": {"$expr": {"$and": [

                {"$eq": ["$date", "$$day"]},

                {"$in": ["$emp_code", "$$codes"]},

            ]}}}],

            "as": "logs",

        }},

        {"$set": {

            "date": "$days",

            "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, [2, 3, 4, 5, 6]]},

            "headcount": {"$size": {"$filter": {

                "input": "$people", "as": "person", "cond": {"$lte": ["$$person.joined_on", "$days"]}

            }}},

            "present_count": {"$sum": {"$map": {

                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE_STATUSES)]}}},

                "as": "l",

                "in": {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]},

            }}},

            "late_count": {"$size": {"$filter": {

                "input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}

            }}},

        }},

        {"$set": {

            "attendance_rate": {"$cond": [

                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},

                {"$divide": ["$present_count", "$headcount"]},

                None,

            ]},

        }},

        {"$setWindowFields": {

            "sortBy": {"date": 1},

            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},

        }},

        {"$project": {

            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,

            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,

        }},

        {"$sort": {"date": 1}},

    ]


def explain_find(query: dict, page: int, page_size: int) -> dict:

    return db.command("explain", {

        "find": "attendance_logs",

        "filter": query,

        "projection": {"_id": 0},

        "sort": {"date": -1, "emp_code": 1},

        "skip": (page - 1) * page_size,

        "limit": page_size,

    }, verbosity="executionStats")


def explain_aggregate(collection_name: str, pipeline: list[dict]) -> dict:

    return db.command("explain", {

        "aggregate": collection_name,

        "pipeline": pipeline,

        "cursor": {},

        "allowDiskUse": True,

    }, verbosity="executionStats")


def round_monthly_result(result: dict) -> dict:

    result["present_days"] = round_half_up(result.get("present_days", 0), 2)

    if result.get("attendance_pct") is not None:

        result["attendance_pct"] = round_half_up(result["attendance_pct"], 2)

    return result


# ---------------------------------------------------------------------------

# System and employee endpoints

# ---------------------------------------------------------------------------

@app.get("/health")

def health() -> dict:

    try:

        client.admin.command("ping")

    except PyMongoError as exc:

        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc

    return {"status": "ok"}


@app.post("/employees", status_code=201)

def create_employee(body: EmployeeCreate) -> dict:

    doc = body.model_dump()

    doc["created_at"] = current_utc_second()

    try:

        employees.insert_one(doc)

    except DuplicateKeyError as exc:

        raise HTTPException(status_code=409, detail="emp_code already exists") from exc

    return serialize_employee(doc)


@app.get("/employees")

def list_employees(

    department: Optional[str] = None,

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=20, ge=1, le=100),

) -> dict:

    query = {"department": department} if department is not None else {}

    total = employees.count_documents(query)

    cursor = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)

    return {"items": [serialize_employee(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}




@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInRequest) -> dict:

    employee = require_employee(body.emp_code)

    instant = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else current_utc_second()

    attendance_day = attendance_date_for(instant, employee["shift_start"], employee["shift_end"]).isoformat()

    late_minutes = compute_late_minutes(instant, employee["shift_start"], attendance_day, employee["shift_end"])

    doc = {

        "emp_code": body.emp_code,

        "date": attendance_day,

        "status": body.status,

        "punch_in": instant,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": late_minutes,

        "overtime_minutes": 0,

        "half_day": False,

        "history": [],

    }

    try:

        attendance_logs.insert_one(doc)

    except DuplicateKeyError as exc:

        raise HTTPException(status_code=409, detail="already punched in for this date") from exc

    return serialize_attendance(doc)


@app.post("/attendance/punch-out")

def punch_out(body: PunchOutRequest) -> dict:

    require_employee(body.emp_code)

    instant = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else current_utc_second()

    doc = attendance_logs.find_one(

        {"emp_code": body.emp_code, "punch_in": {"$lte": instant}},

        sort=[("punch_in", DESCENDING)],

    )

    if doc is None:
        future_punch_in = attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$type": "date", "$gt": instant},
            },
            {"_id": 1},
        )
        if future_punch_in is not None:
            raise HTTPException(
                status_code=422,
                detail="punched_at must be after punch_in",
            )
        raise HTTPException(
            status_code=404,
            detail="no punch-in found at or before punched_at",
        )

    if doc.get("punch_out") is not None:

        raise HTTPException(status_code=409, detail="this attendance record is already punched out")

    punch_in_time = normalize_utc(doc["punch_in"])

    elapsed = (instant - punch_in_time).total_seconds()

    if elapsed <= 0:

        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    if elapsed > 24 * 60 * 60:

        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    employee = require_employee(body.emp_code)

    final = derived_fields(doc["status"], punch_in_time, instant, employee, doc["date"])

    updated = attendance_logs.update_one(

        {"_id": doc["_id"], "punch_out": None},

        {"$set": {

            "punch_out": final["punch_out"],

            "work_hours": final["work_hours"],

            "late_minutes": final["late_minutes"],

            "overtime_minutes": final["overtime_minutes"],

            "half_day": final["half_day"],

        }},

    )

    if updated.modified_count != 1:

        raise HTTPException(status_code=409, detail="this attendance record was punched out concurrently")

    doc.update(final)

    return serialize_attendance(doc)


@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[date] = None,

    date_to: Optional[date] = None,

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=20, ge=1, le=100),

) -> dict:

    query = attendance_list_filter(emp_code, date_from, date_to, status)

    total = attendance_logs.count_documents(query)

    cursor = attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)

    return {"items": [serialize_attendance(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(

    emp_code: str = Path(min_length=1),

    date: date = Path(...),

    body: RegularizeRequest = ...,

) -> dict:

    day_str = date.isoformat()

    employee = require_employee(emp_code)

    doc = attendance_logs.find_one({"emp_code": emp_code, "date": day_str})

    if doc is None:

        raise HTTPException(status_code=404, detail="attendance record not found")


    old_punch_in = normalize_utc(doc["punch_in"]) if isinstance(doc.get("punch_in"), datetime) else None

    old_punch_out = normalize_utc(doc["punch_out"]) if isinstance(doc.get("punch_out"), datetime) else None

    old_state = {

        "status": doc.get("status"),

        "punch_in": old_punch_in,

        "punch_out": old_punch_out,

        "work_hours": doc.get("work_hours"),

        "late_minutes": int(doc.get("late_minutes", 0) or 0),

        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),

        "half_day": bool(doc.get("half_day", False)),

    }


    status = body.status if "status" in body.model_fields_set else old_state["status"]

    punch_in_time = epoch_ms_to_datetime(body.punch_in) if "punch_in" in body.model_fields_set else old_punch_in

    punch_out_time = epoch_ms_to_datetime(body.punch_out) if "punch_out" in body.model_fields_set else old_punch_out

    if status in ("ABSENT", "LEAVE"):

        if {"punch_in", "punch_out"} & body.model_fields_set:

            raise HTTPException(status_code=422, detail="punch times must not be supplied with ABSENT or LEAVE status")

        punch_in_time = None

        punch_out_time = None

    elif punch_in_time is None:

        raise HTTPException(status_code=422, detail="a presence status requires punch_in")


    if punch_in_time is not None:

        computed_day = attendance_date_for(punch_in_time, employee["shift_start"], employee["shift_end"]).isoformat()

        if computed_day != day_str:

            raise HTTPException(status_code=422, detail="punch_in must belong to the record's attendance date")

    final = derived_fields(status, punch_in_time, punch_out_time, employee, day_str)

    new_state = {

        "status": status,

        "punch_in": final["punch_in"],

        "punch_out": final["punch_out"],

        "work_hours": final["work_hours"],

        "late_minutes": final["late_minutes"],

        "overtime_minutes": final["overtime_minutes"],

        "half_day": final["half_day"],

    }

    changes = {}

    for field_name in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):

        before = old_state[field_name]

        after = new_state[field_name]

        if before != after:

            changes[field_name] = {"from": before, "to": after}

    if not changes:

        raise HTTPException(status_code=422, detail="regularization does not change any fields")


    now = current_utc_second()

    history_entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes}

    optimistic_filter = {"_id": doc["_id"], "status": doc.get("status"), "punch_in": doc.get("punch_in"), "punch_out": doc.get("punch_out")}

    if "history" in doc:

        optimistic_filter["history"] = doc["history"]

    else:

        optimistic_filter["history"] = {"$exists": False}

    update = {"$set": {

        "status": status,

        "punch_in": final["punch_in"],

        "punch_out": final["punch_out"],

        "work_hours": final["work_hours"],

        "late_minutes": final["late_minutes"],

        "overtime_minutes": final["overtime_minutes"],

        "half_day": final["half_day"],

    }, "$push": {"history": history_entry}}

    result = attendance_logs.update_one(optimistic_filter, update)

    if result.modified_count != 1:

        raise HTTPException(status_code=409, detail="attendance record changed concurrently; retry regularization")

    doc.update(new_state)

    doc["history"] = list(doc.get("history", [])) + [history_entry]

    return serialize_attendance(doc)




@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(

    emp_code: str = Path(min_length=1),

    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

) -> dict:

    require_employee(emp_code)

    pipeline = build_employee_monthly_pipeline(emp_code, month)

    result = next(iter(employees.aggregate(pipeline, allowDiskUse=True)), None)

    if result is None:

        raise HTTPException(status_code=404, detail="employee not found")

    return round_monthly_result(result)


@app.get("/analytics/departments/summary")

def department_summary(

    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

    department: Optional[str] = None,

) -> dict:

    pipeline = build_department_summary_pipeline(month, department)

    results = list(employees.aggregate(pipeline, allowDiskUse=True))

    for item in results:

        item["present_days"] = round_half_up(item.get("present_days", 0), 2)

        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"), 2)

    return {"month": month, "items": results}


@app.get("/analytics/leaderboard/late")

def late_leaderboard(

    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

    limit: int = Query(default=10, ge=1, le=50),

    department: Optional[str] = None,

) -> dict:

    pipeline = build_late_leaderboard_pipeline(month, limit, department)

    results = list(attendance_logs.aggregate(pipeline, allowDiskUse=True))

    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")

def department_trend(

    department: str = Path(min_length=1),

    from_day: date = Query(alias="from"),

    to_day: date = Query(alias="to"),

) -> dict:

    if to_day < from_day:

        raise HTTPException(status_code=422, detail="to must be on or after from")

    if (to_day - from_day).days + 1 > 92:

        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_day, to_day)

    results = list(employees.aggregate(pipeline, allowDiskUse=True))

    if not results:

        raise HTTPException(status_code=404, detail="department not found")

    for item in results:

        item["present_count"] = round_half_up(item.get("present_count", 0), 2)

        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)

        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)

    return {"department": department, "items": results}



@app.get("/admin/explain/{endpoint}")

def explain_endpoint(

    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],

    emp_code: Optional[str] = None,

    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),

    department: Optional[str] = None,

    limit: int = Query(default=10, ge=1, le=50),

    date_from: Optional[date] = None,

    date_to: Optional[date] = None,

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=20, ge=1, le=100),

    from_day: Optional[date] = Query(default=None, alias="from"),

    to_day: Optional[date] = Query(default=None, alias="to"),

) -> dict:

    if endpoint == "attendance_list":

        query = attendance_list_filter(emp_code, date_from, date_to, status)

        explanation = explain_find(query, page, page_size)

        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":

        if not emp_code or not month:

            raise HTTPException(status_code=422, detail="emp_code and month are required")

        pipeline = build_employee_monthly_pipeline(emp_code, month)

        explanation = explain_aggregate("employees", pipeline)

        collection_name = "employees"

    elif endpoint == "department_summary":

        if not month:

            raise HTTPException(status_code=422, detail="month is required")

        pipeline = build_department_summary_pipeline(month, department)

        explanation = explain_aggregate("employees", pipeline)

        collection_name = "employees"

    elif endpoint == "late_leaderboard":

        if not month:

            raise HTTPException(status_code=422, detail="month is required")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)

        explanation = explain_aggregate("attendance_logs", pipeline)

        collection_name = "attendance_logs"

    else:

        if not department or from_day is None or to_day is None:

            raise HTTPException(status_code=422, detail="department, from and to are required")

        if to_day < from_day or (to_day - from_day).days + 1 > 92:

            raise HTTPException(status_code=422, detail="invalid trend date range")

        pipeline = build_department_trend_pipeline(department, from_day, to_day)

        explanation = explain_aggregate("employees", pipeline)

        collection_name = "employees"

    # Explain output can include BSON-only values (for example, bson.timestamp.Timestamp).

    # Convert it to Extended JSON structures so FastAPI/Pydantic can serialize the response.

    explanation_json_safe = json.loads(json_util.dumps(explanation))

    return {"endpoint": endpoint, "collection": collection_name, "explain": explanation_json_safe}
