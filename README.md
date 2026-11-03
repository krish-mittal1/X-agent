# tech-x-agent

A research-driven agent for a technology-focused X account. It discovers primary
sources (RSS/Atom, GitHub releases), verifies claims against retrieved text, drafts
original posts, stores everything in a PostgreSQL + pgvector memory, and publishes
only after deterministic policy gates pass. Replies are **draft-only** by default
and require explicit, recorded approval configuration before any send path is
available.

The LLM writes content. It does not decide whether content may be published, does
not execute tools, and cannot override policy, budget, or consent checks.

---

## What is implemented and verified

Verified locally with **203 automated tests**, a real PostgreSQL 16 + pgvector
database, mocked X/Gemini HTTP transports, static typing, and a built Docker Compose
stack:

- Configuration validation, kill switches, emergency pause, production auth guard.
- Gemini provider abstraction: timeouts, bounded retries with jitter, rate-limit and
  error taxonomy, JSON-mode structured output with one repair attempt, usage and
  cost recording, budget enforcement.
- RSS and GitHub ingestion with allowlists, SSRF guards, redirect validation,
  size caps, per-host rate limits, dedupe, and provenance.
- Deterministic candidate scoring + optional LLM refinement (relevance/usefulness
  only; source reliability remains human-authored).
- Research verification: verbatim-quote evidence checks, stale-source rejection,
  failed-fetch rejection, narrow numeric-contradiction detection, and
  prompt-injection flagging that excludes a source from evidence.
- PostgreSQL + pgvector RAG: chunking, local embeddings, HNSW index, hybrid
  vector + full-text retrieval with reciprocal rank fusion, account and
  conversation isolation, novelty scoring, consolidation.
- Persona-driven drafting with X weighted-length rules, duplicate/near-duplicate
  checks, banned-phrase and fabricated-experience checks.
- Official X API v2 client with OAuth 1.0a signing (verified against X's published
  signature vector), post creation, mention reads, metrics reads, ambiguity-safe
  publishing, audit logs, and thread support.
- Deterministic publishing gates and a human review queue with authenticated API.
- APScheduler jobs with per-slot idempotency and cross-process advisory locks.
- Dockerfile, Docker Compose, migrations, bootstrap/ingest/cycle/re-embed scripts.
- A real local Sentence Transformers model (384 dimensions) and a healthy Compose
  deployment (`/ready` 200, migrations, bootstrap, and a degraded dry-run cycle
  inside the containers).

**Not verified without credentials:** live posting to X, live Gemini inference, live
X reads/metrics/mentions. All of these are exercised against mock transports.
Nothing is published during tests.

---

## Architecture

```
RSS / GitHub ──► ingestion adapters ──► source_documents ──► chunk + embed
                                                │                 │
                                                ▼                 ▼
                                        deterministic score   memory_records
                                                │            + memory_embeddings
                                                ▼                 │
                            ┌───────────── draft writer ◄── hybrid retrieval ┘
                            │                   │
                     claim verifier      duplicate / text rules
                            │                   │
                            └────► draft (ready | blocked)
                                        │
                                 deterministic policy gate
                                        │
                        dry-run log  OR  official X API v2 POST
                                        │
                              posts + episodic memory + audit
```

The workflow is hard-coded in [`app/agent/orchestrator.py`](app/agent/orchestrator.py).
There is no general-purpose tool loop.

---

## Quick start with Docker Compose

```bash
cp .env.example .env        # add ADMIN_API_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, feed URLs
docker compose up --build -d
curl -s localhost:8000/health
docker compose exec app python -m scripts.bootstrap --handle yourhandle
docker compose exec app python -m scripts.ingest
docker compose exec app python -m scripts.cycle
```

The container runs `alembic upgrade head` before starting the API. Compose
overrides `DATABASE_URL` to the bundled Postgres service. Keep
`PUBLISHING_ENABLED=false` and `DRY_RUN=true` until you have reviewed drafts.

## Local development

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt --extra-index-url https://download.pytorch.org/whl/cpu

# Postgres + pgvector (host port 55432 avoids clashing with a local install)
docker run -d --name techx-pg -p 55432:5432 \
  -e POSTGRES_USER=techx -e POSTGRES_PASSWORD=techx -e POSTGRES_DB=techx \
  pgvector/pgvector:pg16

