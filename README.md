# Backend database and deployment

## Local development

Without `DATABASE_URL`, the backend uses `backend\order_manager.db` for local
development. It does not create or migrate the database when `main.py` is
imported or when a Vercel function starts.

From PowerShell:

```powershell
Set-Location .\backend
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe init_db.py
.\venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

`init_db.py` is an explicit, repeatable schema initializer. It adds missing
schema objects and default products without deleting or resetting existing
records.

## Configure PostgreSQL and Vercel

1. Create a PostgreSQL database with a managed provider. For serverless use,
   prefer the provider's pooled connection URL when available.
2. In the Vercel project settings, add `DATABASE_URL` for every environment
   that will run the backend (at least Production). Use the provider's
   PostgreSQL URL as the value; do not commit it or put it in frontend
   variables. The backend accepts `postgres://` and `postgresql://` URLs and
   selects the psycopg driver.
3. Before deploying, initialize a **new, empty** PostgreSQL database from the
   backend folder:

   ```powershell
   $env:DATABASE_URL = "<paste the provider PostgreSQL URL locally>"
   .\venv\Scripts\python.exe init_db.py
   ```

   This is a one-time/manual schema operation, not a Vercel startup hook.
   Repeat it deliberately when deploying schema changes. Do not configure
   `DATABASE_URL` in the frontend.
4. Redeploy the existing Vercel project without changing its current Python
   entrypoint/root-directory settings. Its runtime already imports
   `main.py`; no `vercel.json` is required for that existing layout.
5. Check `/docs`, `/openapi.json`, and `/health` on the deployed URL.
   `/health` should return HTTP 200 with `"status": "healthy"` and
   `"database": "connected"`. It returns HTTP 503 if the database or required
   tables are unavailable.

If `DATABASE_URL` is missing on Vercel, startup fails with a configuration
error instead of silently creating a SQLite database on Vercel's ephemeral,
read-only filesystem. SQLite remains a local-development option only.

## Migrate the existing SQLite database

The migration reads the source SQLite file in read-only mode, refuses to
import into PostgreSQL tables that already contain data, copies the
shopkeepers, products, and orders while preserving IDs, and advances the
PostgreSQL ID sequences. It does not delete or modify the SQLite source.

1. Back up `backend\order_manager.db` and stop local/backend writes for the
   duration of the migration.
2. Set `DATABASE_URL` to a new, empty PostgreSQL database. **Do not run
   `init_db.py` first**; the migration script creates the schema without
   seeding rows, then imports the existing data.
3. From the backend folder, run:

   ```powershell
   $env:DATABASE_URL = "<paste the provider PostgreSQL URL locally>"
   .\venv\Scripts\python.exe migrate_sqlite_to_postgres.py
   ```

   To select a different SQLite source, add
   `--sqlite-path .\path\to\source.db`.
4. Confirm the reported counts, configure the same URL in Vercel, and redeploy.
   Keep the SQLite backup until the PostgreSQL deployment has been verified.

The migration intentionally stops if any destination table is non-empty. If a
run is interrupted, inspect the PostgreSQL target before retrying; do not clear
or reset it automatically.

## Verify the deployment

After setting `DATABASE_URL`, redeploy and check:

```text
GET https://<deployment>/docs
GET https://<deployment>/openapi.json
GET https://<deployment>/health
```

Then exercise the existing `/shopkeeper/register`, `/shopkeeper/login`,
`/products`, `/order/create`, and `/orders` routes with test data. Do not use a
real customer order as the first deployment check.

## Ring AI integration still needs production setup

Database deployment does not complete the voice integration. Before accepting
real incoming calls, configure and test the Ring AI credentials and workflow,
use the deployed HTTPS URL for the provider callback, and add webhook
authentication/signature validation and event-to-order processing. The current
`/voice/webhook` route only acknowledges and returns its JSON body; it does not
create an order. The existing outbound Ring AI request code also contains a
placeholder authorization header that must be connected to
`RINGAI_API_KEY` before outbound calls can work.
