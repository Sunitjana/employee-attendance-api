from __future__ import annotations
import json, os, random, sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
IST = timezone(timedelta(hours=5, minutes=30))
checks = 0

def epoch(day, hhmm):
    return int(datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=IST).timestamp() * 1000)

def req(method, path, payload=None, query=None):
    url = BASE_URL + path + (("?" + urlencode(query)) if query else "")
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if data is not None: headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=20) as r:
            status, raw = r.status, r.read().decode()
    except HTTPError as e:
        status, raw = e.code, e.read().decode()
    except URLError as e:
        raise RuntimeError(f"Cannot connect to {BASE_URL}. Start Uvicorn first: {e.reason}") from e
    if not raw: return status, {}
    try: return status, json.loads(raw)
    except json.JSONDecodeError: return status, raw

def check(label, method, path, expected, payload=None, query=None):
    global checks
    status, body = req(method, path, payload, query)
    if status != expected:
        raise AssertionError(f"{label}: expected {expected}, received {status}: {body!r}")
    checks += 1
    print(f"[PASS] {label} (HTTP {status})")
    return body

def emp_payload(code, dept, **extra):
    d = {"emp_code": code, "name": "Extended Test Employee",
         "email": f"{code.lower()}@example.test", "department": dept,
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-10-01"}
    d.update(extra)
    return d

def new_code():
    return f"EMP{random.SystemRandom().randint(100000, 999999)}"

def main():
    print(f"Extended API tests: {BASE_URL}")
    check("root endpoint", "GET", "/", 200)
    check("health endpoint", "GET", "/health", 200)
    check("OpenAPI JSON", "GET", "/openapi.json", 200)
    check("favicon no-content route", "GET", "/favicon.ico", 204)

    dept = f"API-EXTRA-{random.randint(1000,9999)}"
    code = new_code()
    created = check("create employee", "POST", "/employees", 201, emp_payload(code, dept))
    assert created["emp_code"] == code
    assert isinstance(created["created_at"], int)
    print("[PASS] employee response shape and epoch timestamp")
    global checks
    checks += 1

    # Employee creation and listing / pagination
    check("employee list defaults", "GET", "/employees", 200)
    check("employee list page 1", "GET", "/employees", 200, query={"page":1,"page_size":10})
    check("employee list page 2", "GET", "/employees", 200, query={"page":2,"page_size":10})
    check("employee department filter", "GET", "/employees", 200, query={"department":dept})
    check("employee page zero rejected", "GET", "/employees", 422, query={"page":0})
    check("employee page size zero rejected", "GET", "/employees", 422, query={"page_size":0})
    check("employee page size 100 accepted", "GET", "/employees", 200, query={"page_size":100})
    check("employee page size 101 rejected", "GET", "/employees", 422, query={"page_size":101})
    check("invalid employee code rejected", "POST", "/employees", 422, emp_payload("EMPX",dept))
    check("invalid email rejected", "POST", "/employees", 422, emp_payload(new_code(),dept,email="invalid"))
    check("blank employee name rejected", "POST", "/employees", 422, emp_payload(new_code(),dept,name="   "))
    check("blank department rejected", "POST", "/employees", 422, emp_payload(new_code(),dept,department="   "))
    check("invalid join date rejected", "POST", "/employees", 422, emp_payload(new_code(),dept,joined_on="2026-02-30"))
    check("equal shift times rejected", "POST", "/employees", 422, emp_payload(new_code(),dept,shift_start="09:30",shift_end="09:30"))
    check("invalid shift format rejected", "POST", "/employees", 422, emp_payload(new_code(),dept,shift_start="9:30"))

    # Analytics validation and common paths
    check("monthly analytics for unknown employee", "GET", "/analytics/employees/EMP999999/monthly", 404, query={"month":"2026-10"})
    check("invalid month rejected", "GET", f"/analytics/employees/{code}/monthly", 422, query={"month":"2026-13"})
    check("department summary valid month", "GET", "/analytics/departments/summary", 200, query={"month":"2026-10","department":dept})
    check("department summary missing month", "GET", "/analytics/departments/summary", 422)
    check("late leaderboard valid", "GET", "/analytics/leaderboard/late", 200, query={"month":"2026-10","department":dept,"limit":10})
    check("leaderboard limit zero rejected", "GET", "/analytics/leaderboard/late", 422, query={"month":"2026-10","limit":0})
    check("leaderboard limit 51 rejected", "GET", "/analytics/leaderboard/late", 422, query={"month":"2026-10","limit":51})
    check("unknown department trend", "GET", "/analytics/departments/NO-SUCH-DEPT-999/trend", 404, query={"from":"2026-10-05","to":"2026-10-06"})
    check("trend reversed dates rejected", "GET", f"/analytics/departments/{dept}/trend", 422, query={"from":"2026-10-07","to":"2026-10-05"})
    check("trend invalid date rejected", "GET", f"/analytics/departments/{dept}/trend", 422, query={"from":"2026-02-30","to":"2026-03-01"})

    # Timestamp / attendance request validation
    check("unknown employee punch-in returns 404", "POST", "/attendance/punch-in", 404, {"emp_code":"EMP999999","punched_at":epoch("2026-10-05","09:30")})
    check("seconds timestamp rejected", "POST", "/attendance/punch-in", 422, {"emp_code":code,"punched_at":1780000000})
    check("negative timestamp rejected", "POST", "/attendance/punch-in", 422, {"emp_code":code,"punched_at":-1})
    check("float timestamp rejected", "POST", "/attendance/punch-in", 422, {"emp_code":code,"punched_at":1780000000000.5})
    check("explicit null timestamp rejected", "POST", "/attendance/punch-in", 422, {"emp_code":code,"punched_at":None})
    check("invalid attendance status rejected", "POST", "/attendance/punch-in", 422, {"emp_code":code,"punched_at":epoch("2026-10-05","09:30"),"status":"VACATION"})
    check("unknown employee punch-out returns 404", "POST", "/attendance/punch-out", 404, {"emp_code":"EMP999999","punched_at":epoch("2026-10-05","19:00")})

    # Create attendance, exercise punch-out validation and normal close
    start = epoch("2026-10-05","09:30")
    check("punch-in at shift start", "POST", "/attendance/punch-in", 201, {"emp_code":code,"punched_at":start})
    check("punch-out before punch-in rejected", "POST", "/attendance/punch-out", 422, {"emp_code":code,"punched_at":start-60000})
    check("punch-out equal to punch-in rejected", "POST", "/attendance/punch-out", 422, {"emp_code":code,"punched_at":start})
    check("punch-out beyond 24 hours rejected", "POST", "/attendance/punch-out", 422, {"emp_code":code,"punched_at":start+25*60*60*1000})
    closed = check("valid punch-out", "POST", "/attendance/punch-out", 200, {"emp_code":code,"punched_at":epoch("2026-10-05","14:00")})
    assert closed["work_hours"] == 4.5
    print("[PASS] 4.5-hour work-hours boundary")
    checks += 1

    # Attendance list / regularization
    check("attendance list defaults", "GET", "/attendance", 200)
    check("attendance filter by employee", "GET", "/attendance", 200, query={"emp_code":code})
    check("attendance invalid status rejected", "GET", "/attendance", 422, query={"status":"VACATION"})
    check("attendance page zero rejected", "GET", "/attendance", 422, query={"page":0})
    check("attendance page size 101 rejected", "GET", "/attendance", 422, query={"page_size":101})
    check("regularize missing date returns 404", "PATCH", f"/attendance/{code}/2026-10-06", 404, {"status":"WFH","regularized_by":"test.runner","reason":"test missing record"})
    check("invalid regularization path date", "PATCH", f"/attendance/{code}/2026-02-30", 422, {"status":"WFH","regularized_by":"test.runner","reason":"invalid date path"})
    check("short regularization reason rejected", "PATCH", f"/attendance/{code}/2026-10-05", 422, {"status":"WFH","regularized_by":"test.runner","reason":"bad"})
    check("blank regularizer rejected", "PATCH", f"/attendance/{code}/2026-10-05", 422, {"status":"WFH","regularized_by":"   ","reason":"valid reason here"})
    check("ABSENT with punch time rejected", "PATCH", f"/attendance/{code}/2026-10-05", 422, {"status":"ABSENT","punch_in":start,"regularized_by":"test.runner","reason":"invalid combination"})

    # Explain endpoint parameter validation
    check("explain attendance list valid", "GET", "/admin/explain/attendance_list", 200, query={"page":1,"page_size":20})
    check("unknown explain endpoint rejected", "GET", "/admin/explain/not_real", 422)
    check("monthly explain missing params", "GET", "/admin/explain/employee_monthly", 422)
    check("department explain missing month", "GET", "/admin/explain/department_summary", 422)
    check("trend explain missing required params", "GET", "/admin/explain/department_trend", 422)

    print(f"\nAll {checks} extended checks passed.")
    print("Test employees and records remain in MongoDB for inspection.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, KeyError, TypeError) as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1)
