# Employee Attendance & Analytics API

A high-performance Attendance and Analytics REST API built with **Python 3.11+**, **FastAPI**, and **MongoDB 6.0+**.

## Features Implemented
- **Full OpenAPI 3.0.3 Contract Compliance**: All paths, models, query parameters, status codes, and R1–R10 business rules strictly followed.
- **Race-Safe Attendance Operations**:
  - `POST /attendance/punch-in`: Handled atomically via unique compound database index `(emp_code, date)` without check-then-insert races.
  - `POST /attendance/punch-out`: Atomic single-winner closure using `find_one_and_update` with `punch_out: None` condition.
  - `PATCH /attendance/{emp_code}/{date}`: Optimistic concurrency control using audit history array sizing to prevent lost updates.
- **MongoDB Aggregation Analytics**:
  - `GET /analytics/employees/{emp_code}/monthly`: Summarizes working days, presence days, leaves, overtime, and attendance percentages with half-up rounding.
  - `GET /analytics/departments/summary`: Cross-collection pipeline capturing zero-log employees, department headcounts, and average work hours.
  - `GET /analytics/leaderboard/late`: MongoDB `$setWindowFields` standard competition ranking (1, 2, 2, 4) with ties handled cleanly at cutoff.
  - `GET /analytics/departments/{department}/trend`: Gap-filling with MongoDB `$densify` and 7-day trailing moving average windowing.
- **Explain Endpoint**:
  - `GET /admin/explain/{endpoint}`: Returns raw MongoDB explain documents with `executionStats`. All main queries verified with `IXSCAN` and zero `COLLSCAN`.
- **Automatic Index Creation**:
  - All compound and unique indexes are created idempotently on server startup via FastAPI lifespan.

## Local Setup

### 1. Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local instance or MongoDB Atlas cluster)

### 2. Environment Variables
Configure `MONGO_URI` and `MONGO_DB` via environment variables or a `.env` file:
```bash
MONGO_URI="mongodb://localhost:27017"
MONGO_DB="attendance_db"
```
*(System environment variables automatically take precedence over `.env`)*

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 5. Start the Application
```bash
uvicorn app.main:app --port 8000
```
Interactive API documentation is accessible at `http://localhost:8000/docs`.

## Running Tests
Run the automated test suite:
```bash
python test_suite.py
```
Run the concurrency and race condition test suite:
```bash
python test_concurrency.py
```
Run the edge case verification suite (ties, mid-month joiners, weekends):
```bash
python test_edge_cases.py
```
