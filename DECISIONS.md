# DECISIONS.md

1. **Indexes.** On `attendance_logs`, unique `(emp_code, date)` enforces one record per day. It also makes punch-in race-safe and serves the monthly query, punch-out, PATCH and the `$lookup`s from employees. `(date desc, emp_code)` matches the list sort exactly and serves the month ranges for the leaderboard. `(status, date desc, emp_code)` covers a status-filtered list. On `employees`: unique `emp_code`, `(department, joined_on)` for the trend and the filtered summary, `(department, emp_code)` for the list, and `joined_on` for the all-department summary. I rejected an index on `late_minutes`: the date range is already selective, and an extra index slows every write.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets MongoDB accept exactly one. The other gets `DuplicateKeyError`, which I turn into a 409. There is no read-before-write to lose. Punch-out and PATCH use the same idea: a conditional `find_one_and_update` (`punch_out: null`; for PATCH, the same fields and history length that were read). A PATCH that loses the race re-reads, recomputes its changes and retries.

3. **Ties.** I rank with `$rank` (1, 2, 2, 4) and then keep rows where `rank <= limit`. If two people tie at the cutoff, both are returned, so the list can be longer than `limit`.

4. **Headcount.** The pipeline starts from `employees` filtered by `joined_on <= month end` and left-joins logs (`preserveNullAndEmptyArrays`). Headcount is the size of the emp_code set, so people with no logs still count.

5. **At 100x.** I would precompute daily per-department rollups when a record is written, so analytics read small summaries instead of scanning raw logs. I would also shard by `emp_code`.
