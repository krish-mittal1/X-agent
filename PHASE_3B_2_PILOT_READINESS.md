# Phase 3B.2 — India Pulse newsroom pilot readiness

## Status: BLOCKED — CONFIGURATION REQUIRED

No newsroom stories were generated and no Gemini request, X request, or browser
publishing action was made during this readiness work.

## CI certification

The GitHub CLI is installed locally but could not reach `api.github.com` during
this check. The GitHub Actions job log was retrieved through the connected
repository integration instead.

| Item | Result |
| --- | --- |
| Repository / branch | `krish-mittal1/X-agent` / `main` |
| Workflow run | [41674254147](https://github.com/krish-mittal1/X-agent/actions/runs/41674254147) |
| Commit | `dc909fd6dd48c1b11f6eacd9fc1a714368e602c2` |
| Full pytest | **333 passed, 1 skipped in 19.31s** |
| Unit suite | 214 passed in 2.84s |
| Integration suite | 95 passed in 19.71s |
| Analytics integration | 1 passed in 1.32s |
| Ruff | `All checks passed!` |
| Mypy | `Success: no issues found in 76 source files` |

The log does not identify the skipped node, so this report intentionally does
not guess which test was skipped. The full suite had no failures.

## Local safety preflight

The local configuration was inspected without printing `.env` values or
credentials. The effective stored settings are:

| Safety control | Observed value | Assessment |
| --- | --- | --- |
| `X_PUBLISHING_MODE` | `local_draft` | safe |
| `PUBLISHING_ENABLED` | `false` | safe |
| `DRY_RUN` | `true` | safe |
| `SCHEDULER_ENABLED` | `true` | **not acceptable for a controlled pilot** |
| Gemini key | configured (value not inspected) | requires operator authorization |
| Gemini model | `gemini-3.5-flash-lite` | must be explicitly allowlisted |
| Database | configured (name/connection not reported) | must be replaced with a verified pilot database |

Draft creation does not invoke `Publisher`. The scheduled publishing task exits
before selecting drafts when `PUBLISHING_ENABLED=false`, and also exits in
`local_draft` mode. `get_publishing_provider` selects `LocalDraftProvider` for
local-draft, disabled, or dry-run configuration; that provider only stages an
approved draft in the local review queue and has no outbound request path.
The `Publisher` also routes zero-cost/local-draft calls to that provider before
any X client use. No browser-based X publisher is selected under the observed
settings. ClaimVerifier and PolicyGate remain in place; this work did not alter
either.

The operator must run the pilot against a **separate, migrated** PostgreSQL
database whose name begins `x_agent_pilot`. The runner refuses an identical
database URL or a database name outside that prefix. This protects the
configured local/production database from pilot writes; it does not modify or
reset either database.

## Gemini safeguards

`app/providers/gemini_provider.py` records provider response token counts,
latency, model ID, success/failure, and the application’s configured cost
estimate in `model_usage_records`. Gemini billing/free-tier eligibility is not
observable from a configured zero price, and has not been assumed here.

The new `scripts/run_newsroom_pilot.py` fails closed unless all of the following
are supplied by the operator at execution time:

- `PILOT_OPERATOR_AUTHORIZATION=AUTHORIZED_FREE_TIER_PILOT`
- `PILOT_FREE_TIER_ONLY=true`
- `PILOT_MAX_COST_USD=0.00`
- `PILOT_MAX_REQUESTS` set to an integer between 1 and 100
- `PILOT_ALLOWED_MODELS` containing the configured Gemini model
- `PILOT_DATABASE_URL` naming a separate `x_agent_pilot...` database

It also requires `SCHEDULER_ENABLED=false`, forces Gemini retry count to zero,
counts provider attempts against the operator cap, and stops on authentication,
quota/rate-limit, billing, transport, or other provider errors. Actual billing
cost remains reported as “not observable” unless Google returns it; the JSON
report contains only observed request counts and application-recorded token/cost
data.

## Ten-story pilot support

`newsroom_pilot_candidates.template.json` provides exactly one slot for each
required category: Indian politics, state governance, Indian economy, Indian
business, geopolitics, defence, technology/science, infrastructure, sports, and
an important public statement. The operator must populate it with IDs of
already-ingested, real source documents in the isolated pilot database.

When explicitly authorized, run:

```powershell
$env:SCHEDULER_ENABLED = "false"
$env:PILOT_OPERATOR_AUTHORIZATION = "AUTHORIZED_FREE_TIER_PILOT"
$env:PILOT_FREE_TIER_ONLY = "true"
$env:PILOT_MAX_COST_USD = "0.00"
$env:PILOT_MAX_REQUESTS = "<operator-approved cap>"
$env:PILOT_ALLOWED_MODELS = "<operator-approved Gemini model>"
$env:PILOT_DATABASE_URL = "postgresql+asyncpg://.../x_agent_pilot..."
python scripts/run_newsroom_pilot.py --manifest newsroom_pilot_candidates.json --report pilot_results.json --account-handle <pilot-account>
```

The runner’s actual-results report retains source provenance, research evidence,
news format, editorial treatment, candidate text, claim verification, critic
result, image provenance, duplicate result, PolicyGate non-invocation, local
draft ID, observed provider requests, and application-recorded usage. It counts
processed candidates, generated/blocked drafts, no-post-worthy decisions, and
duplicates. It does not invent research failures, token counts, costs, or
provider outcomes.

## Release criteria

The status changes to **READY FOR AUTHORIZED FREE-TIER PILOT** only after the
operator has: stopped the scheduler, prepared and migrated the isolated pilot
database, selected ten real eligible source records, confirmed free-tier
eligibility with Google, selected a permitted request cap and model, and set the
explicit authorization values above. No automatic editorial/persona change or
X publishing is part of this pilot.
