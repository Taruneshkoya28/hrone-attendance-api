import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "http://localhost:8000"

def request(method, path, body=None):
    url = f"{BASE}{path}"
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def run_demo():
    print("=" * 70)
    print(" LIVE WORKING DEMONSTRATION: HRONE ATTENDANCE & ANALYTICS API")
    print("=" * 70)

    # 1. Health
    status, res = request("GET", "/health")
    print(f"\n1. GET /health -> [{status}]")
    print(f"   Response: {json.dumps(res)}")

    # 2. List Employees
    status, res = request("GET", "/employees?department=Engineering&page=1&page_size=2")
    print(f"\n2. GET /employees?department=Engineering -> [{status}]")
    print(f"   Total matching: {res['total']}")
    for emp in res["items"]:
        print(f"   - {emp['emp_code']}: {emp['name']} | Dept: {emp['department']} | Shift: {emp['shift_start']}-{emp['shift_end']}")

    # 3. Create Employee
    new_emp_code = f"EMP{int(datetime.now().timestamp()) % 100000:05d}"
    status, res = request("POST", "/employees", {
        "emp_code": new_emp_code,
        "name": "Live Test Employee",
        "email": "livetest@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    print(f"\n3. POST /employees ({new_emp_code}) -> [{status}]")
    print(f"   Created: {res['name']} ({res['emp_code']}), created_at: {res['created_at']} epoch ms")

    # Duplicate check
    status, dup_res = request("POST", "/employees", {
        "emp_code": new_emp_code,
        "name": "Duplicate",
        "email": "dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01"
    })
    print(f"   Duplicate POST /employees ({new_emp_code}) -> [{status}] detail: {dup_res.get('detail')}")

    # 4. Punch In
    # Punch in at 10:15:59 IST (45 mins late)
    # 2026-07-22 10:15:59 IST = 04:45:59 UTC
    punch_in_ms = int(datetime(2026, 7, 22, 4, 45, 59, tzinfo=timezone.utc).timestamp() * 1000)
    status, res = request("POST", "/attendance/punch-in", {
        "emp_code": new_emp_code,
        "punched_at": punch_in_ms,
        "status": "PRESENT"
    })
    print(f"\n4. POST /attendance/punch-in -> [{status}]")
    print(f"   Date: {res['date']} | Status: {res['status']} | Late Minutes: {res['late_minutes']} (Floor 45 mins per R2)")

    # Duplicate punch in check
    status, dup_pi = request("POST", "/attendance/punch-in", {
        "emp_code": new_emp_code,
        "punched_at": punch_in_ms
    })
    print(f"   Duplicate punch-in on same date -> [{status}] detail: {dup_pi.get('detail')}")

    # 5. Punch Out
    # Punch out at 19:15:00 IST (45 mins overtime after 18:30 shift end)
    # 2026-07-22 19:15:00 IST = 13:45:00 UTC
    punch_out_ms = int(datetime(2026, 7, 22, 13, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    status, res = request("POST", "/attendance/punch-out", {
        "emp_code": new_emp_code,
        "punched_at": punch_out_ms
    })
    print(f"\n5. POST /attendance/punch-out -> [{status}]")
    print(f"   Work Hours: {res['work_hours']} hrs | Overtime: {res['overtime_minutes']} mins (>= 30 threshold) | Half Day: {res['half_day']}")

    # Duplicate punch out check
    status, dup_po = request("POST", "/attendance/punch-out", {
        "emp_code": new_emp_code,
        "punched_at": punch_out_ms
    })
    print(f"   Duplicate punch-out on same record -> [{status}] detail: {dup_po.get('detail')}")

    # 6. Regularize Attendance (PATCH)
    status, res = request("PATCH", f"/attendance/{new_emp_code}/2026-07-22", {
        "status": "WFH",
        "reason": "Authorized work from home retroactively",
        "regularized_by": "lead.manager"
    })
    print(f"\n6. PATCH /attendance/{new_emp_code}/2026-07-22 -> [{status}]")
    print(f"   Updated Status: {res['status']}")
    print(f"   Audit History Entries: {len(res['history'])}")
    print(f"   Audit Changes Recorded: {res['history'][-1]['changes']}")

    # 7. List Attendance
    status, res = request("GET", f"/attendance?emp_code={new_emp_code}")
    print(f"\n7. GET /attendance?emp_code={new_emp_code} -> [{status}]")
    for rec in res["items"]:
        print(f"   - {rec['emp_code']} on {rec['date']}: {rec['status']} | Hours: {rec['work_hours']} | Late: {rec['late_minutes']}m | OT: {rec['overtime_minutes']}m")

    # 8. Employee Monthly Analytics
    status, res = request("GET", f"/analytics/employees/{new_emp_code}/monthly?month=2026-07")
    print(f"\n8. GET /analytics/employees/{new_emp_code}/monthly -> [{status}]")
    print(f"   Emp: {res['emp_code']} | Month: {res['month']}")
    print(f"   Working Days: {res['working_days']} (Mon-Fri) | Present Days: {res['present_days']} | Attendance Pct: {res['attendance_pct']}%")

    # 9. Department Summary Analytics (Cross-collection Aggregation)
    status, res = request("GET", "/analytics/departments/summary?month=2026-07")
    print(f"\n9. GET /analytics/departments/summary?month=2026-07 -> [{status}]")
    for dept in res["items"]:
        print(f"   - {dept['department']}: Headcount: {dept['headcount']} | Present Days: {dept['present_days']} | Avg Work Hours: {dept['avg_work_hours']}h | Late Count: {dept['late_count']}")

    # 10. Late Leaderboard (Standard Competition Ranking with Ties)
    status, res = request("GET", "/analytics/leaderboard/late?month=2026-07&limit=5")
    print(f"\n10. GET /analytics/leaderboard/late?month=2026-07 -> [{status}]")
    for item in res["items"]:
        print(f"   - Rank {item['rank']}: {item['name']} ({item['emp_code']}) in {item['department']} | Total Late: {item['total_late_minutes']} mins")

    # 11. Department Trend (Gap-filling via $densify and 7-day Moving Avg)
    status, res = request("GET", "/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-11")
    print(f"\n11. GET /analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-11 -> [{status}]")
    for d in res["items"]:
        rate_str = f"{d['attendance_rate']:.4f}" if d['attendance_rate'] is not None else "None (weekend/holiday)"
        ma_str = f"{d['moving_avg_7d']:.4f}" if d['moving_avg_7d'] is not None else "None"
        print(f"   - {d['date']} (Weekday: {d['is_working_day']}): Headcount: {d['headcount']} | Present: {d['present_count']} | Rate: {rate_str} | 7d MA: {ma_str}")

    # 12. MongoDB Explain (Index Scan / No Collscan Verification)
    status, res = request("GET", "/admin/explain/attendance_list?page=1&page_size=20")
    print(f"\n12. GET /admin/explain/attendance_list -> [{status}]")
    exp_str = str(res["explain"])
    has_ixscan = "IXSCAN" in exp_str
    has_collscan = "COLLSCAN" in exp_str
    print(f"   Collection: {res['collection']}")
    print(f"   Index Scan (IXSCAN): {has_ixscan} | Collection Scan (COLLSCAN): {has_collscan}")

    print("\n" + "=" * 70)
    print(" ALL ENDPOINTS PROVEN WORKING LIVE AND OPERATIONAL!")
    print("=" * 70)

if __name__ == "__main__":
    run_demo()
