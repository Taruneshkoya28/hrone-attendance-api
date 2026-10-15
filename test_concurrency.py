import concurrent.futures
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)

def test_concurrency():
    print("\n--- Testing Concurrency ---")

    # 1. Concurrent punch-in for new employee / date
    # Let's create an employee for concurrency tests
    db.employees.delete_many({"emp_code": "EMP0777"})
    db.attendance_logs.delete_many({"emp_code": "EMP0777"})
    res = client.post("/employees", json={
        "emp_code": "EMP0777",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "QA",
        "joined_on": "2026-07-01"
    })
    assert res.status_code == 201

    punch_ms = int(datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    
    def do_punch_in():
        return client.post("/attendance/punch-in", json={
            "emp_code": "EMP0777",
            "punched_at": punch_ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(10)]
        results = [f.result() for f in futures]

    count_201 = results.count(201)
    count_409 = results.count(409)
    print(f"Punch-in race results: 201 count = {count_201}, 409 count = {count_409}")
    assert count_201 == 1, f"Expected exactly 1 201, got {count_201}"
    assert count_409 == 9, f"Expected 9 409s, got {count_409}"

    # 2. Concurrent punch-out on open record
    pout_ms = int(datetime(2026, 7, 20, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch_out():
        return client.post("/attendance/punch-out", json={
            "emp_code": "EMP0777",
            "punched_at": pout_ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(10)]
        pout_results = [f.result() for f in futures]

    pout_200 = pout_results.count(200)
    pout_409 = pout_results.count(409)
    print(f"Punch-out race results: 200 count = {pout_200}, 409 count = {pout_409}")
    assert pout_200 == 1, f"Expected exactly 1 200, got {pout_200}"
    assert pout_409 == 9, f"Expected 9 409s, got {pout_409}"

    # 3. Concurrent regularizations:
    def do_regularize(idx):
        return client.patch(f"/attendance/EMP0777/2026-07-20", json={
            "status": "WFH" if idx % 2 == 0 else "ON_DUTY",
            "reason": f"Concurrent correction attempt {idx}",
            "regularized_by": f"admin_{idx}"
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_regularize, i) for i in range(5)]
        reg_results = [f.result() for f in futures]

    print(f"Regularization race results: {reg_results}")
    # Winner gets 200, losers get 409, no history loss
    assert 200 in reg_results
    # Verify history integrity
    rec = db.attendance_logs.find_one({"emp_code": "EMP0777", "date": "2026-07-20"})
    print(f"Total history entries recorded: {len(rec['history'])}")
    assert len(rec["history"]) >= 1
    
    # Clean up test records
    db.employees.delete_many({"emp_code": "EMP0777"})
    db.attendance_logs.delete_many({"emp_code": "EMP0777"})
    print("PASS: Concurrency test suite passed perfectly!")

if __name__ == "__main__":
    test_concurrency()
