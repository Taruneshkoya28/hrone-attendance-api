import calendar
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes

client = TestClient(app)

def run_tests():
    print("--- 1. Testing GET /health ---")
    res = client.get("/health")
    assert res.status_code == 200, f"Health failed: {res.text}"
    assert res.json() == {"status": "ok"}
    print("PASS: /health")

    print("\n--- 2. Testing POST /employees & GET /employees ---")
    # Duplicate employee EMP0001 should 409
    dup = {
        "emp_code": "EMP0001",
        "name": "Duplicate Asha",
        "email": "asha_dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=dup)
    assert res.status_code == 409, f"Expected 409, got {res.status_code}: {res.text}"

    # Invalid employee input (shift_start == shift_end)
    bad_shift = {
        "emp_code": "EMP9999",
        "name": "Bad Shift",
        "email": "bad@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=bad_shift)
    assert res.status_code == 422, f"Expected 422 for equal shifts, got {res.status_code}"

    # Invalid emp_code regex
    bad_code = {
        "emp_code": "XYZ123",
        "name": "Bad Code",
        "email": "bad@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=bad_code)
    assert res.status_code == 422, f"Expected 422 for bad emp_code, got {res.status_code}"

    # Valid employee creation
    new_emp = {
        "emp_code": "EMP0100",
        "name": "Pooja Hegde",
        "email": "pooja@example.com",
        "department": "Design",
        "joined_on": "2026-07-01"
    }
    res = client.post("/employees", json=new_emp)
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    created = res.json()
    assert created["emp_code"] == "EMP0100"
    assert "_id" not in created
    assert isinstance(created["created_at"], int)

    # List employees
    res = client.get("/employees?department=Design")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["emp_code"] == "EMP0100"
    print("PASS: /employees")

    print("\n--- 3. Testing POST /attendance/punch-in ---")
    # Punch in for unknown employee -> 404
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

    # Punch in already punched in on date -> 409
    # EMP0001 already has record on 2026-07-06
    # 2026-07-06 09:30:00 IST = 2026-07-06 04:00:00 UTC = 1783310400000 ms
    dt_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": dt_ms})
    assert res.status_code == 409, f"Expected 409 for duplicate date, got {res.status_code}"

    # Overnight shift punch in before shift_end:
    # EMP0005 shift 22:00 to 06:00.
    # Punch in at 2026-07-08 02:00:00 IST (which is < 06:00). Belongs to 2026-07-07!
    # 2026-07-08 02:00:00 IST = 2026-07-07 20:30:00 UTC
    overnight_ms = int(datetime(2026, 7, 7, 20, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": overnight_ms})
    assert res.status_code == 201, f"Expected 201, got {res.status_code}: {res.text}"
    att = res.json()
    assert att["date"] == "2026-07-07", f"Expected date 2026-07-07, got {att['date']}"
    assert att["late_minutes"] == 240, f"Expected 240 late minutes, got {att['late_minutes']}"
    print("PASS: /attendance/punch-in")

    print("\n--- 4. Testing POST /attendance/punch-out ---")
    # Punch out unknown employee -> 404
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

    # Punch out already punched out record (EMP0001 on 2026-07-06) -> 409
    pout_ms = int(datetime(2026, 7, 6, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pout_ms})
    assert res.status_code == 409

    # Punch out open record EMP0006 on 2026-07-07:
    # EMP0006 punched in at 2026-07-07T04:05:00Z (09:35 IST). Shift 09:30 - 18:30.
    # Punch out at 19:15 IST = 13:45 UTC = 2026-07-07T13:45:00Z
    pout_open_ms = int(datetime(2026, 7, 7, 13, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": pout_open_ms})
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    rec = res.json()
    assert rec["punch_out"] == pout_open_ms
    assert rec["work_hours"] == 9.67 # (13:45 - 04:05) = 9h40m = 34800s / 3600 = 9.6666... -> 9.67
    assert rec["overtime_minutes"] == 45 # 19:15 - 18:30 = 45m >= 30m -> 45
    assert rec["half_day"] == False

    # Second punch out should now 409!
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": pout_open_ms})
    assert res.status_code == 409
    print("PASS: /attendance/punch-out")

    print("\n--- 5. Testing GET /attendance ---")
    # Date validation: date_from > date_to -> 422
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res.status_code == 422

    res = client.get("/attendance?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 5
    # Verify sorting: date descending, then emp_code ascending
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)
    print("PASS: /attendance")

    print("\n--- 6. Testing PATCH /attendance/{emp_code}/{date} ---")
    # Setting ABSENT with punch_in -> 422
    res = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "ABSENT", "punch_in": dt_ms, "reason": "Testing bad absent", "regularized_by": "admin"}
    )
    assert res.status_code == 422

    # Changing nothing -> 422
    res = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "Testing no change", "regularized_by": "admin"}
    )
    assert res.status_code == 422

    # Valid regularization: regularize EMP0001 status to WFH
    res = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "WFH", "reason": "Approved retroactive WFH", "regularized_by": "manager.joe"}
    )
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    patched = res.json()
    assert patched["status"] == "WFH"
    assert len(patched["history"]) == 1
    h0 = patched["history"][0]
    assert h0["by"] == "manager.joe"
    assert h0["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}
    print("PASS: PATCH /attendance/{emp_code}/{date}")

    print("\n--- 7. Testing Analytics: employee monthly ---")
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["late_count"] == 0
    assert data["attendance_pct"] == 8.7
    print("PASS: employee monthly")

    print("\n--- 8. Testing Analytics: department summary ---")
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    assert depts["Sales"]["late_count"] == 1
    assert depts["Sales"]["total_late_minutes"] == 35
    print("PASS: department summary")

    print("\n--- 9. Testing Analytics: late leaderboard ---")
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert len(data["items"]) >= 1
    first = data["items"][0]
    assert first["rank"] == 1
    assert first["emp_code"] in ("EMP0003", "EMP0005")
    print("PASS: late leaderboard")

    print("\n--- 10. Testing Analytics: department trend ---")
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-11")
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
    data = res.json()
    assert len(data["items"]) == 7
    days = [item["date"] for item in data["items"]]
    assert days == ["2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"]
    print("PASS: department trend")

    print("\n--- 11. Testing GET /admin/explain/{endpoint} ---")
    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        params = {}
        if ep == "employee_monthly":
            params = {"emp_code": "EMP0001", "month": "2026-07"}
        elif ep in ("department_summary", "late_leaderboard"):
            params = {"month": "2026-07"}
        elif ep == "department_trend":
            params = {"department": "Engineering", "from": "2026-07-05", "to": "2026-07-11"}
        
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain {ep} failed: {res.status_code}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        # Check no COLLSCAN in explain
        exp_text = str(data["explain"])
        assert "COLLSCAN" not in exp_text, f"Explain for {ep} contains COLLSCAN!"
        print(f"PASS: explain {ep} on {data['collection']} (no COLLSCAN)")

    print("\nALL 11 TEST SUITES PASSED PERFECTLY!")

if __name__ == "__main__":
    run_tests()