cp .env.example .env                 # fill ADMIN_API_TOKEN / GEMINI_* / feeds
alembic upgrade head
python -m scripts.bootstrap --handle techbrief
uvicorn app.main:app --reload --port 8000
```

Lightweight install without torch: `pip install -r requirements.txt` and set
`EMBEDDING_PROVIDER=hashing`. That embedder is deterministic and vector-based but
has no semantic generalisation — use it only for development and tests.

### Database migrations

```bash
alembic upgrade head
alembic current
alembic downgrade -1        # destructive; only on a database you own
```

Integration tests create/migrate a separate `techx_test` database automatically
(`TEST_DATABASE_URL` overrides it). They **skip with an explicit reason** if no
database is reachable.

### Tests and lint

```bash
pytest -q
ruff check app tests scripts
mypy app
```

Latest local result: `203 passed`, `ruff` clean, `mypy` clean (`app`, 59 files),
`alembic upgrade head` then `alembic check` reports no pending model changes.

The Compose stack was built and started; `/health` and `/ready` returned 200,
`scripts.bootstrap` and a degraded dry-run `scripts.cycle` ran inside the app
container without external calls. Live Gemini/X calls still require credentials.

---

## Pipeline

### 1. Discovery and scoring

Adapters: `app/ingestion/rss.py`, `app/ingestion/github.py`, optional
`app/ingestion/x_research.py` (mentions only; never trends or scraping).

Each source document stores canonical URL, title, publication time, retrieval
time, full extracted text, a content hash, static source reliability, fetch
status, and provenance. Dedupe uses canonical URL (hard unique constraint) and
content hash (syndicated copies).

Scoring (`app/research/scoring.py`) is a weighted sum of six signals, normalised
to `[0,1]`:

| Signal | Default weight | Source |
|---|---|---|
| freshness | 0.22 | exponential decay, half-life `FRESHNESS_HALF_LIFE_HOURS` |
| technical relevance | 0.23 | persona interest coverage + technical term density |
| novelty | 0.20 | 1 − cosine similarity to recent published posts |
| source reliability | 0.15 | static host/type allowlist in `source_registry.py` |
| evidence quality | 0.12 | primary-source flag, date, body length, specifics |
| audience usefulness | 0.08 | actionable developer vocabulary minus marketing fluff |

An optional LLM classifier may refine only relevance/usefulness (30% blend) and
flag marketing fluff. The score breakdown, weights, and reasons are persisted on
the source document.

### 2. Verification

A factual claim is `verified` only when its supporting quote appears verbatim
(whitespace/case-insensitive) in a source that was actually retrieved, did not
fail, is younger than `VERIFICATION_STALE_DAYS`, and is not flagged for suspected
prompt injection. Interpretations, estimates, and opinions are recorded separately
and never counted as verified facts. Unverified facts block the draft. Numeric
contradictions between factual claims are flagged rather than averaged.

Quantitative statements in the post without a declared supporting quote also
block the draft.

### 3. RAG memory

`memory_records` holds persona, episodic, semantic and conversation memories;
`memory_embeddings` stores real `vector(384)` values with the model name. Retrieval
is account-scoped and conversation-scoped (a thread sees global memory and its own
thread only, never another conversation). Hybrid ranking:

1. pgvector cosine search (HNSW index) and Postgres full-text search (`simple`
   configuration, GIN index) produce ranked lists.
2. Reciprocal Rank Fusion: `Σ 1/(60 + rank)`.
3. Multiplicative priors: recency decay, source reliability, and a small
   conversation-match boost.

Embeddings from different models are never mixed; every query filters on the
current model. Changing models requires `python -m scripts.reembed`; changing
dimensions requires a migration and a fresh schema.

Memory consolidation expires old conversation memory, invalidates semantic memory
whose source failed, and clamps low-reliability confidence. It never turns model
output into a verified fact.

### 4. Drafting

`app/agent/writer.py` builds a persona prompt with primary sources wrapped in
`<untrusted_source>` delimiters, retrieved memory, and recent posts. External text
is sanitised to prevent delimiter escape, and the system prompt instructs the
model to ignore embedded instructions. That is defence in depth, not the security
boundary: deterministic checks and policy gates decide what can happen.

Every draft is persisted with its verification report, duplicate check, text-rule
report, claims, sources, and status (`ready`, `blocked`, `needs_review`, ...).

#### Content formats

The voice is a low-posture Indian tech shitposter: lowercase, specific, funny about
ideas and never about people. It is seeded from `app/agent/persona.py` and can be
replaced without touching code (`python -m scripts.persona --file persona.json`).

`app/agent/formats.py` decides *which shape* each draft takes, weighted by
`POST_FORMAT_MIX`. The application chooses; the model is told what to write. That
keeps the shitpost/insight ratio an operator number instead of a model mood, and
the most recently used format is down-weighted so the account does not post the
same shape twice in a row.

| Format | Needs a source | Numbers need a quote |
|---|---|---|
| `shitpost` | no | no (hyperbole is the format) |
| `relatable` | no | no |
| `hot_take` | no | yes |
| `news_jab` | yes | yes |
| `explainer` | yes | yes |

The first-person rule is scoped the same way: `me after 6 hours of debugging` is
allowed in a joke format, `i tested this on my gpu` is not allowed in an explainer.
Typography is folded to ASCII (smart quotes, en/em dashes, ellipses) so output never
carries the punctuation tell of machine-written copy.

### 5. Publishing gates

`app/publishing/policy_gate.py` is a pure deterministic function over application
state. An original post publishes only if all of these hold:

- emergency pause off and `PUBLISHING_ENABLED=true`;
- active account, draft status `ready`, no published post rows yet;
- verification passed and zero unverified facts (configurable);
- duplicate-check record present and negative;
- X weighted length, link, hashtag, banned-phrase and fabricated-experience rules pass;
- sources present; not a trending-topic draft;
- daily post budget and minimum spacing satisfied;
- credentials present unless `DRY_RUN=true` (dry-run simulates, never calls X).

Budget/spacing/kill-switch failures are **soft**: the draft stays `ready` for a
later run. Content/verification failures are **hard**: the draft becomes `blocked`
with reasons persisted.

Publishing takes a PostgreSQL advisory lock, writes a unique job row and a Post
row per thread part, indexes published text into episodic memory, and writes an
audit log. Network timeouts during post creation are treated as **ambiguous**:
the draft moves to `needs_reconciliation` and is never retried automatically.

### 6. Replies

Reply drafting requires a recorded inbound interaction (a mention of this account,
ingested through the official API). Keyword-only cold replies are impossible by
construction. Opt-out phrases create `opt_out_records` and suppress drafting.

`AI_REPLY_APPROVAL_OBTAINED` and `AI_REPLY_PUBLISHING_ENABLED` must both be true
for the internal send path to be available; configuration validation refuses to
start with sending enabled but approval unrecorded. Separate from that, a reply
must be human-approved in the review queue, have conversation context, no opt-out,
and pass the same text/duplicate/budget checks. **No HTTP endpoint sends replies.**

---

## Configuration

All variables are documented in [`.env.example`](.env.example). The most important:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | must use `postgresql+asyncpg://` |
| `ADMIN_API_TOKEN` | bearer auth for `/drafts` and `/metrics`; required in production |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | inference; no model ID is hardcoded. Without these the app runs degraded |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | local embeddings; dims must match the schema |
| `RSS_FEEDS`, `GITHUB_REPOSITORIES`, `GITHUB_SEARCH_QUERIES` | research allowlists |
| `PUBLISHING_ENABLED`, `DRY_RUN`, `EMERGENCY_PAUSE` | outbound kill switches |
| `MAX_ORIGINAL_POSTS_PER_DAY`, `MIN_HOURS_BETWEEN_POSTS` | rate control |
| `AI_REPLY_APPROVAL_OBTAINED`, `AI_REPLY_PUBLISHING_ENABLED` | dual reply gate |
| `LLM_DAILY_BUDGET_USD`, `LLM_MONTHLY_BUDGET_USD`, `LLM_MAX_REQUESTS_PER_DAY` | cost caps (`0` USD budget = unlimited) |
| `POST_FORMAT_MIX` | shitpost/hot-take/explainer weighting |
| `LLM_TOKENS_PER_MINUTE`, `LLM_REQUESTS_PER_DAY`, etc. | provider rate limits the client paces itself against |
| `SCHEDULER_ENABLED` | starts APScheduler when the API boots |

