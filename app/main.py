"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import Binary, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes Setup (Idempotent at startup)
# --------------------------------------------------------------------------- #
def init_indexes(database):
    """Create indexes required for performance and integrity idempotently."""
    # employees collection
    database.employees.create_index([("emp_code", 1)], unique=True)
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    database.employees.create_index([("joined_on", 1)])
    database.employees.create_index([("department", 1), ("emp_code", 1)])

    # attendance_logs collection
    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    database.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    database.attendance_logs.create_index([("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes(db)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Business Logic Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """
    R1: Truncate instant to whole seconds before any calculation,
    and return as UTC datetime.
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_server_now_dt() -> datetime:
    """Current UTC datetime truncated to whole seconds."""
    sec = int(datetime.now(timezone.utc).timestamp())
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> date:
    """
    R1: Attendance date in IST. Overnight shifts (shift_end <= shift_start):
    a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    st_time = time(sh_h, sh_m, 0)
    end_time = time(eh_h, eh_m, 0)
    is_overnight = end_time <= st_time

    ist_time = punch_in_ist.time()
    ist_date = punch_in_ist.date()

    if is_overnight and ist_time < end_time:
        return ist_date - timedelta(days=1)
    return ist_date


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: date) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly > 10:00
    after shift_start. When late, late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_start_dt_ist = datetime.combine(att_date, time(sh_h, sh_m, 0), tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date: date) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out,
    counted only if >= 30; otherwise 0. Works across midnight for overnight shifts.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    st_time = time(sh_h, sh_m, 0)
    end_time = time(eh_h, eh_m, 0)
    is_overnight = end_time <= st_time

    if is_overnight:
        shift_end_dt_ist = datetime.combine(att_date + timedelta(days=1), end_time, tzinfo=IST)
    else:
        shift_end_dt_ist = datetime.combine(att_date, end_time, tzinfo=IST)

    diff_sec = (punch_out_ist - shift_end_dt_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    val = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(val)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def clean_doc(obj: Any) -> Any:
    """Recursively serialize BSON objects to standard JSON structures."""
    if isinstance(obj, dict):
        return {k: clean_doc(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_doc(v) for v in obj]
    elif isinstance(obj, datetime):
        return dt_to_epoch_ms(obj)
    elif isinstance(obj, (ObjectId, Binary)):
        return str(obj)
    elif isinstance(obj, Decimal):
        return float(obj)
    return obj


def format_employee(doc: dict) -> dict:
    """Format employee document for API response according to Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def format_attendance_record(doc: dict) -> dict:
    """Format attendance log document according to AttendanceRecord schema."""
    hist = []
    for h in doc.get("history", []):
        h_changes = {}
        for k, v in h.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                h_changes[k] = {
                    "from": dt_to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                    "to": dt_to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
                }
            else:
                h_changes[k] = {"from": v.get("from"), "to": v.get("to")}
        hist.append({
            "at": dt_to_epoch_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at"),
            "by": h["by"],
            "reason": h["reason"],
            "changes": h_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": hist,
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v, info):
        shift_start = info.data.get("shift_start", "09:30")
        if v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Pipelines Builders for Analytics & Explain
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str, last_day: int) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(month: str, month_end_str: str, match_filter: dict) -> list:
    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flat_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": ["$$l.dayOfWeek", [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": {"$cond": ["$$l.half_day", 0.5, 1.0]},
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flat_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$flat_logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list:
    from_dt = datetime.fromisoformat(from_str)
    to_dt = datetime.fromisoformat(to_str)
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$dayOfWeek": "$date_dt"},
            }
        },
        {"$sort": {"date_str": 1}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """Create an employee with unique emp_code."""
    doc = body.model_dump()
    doc["created_at"] = get_server_now_dt()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    items = [format_employee(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """Record a punch-in. Creates the day's record (R1, R2)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = get_server_now_dt()

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutIn):
    """Record a punch-out. Closes employee's most recent open record (R3-R5)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_out_dt = get_server_now_dt()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found at or before punched_at")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    punch_in_dt = record["punch_in"]
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    att_date = date.fromisoformat(record["date"])
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], att_date)
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))
    items = [format_attendance_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeIn = ...,
):
    """Regularize (manually correct) one day's record, recompute derived fields, append history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        computed_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date.isoformat() != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")

        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            att_d = datetime.fromisoformat(date).date()
            final_overtime_minutes = compute_overtime(final_punch_out, emp["shift_start"], emp["shift_end"], att_d)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        att_d = datetime.fromisoformat(date).date()
        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], att_d)

    # Detect actual changes
    old_status = rec["status"]
    old_punch_in_ms = dt_to_epoch_ms(rec.get("punch_in"))
    new_punch_in_ms = dt_to_epoch_ms(final_punch_in)
    old_punch_out_ms = dt_to_epoch_ms(rec.get("punch_out"))
    new_punch_out_ms = dt_to_epoch_ms(final_punch_out)
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes_db: Dict[str, Any] = {}
    if old_status != new_status:
        changes_db["status"] = {"from": old_status, "to": new_status}
    if old_punch_in_ms != new_punch_in_ms:
        changes_db["punch_in"] = {"from": rec.get("punch_in"), "to": final_punch_in}
    if old_punch_out_ms != new_punch_out_ms:
        changes_db["punch_out"] = {"from": rec.get("punch_out"), "to": final_punch_out}
    if old_work_hours != final_work_hours:
        changes_db["work_hours"] = {"from": old_work_hours, "to": final_work_hours}
    if old_late_minutes != final_late_minutes:
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": final_late_minutes}
    if old_overtime_minutes != final_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": final_overtime_minutes}
    if old_half_day != final_half_day:
        changes_db["half_day"] = {"from": old_half_day, "to": final_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request made no changes to the attendance record")

    now_utc = get_server_now_dt()
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    current_history_size = len(rec.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": current_history_size}},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_record(res)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee via aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]

    # Calculate working days (Mon-Fri, from joined_on if mid-month joiner)
    month_start = date(year, m, 1)
    month_end = date(year, m, last_day)
    joined_date = date.fromisoformat(emp["joined_on"])

    working_days = 0
    if joined_date <= month_end:
        cur = max(month_start, joined_date)
        while cur <= month_end:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month, last_day)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        doc = res[0]
        present_days = float(doc["present_days"])
        leave_days = doc["leave_days"]
        late_count = doc["late_count"]
        total_late = doc["total_late_minutes"]
        total_ot = doc["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    att_pct = None
    if working_days > 0:
        val = (Decimal(str(present_days)) / Decimal(working_days) * Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        att_pct = float(val)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary for a month via cross-collection aggregation."""
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_end_str = f"{month}-{last_day:02d}"

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_filter["department"] = department

    pipeline = build_department_summary_pipeline(month, month_end_str, match_filter)
    res = list(db.employees.aggregate(pipeline))

    items = []
    for r in res:
        avg_wh = r["avg_work_hours"]
        if avg_wh is not None:
            avg_wh = float(Decimal(str(avg_wh)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard with competition rank and ties handling."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """Daily attendance trend for a department with gap-filling and 7-day moving average."""
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date cannot be earlier than 'from' date")

    days_span = (d_to - d_from).days + 1
    if days_span > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    res = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in res:
        rate = r["attendance_rate"]
        if rate is not None:
            rate = float(Decimal(str(rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        ma7 = r["moving_avg_7d"]
        if ma7 is not None:
            ma7 = float(Decimal(str(ma7)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": ma7,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """MongoDB explain for an endpoint's main query with executionStats."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        exp = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": "attendance_list", "collection": "attendance_logs", "explain": clean_doc(exp)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="Missing required parameters: emp_code and month")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        pipeline = build_employee_monthly_pipeline(emp_code, month, last_day)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": "employee_monthly", "collection": "attendance_logs", "explain": clean_doc(exp)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")
        year, m = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m)[1]
        month_end_str = f"{month}-{last_day:02d}"
        match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
        if department:
            match_filter["department"] = department
        pipeline = build_department_summary_pipeline(month, month_end_str, match_filter)
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": "department_summary", "collection": "employees", "explain": clean_doc(exp)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": "late_leaderboard", "collection": "attendance_logs", "explain": clean_doc(exp)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="Missing required parameters: department, from, to")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from or (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": "department_trend", "collection": "attendance_logs", "explain": clean_doc(exp)}


# --------------------------------------------------------------------------- #
# Interactive Frontend Dashboard
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HROne Attendance & Analytics Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    body { font-family: 'Inter', sans-serif; }
    .badge-present { background-color: #dcfce7; color: #15803d; }
    .badge-wfh { background-color: #e0e7ff; color: #4338ca; }
    .badge-onduty { background-color: #fef3c7; color: #b45309; }
    .badge-absent { background-color: #fee2e2; color: #b91c1c; }
    .badge-leave { background-color: #f3e8ff; color: #7e22ce; }
  </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen">
  <!-- Navbar -->
  <header class="border-b border-slate-800 bg-slate-950/80 backdrop-blur sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
          <i class="fa-solid fa-fingerprint text-white text-lg"></i>
        </div>
        <div>
          <span class="text-lg font-bold bg-gradient-to-r from-white to-slate-400 bg-clip-text text-transparent">HROne Attendance</span>
          <span class="text-xs text-indigo-400 block font-mono">FastAPI + MongoDB Engine</span>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> MongoDB Connected
        </span>
        <a href="/docs" target="_blank" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition flex items-center gap-1.5 shadow-md shadow-indigo-600/20">
          <i class="fa-solid fa-bolt"></i> Swagger Docs
        </a>
        <a href="/redoc" target="_blank" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-300 transition">
          ReDoc
        </a>
      </div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
    
    <!-- Top Stats -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4" id="statsRow">
      <div class="p-5 rounded-2xl bg-slate-800/60 border border-slate-700/50 backdrop-blur">
        <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Total Headcount</span>
        <div class="text-2xl font-bold mt-1 text-white" id="statEmployees">...</div>
      </div>
      <div class="p-5 rounded-2xl bg-slate-800/60 border border-slate-700/50 backdrop-blur">
        <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Logs Seeded</span>
        <div class="text-2xl font-bold mt-1 text-indigo-400" id="statLogs">...</div>
      </div>
      <div class="p-5 rounded-2xl bg-slate-800/60 border border-slate-700/50 backdrop-blur">
        <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Active Departments</span>
        <div class="text-2xl font-bold mt-1 text-emerald-400" id="statDepts">3</div>
      </div>
      <div class="p-5 rounded-2xl bg-slate-800/60 border border-slate-700/50 backdrop-blur">
        <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Indexed Search Scan</span>
        <div class="text-2xl font-bold mt-1 text-amber-400">100% IXSCAN</div>
      </div>
    </div>

    <!-- Live Punch Terminal -->
    <div class="p-6 rounded-2xl bg-slate-800/80 border border-slate-700 shadow-xl space-y-4">
      <div class="flex items-center justify-between border-b border-slate-700/60 pb-3">
        <div class="flex items-center gap-2.5">
          <i class="fa-solid fa-clock text-indigo-400"></i>
          <h2 class="text-base font-bold text-white">Interactive Punch & Attendance Terminal</h2>
        </div>
        <span class="text-xs text-slate-400">Real-time Rule Evaluation (R1 - R5)</span>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-4 gap-4 items-end">
        <div>
          <label class="block text-xs font-medium text-slate-400 mb-1">Select Employee</label>
          <select id="punchEmp" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
            <!-- options populated via JS -->
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-400 mb-1">Presence Status</label>
          <select id="punchStatus" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
            <option value="PRESENT">PRESENT (Office)</option>
            <option value="WFH">WFH (Remote)</option>
            <option value="ON_DUTY">ON_DUTY (Field/Client)</option>
          </select>
        </div>
        <div class="flex gap-2">
          <button onclick="handlePunch('in')" class="flex-1 py-2 px-4 rounded-xl bg-emerald-600 hover:bg-emerald-500 font-semibold text-white text-sm transition shadow-lg shadow-emerald-600/20 flex items-center justify-center gap-2">
            <i class="fa-solid fa-arrow-right-to-bracket"></i> Punch In
          </button>
          <button onclick="handlePunch('out')" class="flex-1 py-2 px-4 rounded-xl bg-rose-600 hover:bg-rose-500 font-semibold text-white text-sm transition shadow-lg shadow-rose-600/20 flex items-center justify-center gap-2">
            <i class="fa-solid fa-arrow-right-from-bracket"></i> Punch Out
          </button>
        </div>
        <div>
          <button onclick="refreshAll()" class="w-full py-2 px-4 rounded-xl bg-slate-700 hover:bg-slate-600 font-semibold text-slate-200 text-sm transition flex items-center justify-center gap-2">
            <i class="fa-solid fa-rotate"></i> Refresh View
          </button>
        </div>
      </div>

      <div id="punchAlert" class="hidden p-3.5 rounded-xl text-xs font-mono transition"></div>
    </div>

    <!-- Analytics Section -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      
      <!-- Department Summary -->
      <div class="lg:col-span-2 p-6 rounded-2xl bg-slate-800/60 border border-slate-700/60 space-y-4">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <i class="fa-solid fa-chart-pie text-violet-400"></i>
            <h3 class="font-bold text-white text-sm">Department Monthly Summary (July 2026)</h3>
          </div>
          <span class="text-xs text-slate-400 font-mono">Cross-Collection Pipeline</span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs text-slate-300">
            <thead class="bg-slate-900/60 text-slate-400 uppercase font-semibold">
              <tr>
                <th class="py-2.5 px-3">Department</th>
                <th class="py-2.5 px-3">Headcount</th>
                <th class="py-2.5 px-3">Present Days</th>
                <th class="py-2.5 px-3">Avg Hours</th>
                <th class="py-2.5 px-3">Late Count</th>
                <th class="py-2.5 px-3">Total Late</th>
              </tr>
            </thead>
            <tbody id="deptSummaryBody" class="divide-y divide-slate-800">
              <tr><td colspan="6" class="p-3 text-center text-slate-500">Loading department summary...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Late Leaderboard -->
      <div class="p-6 rounded-2xl bg-slate-800/60 border border-slate-700/60 space-y-4">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <i class="fa-solid fa-trophy text-amber-400"></i>
            <h3 class="font-bold text-white text-sm">Late Leaderboard</h3>
          </div>
          <span class="text-xs text-amber-400 font-mono">Window $rank</span>
        </div>
        <div class="space-y-2.5" id="leaderboardList">
          <div class="text-xs text-slate-500 text-center py-4">Loading leaderboard...</div>
        </div>
      </div>

    </div>

    <!-- Attendance Logs Table -->
    <div class="p-6 rounded-2xl bg-slate-800/60 border border-slate-700/60 space-y-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <i class="fa-solid fa-table-list text-indigo-400"></i>
          <h3 class="font-bold text-white text-sm">Recent Attendance Logs</h3>
        </div>
        <span class="text-xs text-slate-400">Addressable by (emp_code, date)</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs text-slate-300">
          <thead class="bg-slate-900/60 text-slate-400 uppercase font-semibold">
            <tr>
              <th class="py-3 px-3">Date</th>
              <th class="py-3 px-3">Employee</th>
              <th class="py-3 px-3">Status</th>
              <th class="py-3 px-3">Work Hours</th>
              <th class="py-3 px-3">Late (min)</th>
              <th class="py-3 px-3">Overtime (min)</th>
              <th class="py-3 px-3">Audit Entries</th>
              <th class="py-3 px-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody id="logsTableBody" class="divide-y divide-slate-800">
            <tr><td colspan="8" class="p-4 text-center text-slate-500">Loading records...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Regularization Modal -->
    <div id="regModal" class="fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center hidden">
      <div class="bg-slate-900 border border-slate-700 rounded-2xl p-6 w-full max-w-md space-y-4 shadow-2xl">
        <div class="flex justify-between items-center border-b border-slate-800 pb-3">
          <h4 class="font-bold text-white text-sm" id="modalTitle">Regularize Attendance</h4>
          <button onclick="closeModal()" class="text-slate-400 hover:text-white"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <input type="hidden" id="modalEmp">
        <input type="hidden" id="modalDate">
        <div>
          <label class="block text-xs font-medium text-slate-400 mb-1">New Status</label>
          <select id="modalStatus" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
            <option value="PRESENT">PRESENT</option>
            <option value="WFH">WFH</option>
            <option value="ON_DUTY">ON_DUTY</option>
            <option value="ABSENT">ABSENT</option>
            <option value="LEAVE">LEAVE</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-400 mb-1">Reason (min 5 chars)</label>
          <input type="text" id="modalReason" value="Approved retroactive regularization" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-400 mb-1">Authorized By</label>
          <input type="text" id="modalBy" value="hr.admin" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button onclick="closeModal()" class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-slate-300">Cancel</button>
          <button onclick="submitRegularization()" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white">Save Correction</button>
        </div>
      </div>
    </div>

  </main>

  <script>
    async function loadEmployees() {
      const res = await fetch('/employees?page_size=100');
      const data = await res.json();
      const select = document.getElementById('punchEmp');
      select.innerHTML = '';
      data.items.forEach(e => {
        const opt = document.createElement('option');
        opt.value = e.emp_code;
        opt.textContent = `${e.emp_code} - ${e.name} (${e.department})`;
        select.appendChild(opt);
      });
      document.getElementById('statEmployees').textContent = data.total;
    }

    async function loadDepartmentSummary() {
      const res = await fetch('/analytics/departments/summary?month=2026-07');
      const data = await res.json();
      const tbody = document.getElementById('deptSummaryBody');
      tbody.innerHTML = '';
      data.items.forEach(d => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-800/40 transition';
        tr.innerHTML = `
          <td class="py-2.5 px-3 font-semibold text-white">${d.department}</td>
          <td class="py-2.5 px-3 font-mono">${d.headcount}</td>
          <td class="py-2.5 px-3 font-mono">${d.present_days.toFixed(1)}</td>
          <td class="py-2.5 px-3 font-mono">${d.avg_work_hours !== null ? d.avg_work_hours.toFixed(2) + 'h' : 'N/A'}</td>
          <td class="py-2.5 px-3 font-mono text-amber-400">${d.late_count}</td>
          <td class="py-2.5 px-3 font-mono text-amber-400">${d.total_late_minutes}m</td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function loadLeaderboard() {
      const res = await fetch('/analytics/leaderboard/late?month=2026-07&limit=5');
      const data = await res.json();
      const list = document.getElementById('leaderboardList');
      list.innerHTML = '';
      if (!data.items || data.items.length === 0) {
        list.innerHTML = '<div class="text-xs text-slate-500 text-center py-4">No late-comers recorded!</div>';
        return;
      }
      data.items.forEach(item => {
        const medal = item.rank === 1 ? '🥇' : item.rank === 2 ? '🥈' : item.rank === 3 ? '🥉' : `#${item.rank}`;
        const div = document.createElement('div');
        div.className = 'p-3 rounded-xl bg-slate-900/60 border border-slate-800 flex items-center justify-between text-xs';
        div.innerHTML = `
          <div class="flex items-center gap-2.5">
            <span class="w-6 text-center font-bold text-sm">${medal}</span>
            <div>
              <span class="font-semibold text-white block">${item.name}</span>
              <span class="text-slate-500 font-mono">${item.emp_code} • ${item.department}</span>
            </div>
          </div>
          <div class="text-right">
            <span class="font-mono font-bold text-amber-400 block">${item.total_late_minutes} min late</span>
            <span class="text-slate-500">${item.late_count} occurrence(s)</span>
          </div>
        `;
        list.appendChild(div);
      });
    }

    async function loadAttendanceLogs() {
      const res = await fetch('/attendance?page_size=20');
      const data = await res.json();
      document.getElementById('statLogs').textContent = data.total;
      const tbody = document.getElementById('logsTableBody');
      tbody.innerHTML = '';
      data.items.forEach(log => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-800/40 transition';
        const badgeClass = log.status === 'PRESENT' ? 'badge-present' :
                           log.status === 'WFH' ? 'badge-wfh' :
                           log.status === 'ON_DUTY' ? 'badge-onduty' :
                           log.status === 'ABSENT' ? 'badge-absent' : 'badge-leave';
        tr.innerHTML = `
          <td class="py-3 px-3 font-mono text-slate-400">${log.date}</td>
          <td class="py-3 px-3 font-semibold text-white">${log.emp_code}</td>
          <td class="py-3 px-3"><span class="px-2 py-0.5 rounded-full text-[10px] font-bold ${badgeClass}">${log.status}</span></td>
          <td class="py-3 px-3 font-mono">${log.work_hours !== null ? log.work_hours.toFixed(2) + ' hrs' : '-'}</td>
          <td class="py-3 px-3 font-mono ${log.late_minutes > 0 ? 'text-amber-400 font-bold' : ''}">${log.late_minutes}</td>
          <td class="py-3 px-3 font-mono ${log.overtime_minutes > 0 ? 'text-emerald-400 font-bold' : ''}">${log.overtime_minutes}</td>
          <td class="py-3 px-3 font-mono text-slate-500">${log.history ? log.history.length : 0}</td>
          <td class="py-3 px-3 text-right">
            <button onclick="openModal('${log.emp_code}', '${log.date}', '${log.status}')" class="px-2.5 py-1 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/40 text-indigo-400 font-semibold text-[11px] transition">
              Regularize
            </button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function handlePunch(type) {
      const emp = document.getElementById('punchEmp').value;
      const status = document.getElementById('punchStatus').value;
      const alertBox = document.getElementById('punchAlert');
      alertBox.className = 'p-3.5 rounded-xl text-xs font-mono transition block';

      try {
        let res;
        if (type === 'in') {
          res = await fetch('/attendance/punch-in', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_code: emp, status: status})
          });
        } else {
          res = await fetch('/attendance/punch-out', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({emp_code: emp})
          });
        }
        const data = await res.json();
        if (res.ok) {
          alertBox.className += ' bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
          alertBox.innerHTML = `<strong>Success (${res.status}):</strong> ${type === 'in' ? 'Punched in for ' + data.date + ' (Late: ' + data.late_minutes + 'm)' : 'Punched out! Hours: ' + data.work_hours + 'h | Overtime: ' + data.overtime_minutes + 'm | Half Day: ' + data.half_day}`;
          refreshAll();
        } else {
          alertBox.className += ' bg-rose-500/10 text-rose-400 border border-rose-500/30';
          alertBox.innerHTML = `<strong>Error (${res.status}):</strong> ${data.detail || JSON.stringify(data)}`;
        }
      } catch (err) {
        alertBox.className += ' bg-rose-500/10 text-rose-400 border border-rose-500/30';
        alertBox.textContent = 'Network or server error: ' + err.message;
      }
    }

    function openModal(emp, date, currentStatus) {
      document.getElementById('modalEmp').value = emp;
      document.getElementById('modalDate').value = date;
      document.getElementById('modalStatus').value = currentStatus;
      document.getElementById('modalTitle').textContent = `Regularize: ${emp} (${date})`;
      document.getElementById('regModal').classList.remove('hidden');
    }

    function closeModal() {
      document.getElementById('regModal').classList.add('hidden');
    }

    async function submitRegularization() {
      const emp = document.getElementById('modalEmp').value;
      const date = document.getElementById('modalDate').value;
      const status = document.getElementById('modalStatus').value;
      const reason = document.getElementById('modalReason').value;
      const by = document.getElementById('modalBy').value;

      const res = await fetch(`/attendance/${emp}/${date}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status, reason, regularized_by: by})
      });
      const data = await res.json();
      closeModal();
      if (res.ok) {
        alert('Record regularized successfully!');
        refreshAll();
      } else {
        alert('Regularization error: ' + (data.detail || JSON.stringify(data)));
      }
    }

    function refreshAll() {
      loadEmployees();
      loadDepartmentSummary();
      loadLeaderboard();
      loadAttendanceLogs();
    }

    window.onload = refreshAll;
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, tags=["system"], include_in_schema=False)
def dashboard():
    """Serves the interactive frontend web dashboard."""
    return HTMLResponse(content=DASHBOARD_HTML)
