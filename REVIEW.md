# REVIEW.md

Defects found in the starter `app/main.py`. All are fixed in the submitted `app/main.py`.

| # | Where (function) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=h, minute=m)`, i.e. in whatever zone the datetime happens to be in (naive server time), not IST. | On a UTC server a 10:05 IST punch-in (04:35Z) is compared with 09:30 UTC, so `late_minutes` is 0 instead of 35. | Shift times are built as IST-aware datetimes (`ist_datetime`) and compared with the aware UTC punch. |
| 2 | `compute_late_minutes` | `int(seconds / 60)` is floored *before* the `> 10` test. | 09:40:01 gives int(10.01) = 10, not `> 10`, so 0; the contract says 10. 09:40:30 has the same problem. | Compare whole seconds first (`delta > 600`), then floor to minutes. |
| 3 | `compute_late_minutes` | Always uses the punch-in's own calendar day as the shift day; overnight shifts are ignored. | Overnight 22:00-06:00 employee punching in at 02:00 should be 240 min late for the shift that started the previous day; starter says 0. | Shift start is taken on the attendance date from R1 (`attendance_date`). |
| 4 | `compute_work_hours` | Python `round()` on a float: banker's rounding plus binary float error. Sub-seconds not truncated. | 16218 s = 4.505 h; float 4.505 is slightly below the tie, so `round` gives 4.5, contract (half-up) says 4.51. | Integer half-up on seconds (`compute_work_hundredths`); punches truncated to whole seconds. |
| 5 | `compute_overtime` | No 30-minute minimum (R3): every positive minute counts. Also never called, so there is no punch-out at all. | Punch-out 18:35, shift end 18:30 gives 5; must be 0. | Count only when floor-minutes >= 30; added `POST /attendance/punch-out`. |
| 6 | `compute_overtime` | Shift end is always on `date_str`'s day, so an overnight shift's end is 24 h early. Mixing the aware `end` with a naive `punch_out` raises `TypeError`. | Overnight shift out at 06:40 next day gives about 1480 min of overtime, or a 500. | End is built on date+1 for overnight shifts; every instant is timezone-aware UTC. |
| 7 | `EmployeeIn` | No validation at all. | `emp_code` "abc", `shift_start` "9am", `joined_on` "2026-13-45", equal shift times, empty name all return 201; "9am" then crashes punch-in with a 500. | Patterns, lengths, real-date check and `shift_start != shift_end`, giving 422. |
| 8 | `create_employee` | Check-then-insert race and no unique index. | Two parallel POSTs with the same `emp_code` both pass `find_one` and both insert. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time, and the response carries a datetime instead of epoch milliseconds. | Response `created_at` is an ISO string; value shifts with server zone. | UTC, millisecond precision, returned as epoch ms. |
| 10 | `list_employees` | `skip = page * page_size` (should be `(page-1) * ...`); `page=0` or negative gives a negative skip, so a 500. | `GET /employees?page=1` returns the second page; `page=-1` crashes. | `(page-1) * page_size`; `page >= 1`, `1 <= page_size <= 100` validated (422). |
| 11 | `list_employees` | `total` counts the whole collection, not the filtered set; results are unsorted. | `?department=Sales` reports the total of everyone; order is arbitrary. | Count with the same filter; sort by `emp_code`. |
| 12 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError`. | POST with an unknown `emp_code` returns 500. | 404. |
| 13 | `punch_in` | `datetime.fromtimestamp(ms/1000)` is naive server-local time; the date is the server's date, not IST (R1); no truncation to seconds; `if body.punched_at` treats 0 as missing; lax `int` accepts `"123"` and `1.0`. | Punch at 00:30 IST on a UTC server lands on the previous date; seconds-style or string timestamps are accepted. | `ms_to_dt` (UTC, truncated), `attendance_date` (IST, overnight aware), strict bounded `EpochMs` type (422). |
| 14 | `punch_in` | `status` is a free string. | `status: "ABSENT"` or `"banana"` creates a record with punch times. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 15 | `punch_in` | Check-then-insert race, no unique index on `(emp_code, date)`. | Two simultaneous punch-ins both return 201 and create two records. | Unique index; `DuplicateKeyError` becomes 409. |
| 16 | `punch_in` | Response leaks `id` (ObjectId string) and returns raw datetimes. | Body has an `id` field and ISO strings for `punch_in`. | `serialize_record`: no ids, epoch ms. |
| 17 | `list_attendance` | Loads every matching document into memory, sorts in Python, slices in Python. Sort has no `emp_code` tiebreak. No indexes. | Slow and memory-hungry at 100k records; ties come back in arbitrary order; `page=0` slices from the end. | Filter, sort `(date desc, emp_code asc)`, skip and limit done by MongoDB with matching indexes; `page`/`page_size` validated. |
| 18 | `list_attendance` | No validation of dates or `status`; `date_from > date_to` not rejected; legacy documents (no `half_day` / `history`) and raw `_id` / datetimes leak straight out. | `date_from=banana` silently matches nothing; missing `half_day` key in output. | Strict date parsing (422), status enum, `serialize_record` fills defaults. |
| 19 | `health` | Always returns `ok`. | Stop MongoDB and `/health` is still 200. | Pings MongoDB, 503 on failure. |
| 20 | module setup | `MongoClient()` returns naive datetimes; no indexes are created at startup. | Subtracting naive and aware datetimes raises `TypeError`; every query is a collection scan. | `tz_aware=True`; `ensure_indexes()` at startup, idempotent. |

## Looked at and judged not a defect

- `IST = timezone(timedelta(hours=5, minutes=30))` is correct (India has no DST).
- `load_dotenv()` does not override variables already in the environment, which is what the contract asks for.
- `doc.pop("_id")` after `insert_one` is needed (PyMongo adds `_id` to the dict) and correct.
- The initial punch-in document (`overtime_minutes` 0, `half_day` False, `history` []) matches the data model.
- `{"_id": 0}` projection in `list_employees` is correct.
