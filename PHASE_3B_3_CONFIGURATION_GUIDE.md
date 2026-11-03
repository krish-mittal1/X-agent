# Phase 3B.3 — controlled newsroom pilot configuration

## Status: READY FOR OPERATOR CONFIGURATION

This guide prepares a separate process and a separate PostgreSQL database. It
does not alter `.env`, execute the pilot, generate content, call Gemini, call X,
or publish a post.

The commands use PowerShell. Replace only the placeholders in angle brackets.
Keep credentials out of shell history where possible; PowerShell will prompt for
database passwords if `psql` is configured to do so.

## Pilot-only configuration contract

The pilot runner is [scripts/run_newsroom_pilot.py](scripts/run_newsroom_pilot.py).

When using the lightweight disposable Docker image for a controlled pilot, pass
`EMBEDDING_PROVIDER=hashing`. The image does not include the optional local
sentence-transformers runtime; hashing keeps duplicate-detection embeddings
available without downloading a model or calling an external embedding service.
It has no safe defaults that can begin real generation.

For a documented transient provider failure, the runner also supports a strict,
operator-visible retry subset. It requires `--allow-partial-retry`; without it,
the original exact-ten-category manifest requirement remains enforced.

### Required command-line arguments

| Argument | Required | Expected value | Default | Purpose |
| --- | --- | --- | --- | --- |
| `--manifest` | Yes | Path to a JSON file containing exactly ten real `source_document_id` values, one for each required category | none | Selects the already-ingested source documents. |
| `--report` | Yes | Output JSON path, e.g. `pilot_results.json` | none | Receives only observed pilot results. |
| `--account-handle` | Yes | Existing account handle in `x_agent_pilot` | none | Selects the pilot account in the isolated database. |

The manifest categories are exactly: `indian_politics`, `state_governance`,
`indian_economy`, `indian_business`, `geopolitics`, `defence`,
`technology_science`, `infrastructure`, `sports`, and `public_statement`.
Use `newsroom_pilot_candidates.template.json` as the starting shape, but do not
use its placeholder zero IDs.

### Mandatory pilot environment variables

| Name | Required value / format | Default behavior | Why it is required |
| --- | --- | --- | --- |
| `PILOT_OPERATOR_AUTHORIZATION` | Exactly `AUTHORIZED_FREE_TIER_PILOT` | absent: runner refuses | Explicit operator approval for a real request. |
| `PILOT_FREE_TIER_ONLY` | `true` (case-insensitive) | absent/other: runner refuses | Records free-tier-only intent. |
| `PILOT_MAX_COST_USD` | `0`, `0.0`, or `0.00` only | absent/other: runner refuses | Declares a zero-dollar pilot ceiling. |
| `PILOT_MAX_REQUESTS` | Integer from `1` to `100` | absent/out of range: runner refuses | Hard cap on actual provider chat attempts, including repair attempts. |
| `PILOT_ALLOWED_MODELS` | Comma-separated allowlist containing the exact configured Gemini model | absent/mismatch: runner refuses | Prevents an unintended model change. |
| `PILOT_DATABASE_URL` | `postgresql+asyncpg://x_agent_pilot_app:<password>@<host>:<port>/x_agent_pilot` | absent/wrong DB: runner refuses | The only database accepted by the runner. |

### Mandatory application safety values for the pilot process

These names are existing `Settings` fields; set them as **process environment
variables**, not in the normal `.env` file.

