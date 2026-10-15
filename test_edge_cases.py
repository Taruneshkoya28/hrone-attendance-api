import calendar
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)

def test_ties_and_edge_cases():
    print("\n--- Testing Ties on Leaderboard ---")
    # Clean up test data
    db.employees.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002", "EMP9003", "EMP9004"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002", "EMP9003", "EMP9004"]}})

    for code, name in [("EMP9001", "User A"), ("EMP9002", "User B"), ("EMP9003", "User C"), ("EMP9004", "User D")]:
        db.employees.insert_one({
            "emp_code": code,
            "name": name,
            "email": f"{code.lower()}@example.com",
            "department": "TieDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc)
        })

    # Emp A: 60 min late
    db.attendance_logs.insert_one({
        "emp_code": "EMP9001", "date": "2026-08-03", "status": "PRESENT",
        "late_minutes": 60, "punch_in": datetime.now(timezone.utc), "punch_out": None,
        "work_hours": None, "overtime_minutes": 0, "half_day": False, "history": []
    })
    # Emp B: 40 min late
    db.attendance_logs.insert_one({
        "emp_code": "EMP9002", "date": "2026-08-03", "status": "PRESENT",
        "late_minutes": 40, "punch_in": datetime.now(timezone.utc), "punch_out": None,
        "work_hours": None, "overtime_minutes": 0, "half_day": False, "history": []
    })
    # Emp C: 40 min late (tied with B)
    db.attendance_logs.insert_one({
        "emp_code": "EMP9003", "date": "2026-08-03", "status": "PRESENT",
        "late_minutes": 40, "punch_in": datetime.now(timezone.utc), "punch_out": None,
        "work_hours": None, "overtime_minutes": 0, "half_day": False, "history": []
    })
    # Emp D: 20 min late
    db.attendance_logs.insert_one({
        "emp_code": "EMP9004", "date": "2026-08-03", "status": "PRESENT",
        "late_minutes": 20, "punch_in": datetime.now(timezone.utc), "punch_out": None,
        "work_hours": None, "overtime_minutes": 0, "half_day": False, "history": []
    })

    # Test with limit=2 in TieDept
    res = client.get("/analytics/leaderboard/late?month=2026-08&limit=2&department=TieDept")
    assert res.status_code == 200
    items = res.json()["items"]
    print("Leaderboard items with limit=2 (expect 3 items due to tie at rank 2):")
    for it in items:
        print(" ", it)
    assert len(items) == 3, f"Expected 3 items due to tie at cutoff rank 2, got {len(items)}"
    assert items[0]["rank"] == 1 and items[0]["emp_code"] == "EMP9001"
    assert items[1]["rank"] == 2 and items[1]["emp_code"] == "EMP9002"
    assert items[2]["rank"] == 2 and items[2]["emp_code"] == "EMP9003"

    # Test with limit=4
    res = client.get("/analytics/leaderboard/late?month=2026-08&limit=4&department=TieDept")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 4
    assert items[3]["rank"] == 4 and items[3]["emp_code"] == "EMP9004"
    print("PASS: Standard competition ranking (1, 2, 2, 4) and tie cutoff verified!")

    print("\n--- Testing Mid-Month Joiner & Weekend Presence ---")
    # Joiner on 2026-08-17 (Monday)
    db.employees.delete_many({"emp_code": "EMP9005"})
    db.attendance_logs.delete_many({"emp_code": "EMP9005"})
    db.employees.insert_one({
        "emp_code": "EMP9005", "name": "Mid Month", "email": "mid@example.com",
        "department": "TieDept", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-08-17", "created_at": datetime.now(timezone.utc)
    })
    # August 2026 weekdays: Aug 17 to Aug 31:
    # Aug 17-21 (5), Aug 24-28 (5), Aug 31 (1) = 11 weekdays!
    res = client.get("/analytics/employees/EMP9005/monthly?month=2026-08")
    assert res.status_code == 200
    assert res.json()["working_days"] == 11, f"Expected 11 working days, got {res.json()['working_days']}"

    # Now add a Saturday record (2026-08-22) for EMP9005 with late and overtime
    db.attendance_logs.insert_one({
        "emp_code": "EMP9005", "date": "2026-08-22", "status": "PRESENT",
        "punch_in": datetime.now(timezone.utc), "punch_out": datetime.now(timezone.utc),
        "work_hours": 8.0, "late_minutes": 15, "overtime_minutes": 45,
        "half_day": False, "history": []
    })
    res = client.get("/analytics/employees/EMP9005/monthly?month=2026-08")
    assert res.status_code == 200
    data = res.json()
    # Weekend record: present_days is 0.0 (Mon-Fri only), but late_minutes and overtime_minutes contributed!
    assert data["present_days"] == 0.0, f"Expected 0.0 present_days for weekend, got {data['present_days']}"
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 15
    assert data["total_overtime_minutes"] == 45
    print("PASS: Mid-month joiner and weekend presence rules verified!")

    # Cleanup
    db.employees.delete_many({"emp_code": {"$regex": "^EMP900"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP900"}})

if __name__ == "__main__":
    test_ties_and_edge_cases()
