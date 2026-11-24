# EventSphere

IT4123 Agent Based Computing Practical — ICA 02  
University of Vavuniya

EventSphere is a browser-based university event and venue booking project. Admins manage venues and events, attendees book tickets and request venue hire, and gate staff validate QR tickets. Payments are simulated; no bank account, card or real payment gateway is used.

## Project links

- Source code: https://github.com/Vinoditha19-code/EventSphere_2021ICT73
- Deployment address: https://eventsphere2021ict73-production.up.railway.app/

**Deployment status — 23 November 2026:** Railway reports the persistent-photo-storage deployment as active. Google Chrome has flagged the login address as potentially deceptive. Site ownership was verified in Search Console, and an incorrect-classification report was submitted to Google Safe Browsing. Clearance has not been confirmed. Keep browser protection enabled; public acceptance testing is pending.

## Features

- Registration, hashed passwords, signed login sessions and three user roles.
- Admin venue creation and editing, including photos, capacity and hourly hire rates.
- Draft events, publishing, cancellation and ticket categories.
- Event search, category filters, ticket booking and booking history.
- Simulated payment references and cancellation refunds under the agent rules.
- Unique QR tickets and staff check-in with duplicate-admission prevention.
- Waitlists, timed ticket offers and persistent in-app notifications.
- Private venue hire requests, availability checks, alternative time proposals and admin review.
- Admin sales and check-in summaries, agent messages and saved agent state.

## Cooperating software agents

| Agent | Main responsibility |
|---|---|
| VenueAgent | Venue details, event scheduling, availability and private hire decisions |
| BookingAgent | Ticket allocation, simulated payments and cancellation refunds |
| TicketAgent | Unique ticket issuance and check-in validation |
| WaitlistAgent | Waiting order, timed offers and expiry handling |
| NotificationAgent | Persistent in-app notifications without duplicates |

Agents exchange JSON messages through `message_bus.py`. Conversations use REQUEST, AGREE, REFUSE, PROPOSE, INFORM and FAILURE messages with conversation identifiers. The database stores agent state and message records. A background task checks waitlists approximately every 15 seconds while the application is running.

## Technology

Python, FastAPI, Pydantic, HTML/CSS/JavaScript, pwdlib, QR codes and pytest. Local development supports SQLite; the current hosted configuration uses Neon PostgreSQL. Railway runs the application and provides a mounted volume for venue photos.

## Run locally on Windows

Use Python 3.11 or later. Open a terminal in the project folder:

```cmd
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Choose the database before continuing. With no `DATABASE_URL`, the application uses local SQLite. A `DATABASE_URL` in the project `.env` file selects PostgreSQL instead. Using the production Neon URL locally changes the same records as the hosted application; use a separate development database for experiments.

```cmd
.\.venv\Scripts\python.exe database.py
.\.venv\Scripts\python.exe create_demo_users.py
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open http://127.0.0.1:8000/ after startup completes. See [SETUP.md](SETUP.md) for environment configuration and deployment instructions.

## Demo accounts

| Role | Email |
|---|---|
| Admin | `admin@demo.test` |
| Gate staff | `staff@demo.test` |
| Attendee | `attendee@demo.test` |

`create_demo_users.py` asks for a password for newly created demo accounts. Existing accounts are retained; rerunning the script does not reset their passwords. Share assessment credentials privately with the assessor, not in this repository.

To reset one account in the currently configured database:

```cmd
.\.venv\Scripts\python.exe reset_password.py
```

## Tests and evidence

```cmd
.\.venv\Scripts\python.exe -m pytest -q
```

Latest reported local result: **28 passed, 1 skipped, 1 deprecation warning**. This is not evidence that every hosted workflow has passed. The PostgreSQL integration test is optional and requires `TEST_POSTGRES_URL` pointing to a dedicated test database; see SETUP.md.

Manual local checks using Neon confirmed admin and attendee login, venue creation with a photo, event creation and publishing, a confirmed booking, a displayed QR ticket, dashboard sales figures and persistent agent activity.


