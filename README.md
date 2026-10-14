# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). MongoDB 6.0+ is required (`$setWindowFields`, `$densify`).

## Status
Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`. Nothing is knowingly missing.

## Interpretations worth knowing
- `moving_avg_7d` averages the *reported* (4-decimal rounded) `attendance_rate` values.
- A PATCH is "a request that changes nothing" (422) when status, `punch_in` and `punch_out` all equal the stored values; derived fields are then not re-checked.
- A PATCH on an `ABSENT`/`LEAVE` record that supplies a punch time without also changing the status to a presence status is a 422.
- `present_count` / `present_days` read the stored `half_day`, so an open record (no punch-out) counts as a full day.
- The trend passes the department's employee codes into the pipeline (`$in`), which is simple but would need rethinking for very large departments.
