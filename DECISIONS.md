# DECISIONS.md

1. **Indexes.** On `attendance_logs` I made four. A unique one on (emp_code, date), so an employee can have only one record per day, and it also makes one-employee lookups fast. One on (date desc, emp_code), which matches the sort of the attendance list and helps month queries. One on (status, date desc, emp_code) for when the list is filtered by status. One on (emp_code, punch_in desc), to find the open record when someone punches out. On `employees`: unique emp_code, (department, emp_code), joined_on, and (department, joined_on) for headcount. I thought about one big index that also holds late_minutes and work_hours, but I skipped it. It makes writes slower, and one month of data is small anyway.

2. **Punch-in race.** Both requests try to insert the same (emp_code, date). The unique index lets only one insert go through. The other one gets a duplicate key error and I return 409. I never check first and then insert.

3. **Ties.** I rank first (using $rank) and apply the limit after that. So if two people are tied at the cutoff, both are returned. The list can be longer than limit.

4. **Headcount.** The summary starts from the employees collection, not from the logs. I join each person's logs, and people with no logs are kept, so they still count as 1.

5. **100x data.** I would store a monthly summary per employee and update it on every write, so analytics don't scan raw logs each time.