# EventSphere setup

## 1. Install dependencies

From the project folder on Windows:

```cmd
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `.venv` already exists, reuse it. Commands below use its Python directly, so activation is optional.

## 2. Select a database

### Local SQLite

Leave `DATABASE_URL` unset in both the terminal environment and the project `.env` file. The default database is `eventsphere.db` beside the source files. `EVENTSPHERE_DB` can select a different SQLite file.

### Neon PostgreSQL

Create a `.env` file beside `main.py`. Add your real connection URL privately:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
```

This is a placeholder: replace it with the actual Neon URL. Copy the connection string only, without a `psql` command or masked password. Retain the SSL parameters supplied by Neon. The project's existing working configuration uses a direct connection.

SQLite records are not automatically transferred to PostgreSQL. Each database has its own records and accounts. Initialization retains existing records in the selected database.

**Use a separate Neon development/test database for experiments.** If local scripts and Railway use the same URL, they modify the same database.

## 3. Initialize and create demo accounts

```cmd
.\.venv\Scripts\python.exe database.py
.\.venv\Scripts\python.exe create_demo_users.py
```

The account script prompts for a password of at least eight characters and creates missing accounts:

- `admin@demo.test`
- `staff@demo.test`
- `attendee@demo.test`

Use a unique password. Existing accounts are not overwritten. Use `create_demo_users.py` for the database-aware setup rather than the older `demo_users.py` instructions.

To reset only one account:

```cmd
.\.venv\Scripts\python.exe reset_password.py
```

Enter the account email, then enter the new password twice. Password input is hidden. This updates only the selected account in the configured database; it does not change the database connection password or other accounts.

## 4. Run locally

```cmd
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open http://127.0.0.1:8000/. Keep the terminal running. Press Ctrl+C to stop.

For local HTTP testing, leave `HTTPS_ONLY` unset or set it to `false` in the local configuration. A secure-only session cookie will not support normal login over local HTTP. Railway should use `HTTPS_ONLY=true`.

## 5. Configuration reference

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection URL; absent means SQLite |
| `EVENTSPHERE_DB` | Optional SQLite database file path |
| `SESSION_SECRET` | Stable secret used to sign login sessions |
| `HTTPS_ONLY` | Set `true` on Railway HTTPS; local HTTP normally uses `false` |
| `EVENT_TIMEZONE` | Event timezone; default `Asia/Colombo` |
| `RAILWAY_VOLUME_MOUNT_PATH` | Railway-provided mounted storage directory; currently `/data` |
| `TEST_POSTGRES_URL` | Optional separate PostgreSQL database URL for integration testing |

For local development, the application creates `.session_secret` if `SESSION_SECRET` is absent. Never commit that file.

Generate a private deployment secret with:

```cmd
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

## 6. Railway deployment

Connect the GitHub repository and its `main` branch to the Railway service. The current service is deployed; do not create a duplicate just to follow this guide.

Configure these service variables privately:

- `DATABASE_URL`: the production Neon connection URL.
- `SESSION_SECRET`: a generated, stable random secret.
- `HTTPS_ONLY`: `true`.

Apply pending changes. The application initializes its schema during startup; demo account creation is a separate operation.

If a custom start command is needed, use this in Railway's start-command setting:

```sh
python -m uvicorn main:app --host 0.0.0.0 --port $PORT
```

`$PORT` is expanded in the hosted environment; this is not a Windows CMD command. Public networking must target the port the application actually listens on. The current generated domain targets port 8080. Check deployment logs before changing a working command or port.

Generate a public domain under the service's Settings → Networking. Use HTTPS for browser access. Pushing commits to the connected branch triggers deployment; check the latest commit is ACTIVE, not merely that an older deployment is online.

## 7. Persistent venue photos

The current service has a Railway volume mounted at `/data`. Railway supplies `RAILWAY_VOLUME_MOUNT_PATH` automatically.

The updated `main.py`:

1. Uses `<volume mount path>/uploads` on Railway, currently `/data/uploads`.
2. Uses the project's `uploads` folder locally when no mount path is provided.
3. Copies existing repository photos into persistent storage when a same-named file is absent.
4. Serves photos under the existing `/uploads/<filename>` URLs.
5. Saves newly uploaded photos into the selected upload directory.

Keep the volume attached across deployments. Do not wipe it. Copying repository photos is not a backup of future uploads. Neon stores venue records and image references, not the image files themselves.

**Pending acceptance check:** after the browser warning is resolved, upload a new photo through the hosted admin page, record its URL, redeploy the same application, then confirm that photo still loads after a fresh browser reload. A successful deployment alone does not prove persistence.

## 8. Run tests

```cmd
.\.venv\Scripts\python.exe -m pytest -q
```

Latest reported local outcome: 28 passed, 1 skipped and 1 Starlette/httpx deprecation warning. The warning is not a failed test.

The route test clears `DATABASE_URL` and uses a temporary SQLite database. The optional PostgreSQL integration test is skipped unless `TEST_POSTGRES_URL` is configured.

Use only a dedicated test database for that test. It creates and removes an isolated test schema. In a new Windows CMD terminal:

```cmd
set "TEST_POSTGRES_URL=YOUR_SEPARATE_TEST_DATABASE_URL"
.\.venv\Scripts\python.exe -m pytest tests/test_postgres.py -q
set "TEST_POSTGRES_URL="
```

Replace the placeholder privately. Do not share terminal screenshots containing the URL. Do not claim the PostgreSQL test passed until it has actually run successfully.

## 9. Browser warning status

As of 23 November 2026, Search Console reports “Possible Phishing Detected on User Login” without sample URLs. Ownership was verified using an HTML meta tag, and an incorrect-classification report was submitted through Google Safe Browsing. Removal of the warning has not been confirmed.

Inspection of the supplied `index.html` and `main.py` found same-site login submission and password hashing, with no external login-page scripts or password forwarding identified in those two files. This limited review does not establish the security of every dependency or the complete live deployment.

Keep browser protection enabled. Do not tell assessors or users to bypass the warning. Retain the verification meta tag. Avoid publishing passwords in the repository; provide assessment credentials privately.

## 10. Git and final checklist

Ensure `.gitignore` includes:

```gitignore
.env
.session_secret
.venv/
__pycache__/
.pytest_cache/
eventsphere.db
```

Check private files are not tracked:

```cmd
git ls-files .env .session_secret eventsphere.db
```

No output means those paths are not currently tracked; it does not inspect older Git history.

Before final submission, confirm:

- Browser warning clearance and public HTTPS access.
- All three roles work on the hosted application.
- New images survive redeployment.
- Booking, cancellation/refund, waitlist offers, notifications and venue hire work live.
- Staff scanning accepts a valid ticket once and rejects invalid, wrong-event, cancelled and duplicate tickets with clear reasons.
- Scan tests use the allowed check-in window: from 30 minutes before event start until event end.
- Phone display and camera/manual-entry behavior are checked on a real device.
- Test results, report claims and screenshots match the final code.
- The report includes the public URL, GitHub URL, agent design, goals/state/rules and required evidence.
- Hosting remains available for assessment within the trial or another chosen plan.