| Name | Required value | Config default | Effect |
| --- | --- | --- | --- |
| `SCHEDULER_ENABLED` | `false` | `false` | Prevents `create_app()` from calling `start_scheduler()`. |
| `X_PUBLISHING_MODE` | `local_draft` | `local_draft` | Selects `LocalDraftProvider`, never Brave CDP or the official X provider. |
| `PUBLISHING_ENABLED` | `false` | `false` | The `publishing` job exits before selecting a draft. |
| `DRY_RUN` | `true` | `true` | Keeps the provider factory in local staging mode. |
| `X_API_ENABLED` | `false` | `false` | Removes official X API capability. |
| `X_READS_ENABLED` | `false` | `false` | Removes X read capability. |
| `X_RESEARCH_ENABLED` | `false` | `false` | Removes X research capability. |
| `ENABLE_REPLY_SCANNER` | `false` | `false` | Prevents automated mention scanning. |
| `AI_REPLY_PUBLISHING_ENABLED` | `false` | `false` | Prevents automated reply delivery. |
| `LLM_PROVIDER` | `gemini` | `gemini` | Identifies the selected provider; the runner separately requires a Gemini key and model. |
| `GEMINI_MODEL` | Exact operator-approved model ID | `gemini-3.5-flash-lite` | Must also appear in `PILOT_ALLOWED_MODELS`. |
| `GEMINI_API_KEY` | Existing secret supplied through the operator’s secure environment | unset disables normal provider construction | Required by the runner but never printed or written to the report. |

`GEMINI_API_KEY`, `GEMINI_MODEL`, and the normal `DATABASE_URL` may be read from
the existing environment or `.env`; do not copy secrets into this guide. The
runner uses `PILOT_DATABASE_URL` for all pilot database access, and refuses it
if it points to any database except exactly `x_agent_pilot` or equals the normal
`DATABASE_URL`.

## Scheduler isolation

`SCHEDULER_ENABLED=false` is the exact scheduler kill switch. In `app/main.py`,
APScheduler starts only when this setting is true. With it false,
`app.jobs.scheduler.start_scheduler()` is never called and its recurring RSS,
ranking, drafting, publishing, reply, memory, and analytics jobs are not
registered.

Set the pilot process environment before any command:

```powershell
$env:SCHEDULER_ENABLED = "false"
$env:X_PUBLISHING_MODE = "local_draft"
$env:PUBLISHING_ENABLED = "false"
$env:DRY_RUN = "true"
$env:X_API_ENABLED = "false"
$env:X_READS_ENABLED = "false"
$env:X_RESEARCH_ENABLED = "false"
$env:ENABLE_REPLY_SCANNER = "false"
$env:AI_REPLY_PUBLISHING_ENABLED = "false"
```

The pilot runner is a direct script, not a FastAPI/uvicorn process. It does not
import or call APScheduler. Its configuration check independently refuses to
start if `SCHEDULER_ENABLED` is true.

For the Docker-backed local pilot database prepared on this workstation, use the
non-persistent activation helper in the same PowerShell session as preflight:

```powershell
.\scripts\set_pilot_environment.ps1 -PilotOperatorAuthorization AUTHORIZED_FREE_TIER_PILOT -PilotMaxRequests <approved-request-cap> -PilotAllowedModel <approved-model-id> -PilotDbPassword <x_agent_pilot_app-password>
```

It changes process environment variables only and never writes a credential to
`.env` or disk.

The read-only preflight below is the executable verification: a successful JSON
result contains `"scheduler_enabled": false` and
`"external_publishing_capability": false`. It does not start the application,
register jobs, create drafts, or make external provider calls.

## Create the isolated PostgreSQL database

These are **operator instructions only**. Run them from an administrative
PostgreSQL session after confirming the target host is the intended pilot host.
They contain no `DROP`, `TRUNCATE`, or command against `x_agent` or `techx`.
The pgvector extension must already be installed on the PostgreSQL server.

```sql
-- Connect to the PostgreSQL maintenance database, not x_agent or techx.
CREATE ROLE x_agent_pilot_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '<migrator-password>';
CREATE ROLE x_agent_pilot_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '<application-password>';

CREATE DATABASE x_agent_pilot OWNER x_agent_pilot_migrator;
\connect x_agent_pilot

CREATE EXTENSION vector;
GRANT CONNECT ON DATABASE x_agent_pilot TO x_agent_pilot_migrator, x_agent_pilot_app;
GRANT USAGE, CREATE ON SCHEMA public TO x_agent_pilot_migrator;
GRANT USAGE ON SCHEMA public TO x_agent_pilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE x_agent_pilot_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO x_agent_pilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE x_agent_pilot_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO x_agent_pilot_app;
```

