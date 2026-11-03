# Linux integration environment

## Architecture

The GitHub Actions workflow at `.github/workflows/linux-integration.yml` runs on
`ubuntu-latest` with Python 3.12 and an ephemeral PostgreSQL 16 + pgvector
service container (`pgvector/pgvector:0.8.7-pg16-bookworm`). No volume is
configured, so GitHub disposes the database after the job.

The workflow uses two separate local-only databases:

| Database | Purpose |
| --- | --- |
| `x_agent_test` | pytest fixture reset, migrations, and integration tests |
| `x_agent_migration_test` | independent Alembic upgrade validation |

The container user/password are CI-only literals. They are not application,
provider, X, or production credentials.

## Configuration paths

- Application and Alembic read `DATABASE_URL`: `Settings` is the source for
  `migrations/env.py`.
- The test fixture reads `TEST_DATABASE_URL`, then passes it as `DATABASE_URL`
  to its Alembic subprocess.
- `APP_ENV=test` is set for the job and migration validation.
- The fixture's `Settings` also forces `llm_provider="disabled"`, hashing
  embeddings, `dry_run=True`, and `publishing_enabled=False`.

## Database safety

The test fixture recreates `public` and truncates every mapped table. Before it
does either operation, it now accepts only `postgresql+asyncpg` URLs targeting
loopback (`localhost`, `127.0.0.1`, or `::1`) and exactly one of the approved
disposable names: `techx_test` for existing local development or `x_agent_test`
for CI. It rejects `techx`, `x_agent`, remote hosts, other database names, and
other drivers.

Do not point `TEST_DATABASE_URL` at any development, staging, or production
database. The migration database is never used by the destructive pytest
fixture.

## Commands run in CI

1. Verify `asyncio.run(asyncio.sleep(0))` on Linux.
2. Create `x_agent_migration_test`, enable `vector` in both disposable DBs, and
   verify the test DB extension.
3. Run Alembic against `x_agent_migration_test` and print its current revision.
4. Run the previously blocked analytics integration test, unit tests,
   integration tests, the full pytest suite, Ruff, and Mypy.

The job reports failures and skips through ordinary failing command exit codes;
it does not convert skips into passes or suppress results.

## Safety controls

The workflow sets only local-test controls relevant to runtime safety:

- `X_PUBLISHING_MODE=local_draft`
- `PUBLISHING_ENABLED=false`
- `DRY_RUN=true`
- `APP_ENV=test`

It supplies no Gemini credential, X write credential, X session cookie, Brave
CDP credential, or remote endpoint. With no provider credentials and with
local-draft mode, the workflow must not make real model or X publishing calls.

## Running on another Linux runner

Use the same two database names and loopback URLs. Install dependencies only
from the declared files:

```bash
python -m pip install -r requirements-dev.txt
export APP_ENV=test
export DATABASE_URL='postgresql+asyncpg://x_agent_ci:ci-only-password@127.0.0.1:5432/x_agent_test'
export TEST_DATABASE_URL="$DATABASE_URL"
export X_PUBLISHING_MODE=local_draft
export PUBLISHING_ENABLED=false
export DRY_RUN=true
```

Create the migration database and enable `vector` in both databases before
running Alembic or pytest, as shown in the workflow.