Secrets are loaded from the environment/`.env`; `.env` is gitignored. Logs redact
authorization headers, bearer tokens, signatures, and API keys.

### Rate limits

On Google Gemini's free tier the numbers for `openai/gpt-oss-120b` are **30 requests/minute,
1000 requests/day, 8000 tokens/minute, 200000 tokens/day**. Check them against your
own key, because they are per organisation:

```bash
curl -s -D - -o /dev/null https://api.gemini.com/openai/v1/models \
  -H "Authorization: Bearer $GEMINI_API_KEY" | grep -i x-ratelimit
```

Tokens/minute is the constraint that actually bites: the persona prompt, the JSON
schema and the source excerpts together are roughly 2400 tokens, so a repair retry
or two in the same minute used to cross 8000 and earn a 429.

`app/providers/rate_limit.py` is a sliding-window limiter over both minutes and days.
It estimates the cost of a request before sending, waits for headroom instead of
sending and failing, and settles the estimate with real `usage` figures afterwards so
the accounting stays honest. `LLM_RATE_LIMIT_SAFETY_RATIO` (default `0.9`) keeps a
margin for tokenizer disagreement. If a daily quota is exhausted the cycle stops and
leaves the remaining candidates queued — it does not burn the remaining budget on
requests it knows will fail.

