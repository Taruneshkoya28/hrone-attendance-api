# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** On `employees`: `(emp_code: 1, unique)` enforces unique IDs; `(department: 1, joined_on: 1)` and `(joined_on: 1)` back department filtering and trend headcount lookups. On `attendance_logs`: `(emp_code: 1, date: 1, unique)` guarantees one record per employee/day; `(date: -1, emp_code: 1)` drives paginated list sorting; `(date: 1, late_minutes: 1, emp_code: 1)` optimizes the late leaderboard; `(date: 1)` supports trend queries. We rejected indexing `(history.at: 1)` as audit history is only appended and never queried independently.

2. **Punch-in race.** Both requests compute the attendance date and attempt `db.attendance_logs.insert_one()`. Relying on the unique index on `(emp_code, date)` rather than check-then-insert ensures database-level atomicity: exactly one insert succeeds with 201, while the concurrent duplicate triggers a `DuplicateKeyError`, handled to return 409 Conflict.

3. **Ties.** Using MongoDB's `$rank` window function assigns standard competition ranking (1, 2, 2, 4). Filtering by `{rank: {$lte: limit}}` ensures all tied employees at or below the cutoff are included. When two employees tie at rank 2 with `limit=2`, both are returned (yielding 3 rows), sorted by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.** The aggregation pipeline starts on `employees` (filtering `joined_on <= month_end`) rather than `attendance_logs`. Grouping by department counts each employee (`$sum: 1`) before matching attendance records via `$lookup`, ensuring employees with zero logs are accurately counted.

5. **One thing you would change for 100x data.** Maintain pre-aggregated daily and monthly department summary collections updated asynchronously via MongoDB change streams or triggers, eliminating repetitive cross-collection `$lookup` joins and runtime densification across millions of documents.
