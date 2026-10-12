"""Employee Attendance & Analytics API — HROne assignment."""
import os
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
from bson.json_util import CANONICAL_JSON_OPTIONS
from fastapi.responses import JSONResponse

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def truncate_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def epoch_to_dt(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "invalid epoch milliseconds")


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def rounded(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def clock_parts(value: str):
    h, m = map(int, value.split(":"))
    return h, m


def attendance_date(punch_in: datetime, emp: dict) -> str:
    local = punch_in.astimezone(IST)
    sh, sm = clock_parts(emp["shift_start"])
    eh, em = clock_parts(emp["shift_end"])
    start_minutes, end_minutes = sh * 60 + sm, eh * 60 + em
    day = local.date()
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(day: str, emp: dict) -> datetime:
    h, m = clock_parts(emp["shift_start"])
    return datetime.combine(date.fromisoformat(day), time(h, m), IST).astimezone(UTC)


def shift_end_dt(day: str, emp: dict) -> datetime:
    h, m = clock_parts(emp["shift_end"])
    d = date.fromisoformat(day)
    sh, sm = clock_parts(emp["shift_start"])
    if h * 60 + m <= sh * 60 + sm:
        d += timedelta(days=1)
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)



def compute_late_minutes(punch_in: datetime, day: str, emp: dict) -> int:
    shift_start = shift_start_dt(day, emp)
    elapsed_seconds = (punch_in - shift_start).total_seconds()

    # Late only when strictly more than 10 minutes after shift start.
    if elapsed_seconds <= 600:
        return 0

    # Return whole minutes elapsed since shift start.
    return int(elapsed_seconds // 60)


def compute_overtime(punch_out: datetime, day: str, emp: dict) -> int:
    shift_end = shift_end_dt(day, emp)
    elapsed_seconds = (punch_out - shift_end).total_seconds()
    minutes = int(elapsed_seconds // 60)

    # Overtime counts only when it reaches at least 30 minutes.
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return rounded(seconds / 3600, 2)



def employee_out(doc):
    d = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in d:
        d["created_at"] = to_ms(d["created_at"])
    return d


def attendance_out(doc):
    d = {k: v for k, v in doc.items() if k != "_id"}
    d.setdefault("punch_in", None)
    d.setdefault("punch_out", None)
    d.setdefault("work_hours", None)
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    d.setdefault("history", [])
    d["punch_in"] = to_ms(d["punch_in"])
    d["punch_out"] = to_ms(d["punch_out"])
    hist = []
    for entry in d.get("history", []):
        e = dict(entry)
        e["at"] = to_ms(e.get("at"))
        changes = {}
        for key, change in e.get("changes", {}).items():
            ch = dict(change)
            if key in ("punch_in", "punch_out"):
                ch["from"] = to_ms(ch.get("from"))
                ch["to"] = to_ms(ch.get("to"))
            changes[key] = ch
        e["changes"] = changes
        hist.append(e)
    d["history"] = hist
    return d


def valid_date(value: str, field="date"):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def month_bounds(month: str):
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        y, m = map(int, month.split("-"))
        if not 1 <= m <= 12:
            raise ValueError
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        return first.isoformat(), last.isoformat(), first, last
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be YYYY-MM")


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def rounded_rate(n, d):
    return None if not d else rounded((Decimal(str(n)) / Decimal(str(d))), 4)


def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uniq_emp_code"
    )

    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="dept_joined_on"
    )

    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department"
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_date"
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_emp_code_desc"
    )

    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="ix_attendance_date_status"
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punchin"
    )