A `429` still has a fallback path: `retry-after` and Google Gemini's `x-ratelimit-reset-*`
headers are durations (`570ms`, `1m26.4s`), and they are parsed as such before
exponential backoff is used.

---

## Administration API

Authenticated with `Authorization: Bearer $ADMIN_API_TOKEN`. If the token is not
configured the endpoints return `503` rather than running unauthenticated.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness (no I/O) |
| GET | `/ready` | DB, pgvector dimension, migrations, LLM/config status |
| GET | `/drafts?status=&kind=&limit=&offset=` | paginated review queue |
| GET | `/drafts/{id}` | draft, claims, verification, policy gate |
| POST | `/drafts/{id}/approve` | human approval (`reviewer`, `notes`) |
| POST | `/drafts/{id}/reject` | rejection with mandatory reason |
| GET | `/metrics/summary` | post/draft/failure counts, LLM spend, recent metrics |

```bash
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  "http://127.0.0.1:8000/drafts?status=needs_review"

curl -X POST -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reviewer":"alice","notes":"specific and accurate"}' \
  http://127.0.0.1:8000/drafts/1/approve
```

There is deliberately no endpoint that accepts arbitrary text to publish, and no
reply-send endpoint.

---

## Scheduled jobs

With `SCHEDULER_ENABLED=true`, APScheduler runs (interval, jitter, max one instance):

| Job | Default interval |
|---|---|
| `rss_ingestion` | 60 min |
| `github_discovery` | 60 min |
| `ranking` | 120 min |
| `drafting` | 180 min |
| `publishing` | 30 min |
| `reply_drafts` | 60 min |
| `memory_consolidation` | 24 h |
| `analytics` | 6 h |

Each job records a `job_runs` row keyed by its interval slot and holds a
PostgreSQL advisory lock, so duplicate runs are skipped across restarts and
multiple workers. Failures are recorded without crashing the scheduler.

Operator scripts:

```bash
python -m scripts.bootstrap --handle techbrief   # account + default persona
python -m scripts.ingest --source all            # ingest + score
python -m scripts.cycle                          # full dry-run cycle by default
python -m scripts.reembed                        # after changing embedding model
python -m scripts.persona --show                 # inspect the active persona
python -m scripts.persona --file persona.json    # publish a new persona version
```

`persona.json` accepts `name`, `system_prompt`, `style_guide`, `interests[]`, and
`boundaries[]`. Updates create a new version and deactivate the previous one; the
old row is retained for audit.

---

## X API access and costs

Step-by-step setup, including the permission mistake that silently makes every
post fail, is in [docs/X_SETUP.md](docs/X_SETUP.md).

- Posting uses OAuth 1.0a user context against `POST /2/tweets`; the signature
  implementation is verified against X's documented example.
- Reads (posts, metrics, mentions) require `X_READS_ENABLED=true` **and** an access
  tier that includes those endpoints. The code reports missing permissions clearly
  and skips the feature; it does not scrape X.
