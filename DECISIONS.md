1. **Indexes.** A unique `employees.emp_code` index protects employee identity. A unique `attendance_logs(emp_code, date)` index enforces one daily record per employee. Compound indexes support department filtering, attendance history, date/status queries, punch-in lookup, lateness queries, and joined-date headcount. I did not add a separate status-only index because the existing status/date index starts with status; redundant indexes cost storage and slow writes.

2. **Punch-in race.** Concurrent requests may both pass validation, but the unique employee/date index allows only one insert. The winner returns 201; the other raises `DuplicateKeyError`, mapped to 409. The database constraint provides the guarantee.

3. **Ties.** MongoDB `$rank` gives tied totals the same rank and skips the next rank (1, 2, 2, 4). The pipeline filters by rank after ranking, then sorts ties by employee code. A tie at the cutoff can return more rows than `limit`.

4. **Headcount.** Department summary starts from employees who joined by period end, then looks up logs. Employees with no logs still count.

5. **100x data.** I would benchmark query plans, pre-aggregate expensive daily/monthly reports, and tune indexes from measured workloads before considering sharding or a larger database tier.