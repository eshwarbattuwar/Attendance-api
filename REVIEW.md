# REVIEW.md

## Defects found in the starter

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `health()` | Returns `ok` without contacting MongoDB, so a broken database connection still passes readiness. | Stop MongoDB and call `/health`; starter still returns 200. | Ping MongoDB and return 503 on a database error. |
| 2 | `create_employee()` | Checks for duplicates before insert, which is race-prone; there is no database uniqueness guarantee. | Send simultaneous creates with the same `emp_code`. | Create a unique `emp_code` index and translate `DuplicateKeyError` to 409. |
| 3 | `create_employee()` | Stores a naive local `datetime.now()` instead of an explicit UTC instant and returns it without epoch-ms conversion. | Compare `created_at` with the contract or run in a non-UTC environment. | Store UTC-aware server time and serialize instants to integer epoch milliseconds. |
| 4 | `EmployeeIn` | Does not enforce the documented employee-code, email, length, date, shift-time, or unequal-shift constraints. | Submit malformed codes, dates, shift strings, or identical shift start/end. | Add Pydantic field and model validation. |
| 5 | `list_employees()` | Uses `page * page_size` as the skip, so page 1 skips the first page; `total` ignores the department filter. | Request page 1 with a nonempty collection or filter by department. | Use `(page - 1) * page_size` and count the filtered query. |
| 6 | `compute_late_minutes()` | Compares a naive timestamp against a same-date shift start and does not correctly account for IST, overnight shifts, or whole-second truncation. | Test late punches and overnight shifts near midnight. | Normalize timestamps to UTC, calculate shift instants in IST, apply the strict 10-minute grace rule, and truncate seconds. |
| 7 | `compute_work_hours()` | Python `round()` uses ties-to-even and does not guarantee the required half-up rounding. | Use a duration exactly on a two-decimal rounding tie. | Use `Decimal` with `ROUND_HALF_UP`. |
| 8 | `compute_overtime()` | Always puts shift end on the attendance date, mishandles overnight shifts, and counts overtime even below the 30-minute threshold. | Punch out after an overnight shift or 29 minutes after shift end. | Compute the correct next-day shift end and only count overtime at 30 whole minutes or more. |
| 9 | `punch_in()` | Does not return 404 for an unknown employee before dereferencing `emp`; uses local time and treats zero-like timestamps as omitted. | Punch for an unknown employee or provide a boundary timestamp. | Validate employee existence, distinguish `None` from a supplied timestamp, normalize UTC/IST correctly, and return contract status codes. |
| 10 | `punch_in()` | Check-then-insert permits duplicate records under concurrency; response leaks an `id` derived from MongoDB `_id`. | Send parallel punch-ins for one employee/date or inspect response fields. | Enforce a unique `(emp_code, date)` index, handle duplicate-key conflicts as 409, and never expose `_id`. |
| 11 | `list_attendance()` | Loads all matching documents into memory, sorts in Python, paginates afterward, and does not validate date range ordering. It also sorts only by date and leaks Mongo IDs. | Use a large dataset, `date_from > date_to`, same-date records, or inspect response. | Count filtered documents, sort/page in MongoDB, validate dates, sort by date descending then code ascending, and serialize only contract fields. |
| 12 | Application structure | Missing punch-out, regularization, analytics, and explain endpoints. | Call any documented endpoint not present in the starter. | Implement every endpoint in `openapi.yaml`. |
| 13 | Analytics/data model | No startup indexes or index-backed explain operation; seeded documents may omit `history`, `half_day`, or derived defaults. | Run against seeded data and inspect query plans. | Add idempotent indexes, use safe defaults for optional stored fields, and implement explain output. |

## Reviewed and not treated as defects

- `emp_code` is used as the business identifier; MongoDB `_id` is internal and is not a valid API identifier.
- `date`, `joined_on`, `shift_start`, and `shift_end` remain strings because they represent calendar dates or local clock times, not UTC instants.
- The API does not create `ABSENT` or `LEAVE` records through punch-in; those statuses belong to seeded/back-office data or manual corrections.
