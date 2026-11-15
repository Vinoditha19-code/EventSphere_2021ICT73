# EventSphere

IT4123 Agent Based Computing Practical — ICA 02

## Run locally on Windows

Open a terminal in the project folder and run:

```cmd
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "from database import create_tables; create_tables()"
.\.venv\Scripts\python.exe demo_users.py
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Run `demo_users.py` once on a new database. It asks you to choose passwords
for the admin, staff, and attendee accounts.

Open http://127.0.0.1:8000 after the server starts.

## Demo accounts

- Admin: `admin@demo.test`
- Staff: `staff@demo.test`
- Attendee: `attendee@demo.test`

## Environment variables

The current local SQLite setup does not require any environment variables.