- X's free tier is limited; read endpoints commonly require a paid plan. Check the
  current plan matrix at <https://docs.x.com/x-api> and your developer portal.
- Google Gemini has a free tier; limits and model availability change. Set a model from the
  current catalogue (`https://console.gemini.com/docs/models`) and configure budgets.
- Embeddings run locally on CPU. Cost is compute/hosting, not per-token.
- Hosting and PostgreSQL are yours to provide; Docker Compose is suitable for a
  single small VM.

The application does not promise that running it is free.

## Platform compliance

The application is designed around X's automation rules and developer agreement
(<https://help.x.com/en/rules-and-policies/x-automation>,
<https://developer.x.com/en/developer-terms/agreement-and-policy>):

- official API only; no browser scripting, no scraping, no automated likes;
- no trend discovery or trend-driven posting;
- no unsolicited keyword replies;
- AI-generated replies require prior written approval from X and remain disabled by
  default at two independent configuration layers;
- opt-out requests are recorded and enforced;
- conservative posting limits and human review for replies.

**Operator responsibility:** label the account as automated where X requires it,
keep within your API plan, review drafts, and obtain any required written approval
before enabling reply sending. Policies change; re-read them before deployment.

---

## Remaining limitations

1. **No live credentials were available**, so real posting, Google Gemini inference and X
   reads are only verified with mocks.
2. Read features (metrics/mentions) may be unavailable on your X plan; the app
   degrades and reports it.
3. Single-account operation. Multi-account data is isolated in the schema, but the
   admin API resolves one default account.
4. Replies are drafted only for inbound mentions recorded through the API. There is
   no general conversation search (that would require additional paid endpoints).
5. Contradiction detection is narrow: numeric conflicts between declared factual
   claims. It is not a full fact-checking system.
6. Thread publishing stops on first failure; partial threads may require manual
   reconciliation. Ambiguous network failures are never auto-retried.
7. The dimension of `memory_embeddings.embedding` is fixed at 384. Changing the
   model dimension needs a migration and fresh vector data.
8. The admin API uses a single bearer token, not per-user roles. Keep it on a
   private network or behind an authenticated reverse proxy.
9. SSRF validation resolves DNS before each request but a DNS-rebinding TOCTOU
   window remains; use a trusted feed allowlist.
10. `DRY_RUN=true` is the default, so a quick start will draft and simulate, not post.
11. There is no external error-monitoring integration (Sentry etc.); structured
    JSON logs and `/metrics/summary` are the operational surface.
12. Google Gemini usage is estimated from reported tokens; cost figures use configurable
    per-million-token prices, not provider billing data.
13. Model availability is validated when the scheduler starts, not by `/ready`;
    `/ready` reports configuration, not a live inference probe.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/ready` returns 503 with pgvector error | run `alembic upgrade head`; ensure the `vector` extension is available |
| `memory_embeddings.embedding has dimension ...` | set `EMBEDDING_DIMENSIONS` to match the migrated column or migrate deliberately |
| `sentence-transformers is not installed` | install `requirements-embeddings.txt`, or use `EMBEDDING_PROVIDER=hashing` for dev |
| Drafts stay `blocked` with "unverified factual claim" | the model quoted text not present in the retrieved source; inspect `/drafts/{id}` and the source text |
| `403`/`401` from X | check OAuth 1.0a keys, app permissions, and read/write access tier |
| Everything is skipped with "publishing disabled" | set `PUBLISHING_ENABLED=true`; keep `DRY_RUN=true` until verified |
| Google Gemini 404 model error | set `GEMINI_MODEL` from the current catalogue |
| Tests skip integration | start Postgres+pgvector or set `TEST_DATABASE_URL` |

## Next operator actions

1. Create the X developer app, enable OAuth 1.0a user authentication, and confirm
   your plan includes posting (and reads if you want metrics).
2. Set `ADMIN_API_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`, and a small allowlist of
   primary-source feeds/repositories.
3. Run `python -m scripts.bootstrap`, then `python -m scripts.ingest` and inspect
   `/drafts`.
4. Keep `DRY_RUN=true`; run `python -m scripts.cycle` and review the gate report.
5. Only then enable publishing, with conservative limits, and monitor
   `/metrics/summary` and logs.
