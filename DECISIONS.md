# Design decisions

1. **Indexes.** Unique `employees.emp_code` and `attendance_logs(emp_code, date)` protect identity and one daily row. Department/code seeds trends; department/join date filters headcount; attendance date/employee serves date lists and monthly analytics. The standalone `joined_on` index serves unfiltered monthly eligibility, which lacks department equality. It costs storage and write work; MongoDB used it without a hint in the 100,000-log audit.

2. **Punch-in race.** Both requests may read the employee, then insert the same employee/date pair. The unique index accepts one (201); duplicate-key handling returns 409 for the other. Correctness does not depend on a preflight check.

3. **Ties.** `$setWindowFields` with `$rank` gives equal totals equal ranks and leaves a gap afterward. Filtering by rank after ranking returns all employees tied at the cutoff. `emp_code` stabilizes their display order.

4. **Headcount.** The summary starts with employees joined by month end, then looks up attendance. Empty lookups get zero metrics, so eligible employees without logs count and later joiners do not.

5. **100x data.** I would measure query/index costs, then consider maintained monthly department rollups. They reduce repeated lookups but require reliable updates and reconciliation after corrections or late records.
