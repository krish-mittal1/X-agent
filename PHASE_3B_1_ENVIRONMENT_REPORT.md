# Phase 3B.1 — integration-test environment report

Date: 2026-11-03 (Asia/Kolkata)

## Summary

The Windows host cannot currently create an asyncio event loop because its
internal socket-pair creation hangs. This reproduces outside the application,
outside the project virtual environment, and in a separate Python version.
No application business logic was changed.

The integration suite has **not** been certified on this host. The newsroom
pilot has not run.

## Locally verified

### Python and asyncio

| Check | Result |
| --- | --- |
| Project interpreter | `C:\Users\mitta\OneDrive\Desktop\X agent\.venv\Scripts\python.exe` |
| Project version | CPython 3.12.14, MSC v.1944, AMD64 |
| Project base distribution | `C:\Users\mitta\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none` |
| Default policy | `asyncio.windows_events.WindowsProactorEventLoopPolicy` |
| `asyncio.run(asyncio.sleep(0))` in project venv | Hangs; terminated after 10 seconds |
| Clean CPython 3.12 venv | Bootstrap reached the interpreter but `ensurepip` exited non-zero; the isolated interpreter still reproduces the same hang |
| Separate interpreter | `C:\Users\mitta\AppData\Local\Python\pythoncore-3.14-64\python.exe`, CPython 3.14.6, MSC v.1944, AMD64 |
| Same probe on CPython 3.14 | Hangs; terminated after 10 seconds |
| Process-local Selector policy retry | Still hangs creating `_make_self_pipe` |
| Relevant environment variables | No `PYTHON*`, `ASYNCIO*`, `UV_*`, or `VIRTUAL_ENV` variables were present |

The faulthandler trace places the block in:

`socket.accept -> socket._fallback_socketpair -> asyncio.*._make_self_pipe -> asyncio.new_event_loop`

It occurs before `_prepare_database()` attempts an asyncpg connection. This is
host-level Windows/Python socket-pair behavior, not a newsroom, database-schema,
or pytest-test-body failure.

The Windows `py` launcher could not be accessed from this session, so a complete
launcher-managed Python inventory was not available. Direct inspection verified
the two interpreters above.

### Test database endpoint

`localhost:55432` has no reachable listener. The suite's fixture targets the
isolated database URL `postgresql+asyncpg://techx:techx@localhost:55432/techx_test`,
not a production `x_agent` database.

No database was created, migrated, truncated, reset, or otherwise modified.

### Quality commands

| Command | Result |
| --- | --- |
| `pytest --collect-only -q` | 328 tests collected |
| `pytest -q` | Blocks at initial async integration-fixture setup; manually interrupted after 30 seconds |
| `ruff check app tests` | Pass |
| `mypy app` | Pass: 76 source files |

The first blocked node under verbose execution is:

`tests/integration/test_analytics.py::test_metrics_are_collected_and_evaluated`

It is the first test needing the session database fixture; no assertion,
background job, or outbound application request has executed.

## Blocked

| Capability | Evidence | Consequence |
| --- | --- | --- |
| WSL2 | `Wsl/E_ACCESS_DENIED` for status and distribution listing | Cannot verify or prepare Ubuntu locally |
| Docker Engine | Docker CLI installed, but `dockerDesktopLinuxEngine` pipe is absent | Cannot start isolated PostgreSQL/pgvector container |
| Test PostgreSQL | No listener at port 55432 | No connection, pgvector, migration, transaction-cleanup, or isolation verification |
| Full pytest | Async loop cannot initialise | No complete result, skipped-test count, or integration pass/fail count |

## Externally configured: reproducible Linux/CI setup

Use an Ubuntu 22.04+ runner with Python 3.12 and a disposable PostgreSQL 16
instance with pgvector. Keep `TEST_DATABASE_URL` separate from every development
or production URL.

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv postgresql-16 postgresql-16-pgvector
sudo -u postgres createuser --pwprompt techx_test_runner
sudo -u postgres createdb -O techx_test_runner techx_agent_integration_test

python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
export TEST_DATABASE_URL='postgresql+asyncpg://techx_test_runner:REDACTED@127.0.0.1:5432/techx_agent_integration_test'
export APP_ENV=test
python -m alembic upgrade head
python -m pytest -q
ruff check app tests
mypy app
```

Before running tests, verify isolation explicitly:

```bash
psql "$TEST_DATABASE_URL" -c 'SELECT current_database(), current_user;'
psql "$TEST_DATABASE_URL" -c 'CREATE EXTENSION IF NOT EXISTS vector;'
psql "$TEST_DATABASE_URL" -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"
python -m alembic current
```

The existing test fixture truncates all mapped tables with `RESTART IDENTITY
CASCADE` before each test. This is safe only for the dedicated database named in
`TEST_DATABASE_URL`; never use a production URL.

## Not tested

- PostgreSQL connection, pgvector availability, Alembic migration status,
  transaction cleanup, and integration-test isolation.
- Full pytest pass/fail and skip totals.
- Gemini generation, any other provider request, X publishing, and newsroom
  pilot processing.

## Safety status

No Gemini or X API request was sent. Local-draft configuration remains intact;
no editorial-generation behavior, tests, or production database records were
changed.