@app.on_event("startup")
def startup():
    create_indexes()


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined(cls, v):
        valid_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            epoch_to_dt(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            epoch_to_dt(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, v):
        if v is not None:
            epoch_to_dt(v)
        return v


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
                 .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = truncate_dt(epoch_to_dt(body.punched_at) if body.punched_at is not None else now_utc())
    day = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, day, emp), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = truncate_dt(epoch_to_dt(body.punched_at) if body.punched_at is not None else now_utc())
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    pin = truncate_dt(record["punch_in"])
    seconds = (ts - pin).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    hours = compute_work_hours(pin, ts)
    update = {"punch_out": ts, "work_hours": hours,
              "overtime_minutes": compute_overtime(ts, record["date"], emp),
              "half_day": hours < 4.50}
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update}, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "record already punched out")
    return attendance_out(result)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from:
        valid_date(date_from, "date_from")
    if date_to:
        valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_out(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    new = dict(old)
    supplied = body.model_fields_set
    if "status" in supplied and body.status is not None:
        new["status"] = body.status
    if "punch_in" in supplied:
        new["punch_in"] = truncate_dt(epoch_to_dt(body.punch_in)) if body.punch_in is not None else None
    if "punch_out" in supplied:
        new["punch_out"] = truncate_dt(epoch_to_dt(body.punch_out)) if body.punch_out is not None else None

    status = new.get("status")
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        new["punch_in"] = new["punch_out"] = None
        new.update(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    else:
        pin, pout = new.get("punch_in"), new.get("punch_out")
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        pin = truncate_dt(pin)
        if attendance_date(pin, emp) != date:
            raise HTTPException(422, "punch_in must belong to attendance date")
        new["punch_in"] = pin
        new["late_minutes"] = compute_late_minutes(pin, date, emp)
        if pout is None:
            new.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            pout = truncate_dt(pout)
            seconds = (pout - pin).total_seconds()
            if seconds <= 0 or seconds > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            new["punch_out"] = pout
            hours = compute_work_hours(pin, pout)
            new.update(work_hours=hours, overtime_minutes=compute_overtime(pout, date, emp), half_day=hours < 4.50)

    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in fields:
        before, after = old.get(field), new.get(field)
        if field in ("late_minutes", "overtime_minutes"):
            before, after = before or 0, after or 0
        if field == "half_day":
            before, after = bool(before), bool(after)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "regularization must change at least one field")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    new["history"] = list(old.get("history", [])) + [entry]
    # Compare-and-swap on old values prevents concurrent edits from overwriting history.
    selector = {"_id": old["_id"], "history": old.get("history", [])}
    if "punch_in" in old: selector["punch_in"] = old.get("punch_in")
    if "punch_out" in old: selector["punch_out"] = old.get("punch_out")
    result = db.attendance_logs.find_one_and_update(selector, {"$set": {
        "status": new["status"], "punch_in": new["punch_in"], "punch_out": new["punch_out"],
        "work_hours": new["work_hours"], "late_minutes": new["late_minutes"],
        "overtime_minutes": new["overtime_minutes"], "half_day": new["half_day"]},
        "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return attendance_out(result)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_s, end_s, start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    joined = valid_date(emp["joined_on"], "joined_on")
    wd = working_days(max(start, joined), end) if joined <= end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$group": {"_id": None,
          "present_days": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
             {"$cond": [{"$and": [{"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}, 0]}},
          "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
          "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    a = result[0] if result else {}
    present = rounded(a.get("present_days", 0), 2)
    pct = rounded((present / wd) * 100, 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": a.get("leave_days", 0), "late_count": a.get("late_count", 0),
            "total_late_minutes": a.get("total_late_minutes", 0),
            "total_overtime_minutes": a.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_s, end_s, start, end = month_bounds(month)
    employee_match = {"joined_on": {"$lte": end_s}}
    if department is not None: employee_match["department"] = department
    # Aggregate per employee first so multiple daily logs never inflate headcount.
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
          "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
            {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}},
            {"$project": {"status": 1, "date": 1, "half_day": 1, "work_hours": 1,
                          "late_minutes": 1, "overtime_minutes": 1}}],
          "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"emp_code": "$emp_code", "department": "$department"},
          "present_days": {"$sum": {"$cond": [{"$and": [
             {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE)]},
             {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
             {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, 7]},
             {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, 1]}]},
             {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
          "work_hours": {"$push": {"$cond": [{"$and": [
             {"$in": [{"$ifNull": ["$logs.status", ""]}, list(PRESENCE)]},
             {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", None]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1},
          "present_days": {"$sum": "$present_days"},
          "work_hours": {"$push": "$work_hours"},
          "late_count": {"$sum": "$late_count"},
          "total_late_minutes": {"$sum": "$total_late_minutes"},
          "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$sort": {"_id": 1}}
    ]
    # More reliable headcount and zero-log handling: aggregation groups employee documents, not logs.
    rows = list(db.employees.aggregate(pipeline))
    
    items = []
    for row in rows:
        # Flatten the nested arrays and ignore missing work-hour values.
        hours = [
            value
            for employee_hours in (row.get("work_hours") or [])
            for value in employee_hours
            if value is not None
        ]

        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": rounded(row.get("present_days", 0), 2),
            "avg_work_hours": (
                rounded(sum(hours) / len(hours), 2)
                if hours else None
            ),
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds(month)
    pipeline = [
      {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                  "late_count": {"$sum": 1}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
      {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
      {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
      {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
        "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                    "total_late_minutes": 1, "late_count": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = valid_date(from_, "from"), valid_date(to, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    days = (end - start).days + 1
    if days > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    # $densify creates missing calendar days in MongoDB; Python only formats the final derived metrics.
    pipeline = [
      {"$match": {"date": {"$gte": from_, "$lte": to}}},
      {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
      {"$unwind": "$employee"},
      {"$match": {"employee.department": department}},
      {"$group": {"_id": "$date",
        "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
          {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
      {"$project": {"date": "$_id", "present_count": 1, "late_count": 1, "_id": 0}},
      {"$addFields": {"day": {"$dateFromString": {"dateString": "$date"}}}},
      {"$densify": {
    "field": "day",
    "range": {
        "step": 1,
        "unit": "day",
        "bounds": [
            datetime.combine(start, datetime.min.time()),
            datetime.combine(end, datetime.min.time())
        ]
    }
}},
      {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}}},
      {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
      {"$sort": {"date": 1}}
    ]
    raw = list(db.attendance_logs.aggregate(pipeline))
    by_date = {x["date"]: x for x in raw}
    # Include every calendar day as a defensive fallback for MongoDB versions with boundary behavior differences.
    employees = list(db.employees.find({"department": department, "joined_on": {"$lte": to}}, {"joined_on": 1, "_id": 0}))
    rates = []
    items = []
    cur = start
    while cur <= end:
        ds = cur.isoformat()
        rec = by_date.get(ds, {})
        headcount = sum(1 for e in employees if e["joined_on"] <= ds)
        is_working = cur.weekday() < 5
        present = float(rec.get("present_count", 0))
        late = int(rec.get("late_count", 0))
        rate = rounded_rate(present, headcount) if is_working and headcount > 0 else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        moving = rounded(sum(window) / len(window), 4) if window else None
        items.append({"date": ds, "is_working_day": is_working, "headcount": headcount,
          "present_count": rounded(present, 2), "late_count": late,
          "attendance_rate": rate, "moving_avg_7d": moving})
        cur += timedelta(days=1)
    return {"department": department, "items": items}


def explain_json(collection, operation):
    # MongoDB's explain result may contain BSON values; convert it to API-safe JSON.
    return json.loads(json_util.dumps(operation, json_options=CANONICAL_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                     emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if date_from: valid_date(date_from, "date_from")
        if date_to: valid_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        explain = db.command("explain", {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start_s, end_s, _, _ = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
                    {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        _, end_s, _, _ = month_bounds(month)
        match = {"joined_on": {"$lte": end_s}}
        if department is not None: match["department"] = department
        pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}]
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start_s, end_s, _, _ = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}}
        ]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        valid_date(from_, "from"); valid_date(to, "to")
        pipeline = [{"$match": {"date": {"$gte": from_, "$lte": to}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