Before migrations, verify the administrative session is on the new database:

```sql
SELECT current_database(), current_user, inet_server_addr(), inet_server_port();
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

Run the migrations using only the migrator connection in a clean PowerShell
process. This is the only instructed write operation, and it targets the new
database only:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://x_agent_pilot_migrator:<migrator-password>@<host>:<port>/x_agent_pilot"
python -m alembic upgrade head
python -m alembic current
```

The expected current revision is `7922d051fd24`. After migration, connect as
`x_agent_pilot_app` and verify it is a non-superuser. The preflight verifies the
database name, current user, server host and port, pgvector, and this exact
revision before allowing a pilot.

## Gemini free-tier authorization

Before setting `PILOT_OPERATOR_AUTHORIZATION`, the operator must inspect the
key’s project in Google AI Studio and confirm all of the following:

1. The API key belongs to the intended project and selected model is available
   to that project’s free tier.
2. The project/key has not been moved to a paid billing tier for this work.
3. Current quota/rate-limit information in AI Studio supports the chosen request
   cap.
4. The operator accepts that free-tier availability varies by model, region,
   project, and usage tier.

Google’s current [billing documentation](https://ai.google.dev/gemini-api/docs/billing/)
states that free-tier access is model-limited and that setting up a billing
account upgrades access to paid tiers. Its [pricing page](https://ai.google.dev/gemini-api/docs/pricing)
and [rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits)
are the authoritative sources for the selected model’s current eligibility and
limits. Do not infer eligibility from `LLM_INPUT_PRICE_PER_MTOK=0` or
`LLM_OUTPUT_PRICE_PER_MTOK=0`; those fields only determine the application’s
estimate.

The runner enforces an explicit acknowledgment, exact `$0.00` declared ceiling,
model allowlist, and per-chat-attempt cap. It forces zero retries. Token counts,
latency, model, success/failure, and estimated cost are stored in
`model_usage_records` through `LLMService`. Actual Google billing cost is not
observable from an API response and is reported as such. Any authentication,
unauthorized, quota, billing, or rate-limit error immediately stops the pilot.

## Read-only pre-pilot safety check

With all pilot values set in the current PowerShell process, run this single
command. It performs only database `SELECT` queries; it does not create drafts,
run migrations, validate the Gemini model over the network, call Gemini, or
instantiate a publisher.

```powershell
$env:PILOT_OPERATOR_AUTHORIZATION = "AUTHORIZED_FREE_TIER_PILOT"; $env:PILOT_FREE_TIER_ONLY = "true"; $env:PILOT_MAX_COST_USD = "0.00"; $env:PILOT_MAX_REQUESTS = "<approved-request-cap>"; $env:PILOT_ALLOWED_MODELS = "<approved-model-id>"; $env:PILOT_DATABASE_URL = "postgresql+asyncpg://x_agent_pilot_app:<application-password>@<host>:<port>/x_agent_pilot"; python scripts/pilot_preflight.py
```

The command will fail closed unless every safety switch is correct, the database
is precisely `x_agent_pilot`, pgvector is present, the migration is at
`7922d051fd24`, and the runtime role is not a superuser.

## Operator stop procedure

Do not launch the pilot until separately authorized. If it is running, press
`Ctrl+C`; the script catches no signal by design and will stop before starting a
new candidate. To prevent a subsequent launch in the same PowerShell session:

```powershell
Remove-Item Env:PILOT_OPERATOR_AUTHORIZATION -ErrorAction SilentlyContinue
$env:SCHEDULER_ENABLED = "false"
$env:PUBLISHING_ENABLED = "false"
$env:DRY_RUN = "true"
```

The provider cap also stops execution before a request beyond
`PILOT_MAX_REQUESTS`, and the runner stops immediately on provider safety
errors. Neither path sends an X request: local-draft mode remains mandatory.
