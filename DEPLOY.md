# Deployment

Run the agent 24/7 on a VPS so it drafts and posts on its own schedule.

## Prerequisites

- A VPS with Docker and the compose plugin (tested on Ubuntu 22.04 / 24.04).
- Posting ability, one of:
  - credits on the X Developer app (`X_SETUP.md`), posting through the official API, or
  - the headless-browser path this repo implements, fed by `X_AUTH_TOKEN` + `X_CT0`.
    For this account that is currently the only path that actually posts, because
    the X API answers `402 credits depleted` for reads and writes.

## What you are deploying

| File | Purpose |
| --- | --- |
| `compose.prod.yaml` | The production stack: `db` (pgvector), `app` (API + scheduler), `monitor` (failure alarm). |
| `Dockerfile` | The app image, including headless Chromium so the browser publisher works without a display. |
| `scripts/monitor.py` | The alarm: exits non-zero and can POST to a webhook when the agent silently stops working. |

Never deploy `compose.yaml`; it is the local development stack (scheduler off by
default, no browser).

## 1. Build

```bash
cd /path/to/repo
docker compose -f compose.prod.yaml build
```

The image installs `chromium` (see `Dockerfile`). If that line disappears, the
browser publisher fails with "Brave browser is not installed or detected" /
"no Chromium executable found".

## 2. Configure

Environment has two layers, and the split matters.

1. **`compose.prod.yaml`** pins production intent as *literal* values in the
   shared `x-prod-flags` anchor: `APP_ENV=production`, `SCHEDULER_ENABLED=true`,
   `PUBLISHING_ENABLED=true`, `DRY_RUN=false`, `X_API_ENABLED=false`,
   `X_PUBLISHING_MODE=brave_cdp`, and `DATABASE_URL` pointed at the `db`
   service. Both `app` and `monitor` consume that anchor.

   These are deliberately not `${VAR:-default}`. Compose interpolates from the
   same `.env`, so a copied development file — which says `DRY_RUN=true`,
   `PUBLISHING_ENABLED=false`, `X_PUBLISHING_MODE=local_draft` — would have
   switched the whole deployment into no-op mode while looking healthy. Literal
   `environment:` values win over `env_file`, so those keys in `.env` are now
   inert. That is intended: switch behaviours by editing the compose file, not
   by editing a secret file.

2. **`.env`** on the server holds secrets and per-instance tunables. It is the
   same file the local install uses, it is gitignored, and containers read it
   via `env_file`. Create it with `chmod 600`.

A minimal server `.env`:

```ini
ADMIN_API_TOKEN=<long-random-string>   # required: production refuses to start without it
X_PERSONA_HANDLE=ssh_bugs

# Browser-session posting (copy both from a real logged-in browser session)
X_AUTH_TOKEN=...
X_CT0=...

# Drafting
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
LLM_MAX_REQUESTS_PER_DAY=500

# Cadence (see "Throughput" below)
DRAFT_INTERVAL_MINUTES=45
PUBLISH_INTERVAL_MINUTES=15
MAX_POSTS_PER_RUN=1

# Alerting (see "Monitoring" below)
ALERT_WEBHOOK_URL=
```

`POSTGRES_PASSWORD` defaults to `techx` and feeds both the `db` service and
`DATABASE_URL`. If you change it on a database that already exists, change it in
both places and in the Postgres role itself, or the app cannot connect.

`ADMIN_API_TOKEN` already exists in the local `.env`; keep it when you copy the
file, or the container exits at startup with an explicit error.

## 3. Start

```bash
docker compose -f compose.prod.yaml up -d
docker compose -f compose.prod.yaml logs -f app
```

The container runs `alembic upgrade head` before uvicorn, so migrations apply on
every start. Watch for the scheduler start message.

### Sharing the host with other projects

The stack is namespaced and capped so it does not fight its neighbours:

- **Project name is `techx-agent`**, so containers are `techx-agent-app-1`,
  `techx-agent-db-1`, `techx-agent-monitor-1`, the network is
  `techx-agent_default` and volumes are `techx-agent_pgdata` /
  `techx-agent_media_tmp`. Another compose project on the same daemon can also
  have a `db` and an `app` — different namespaces, no clash.
- **No database port is published**, and the API is bound to
  `127.0.0.1:${APP_HOST_PORT:-8100}` — loopback only, off the default 8000 that
  other projects usually take. If 8100 is also busy, put `APP_HOST_PORT=8200`
  (or any free port) in `.env`. Check with:
  `ss -ltnp | grep -E ':(8000|8100)\b'`.
- **Reach the dashboard through a tunnel**, never by opening the port:
  `ssh -L 8100:127.0.0.1:8100 user@host`, then browse `http://localhost:8100`.
- **Resource ceilings** (enforced by Compose, verified with `docker inspect`):
  `app` 3 GB / 2 CPU, `db` 1 GB / 1 CPU, `monitor` 256 MB / 0.5 CPU. Override
  with `APP_MEMORY_LIMIT`, `APP_CPUS`, `DB_MEMORY_LIMIT`, `DB_CPUS`,
  `MONITOR_MEMORY_LIMIT`, `MONITOR_CPUS` in `.env` — raise `APP_MEMORY_LIMIT`
  rather than removing it if headless Chromium gets OOM-killed mid-publish.
- **Disk:** budget ~5 GB free on `/var/lib/docker` (image plus the model).

Two shared-host hazards worth knowing:

1. `docker volume prune` (or `system prune --volumes`) run **while this stack is
   stopped** can delete `techx-agent_pgdata`. Never prune volumes on a shared
   box without listing them first.
2. `docker system prune -a` in another project deletes our `techx-agent:prod`
   image. Running containers survive, but the next rebuild is a full rebuild.

Also: build during a quiet window. The `pip`/torch layer spikes past 1 GB of RAM
and competes with whatever else is running.

## 4. Bootstrap an empty database

A fresh database has no account row; the scheduler then logs that it has no
account and drafts nothing.

```bash
docker compose -f compose.prod.yaml exec app python -m scripts.bootstrap --handle ssh_bugs
```

Idempotent. Not needed on the existing production data.

## 5. Prove it before trusting it

1. Health: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8100/health` → `200`.
2. Readiness: `curl -s http://localhost:8100/ready` → `200` and every check `ok`;
   a `503` names the failing dependency (database, pgvector, migrations).
3. Run one cycle by hand:
   ```bash
   docker compose -f compose.prod.yaml exec app python -m scripts.cycle --i-understand-live-publishing
   ```
   Remove the flag while `DRY_RUN=true`; it exists to stop an accidental live post.
4. Confirm the post on the profile. Until a real post exists, the deployment is
   unproven, and the monitor will say so.

## Monitoring

The `monitor` sidecar runs `scripts/monitor.py` every 15 minutes. It prints only
problems, and POSTs `{"text": ..., "content": ...}` to `ALERT_WEBHOOK_URL` when
a check fails (Slack, Discord and ntfy-style endpoints all accept that shape).
Set that variable before you walk away — with it unset, alerts are printed to
the container log and nobody sees them.

Checks, and the thresholds you can override in `.env`:

| Check | Fails when | Setting |
| --- | --- | --- |
| `api` / `ready` | `/health` isn't 200, or `/ready` is 503 (database, pgvector or migrations) | — |
| `post-freshness` | the newest post is older than the limit while publishing is live | `MAX_POST_AGE_HOURS` (6) |
| `publish-failures` | too many `failed`/`unknown` publish rows in the window | `MAX_PUBLISH_FAILURES` (3), `FAILURE_WINDOW_HOURS` (24) |
| `cookie-session` | the latest publish failure looks auth-shaped (cookie/login/401/403/expired) | — |
| `credentials` | the live publishing mode is missing what it needs (for `brave_cdp`: `X_AUTH_TOKEN`/`X_CT0`) | — |
| `scheduler` | no job has run for 2× `INGEST_INTERVAL_MINUTES` (min 30 min) | `INGEST_INTERVAL_MINUTES` |
| `publish-loop` | the `publishing` job hasn't run for 2× publish interval + 15 min | `PUBLISH_INTERVAL_MINUTES` |
| `stuck-jobs` | a job has been `running` for over 30 minutes | — |

Run it by hand any time, including on the host:

```bash
docker compose -f compose.prod.yaml exec monitor python -m scripts.monitor
docker compose -f compose.prod.yaml exec monitor python -m scripts.monitor --json
```

Exit code `0` means healthy, `1` means at least one check failed, so a cron entry
or systemd timer works too:

```cron
*/15 * * * * cd /opt/x-agent && docker compose -f compose.prod.yaml exec -T monitor python -m scripts.monitor --quiet || echo "x-agent unhealthy" | mail -s "x-agent" you@example.com
```

## Ops

- Follow logs: `docker compose -f compose.prod.yaml logs -f`.
- Rebuild after a code change: `docker compose -f compose.prod.yaml up -d --build`.
- Back up the database:
  ```bash
  docker compose -f compose.prod.yaml exec -T db pg_dump -U techx techx | gzip > /backups/techx-$(date +%F).sql.gz
  ```
- `down -v` destroys the stack **and** the Postgres volume: every draft, post,
  memory and source document. Never run it on production unless that is the point.

Run exactly one `app` container. The scheduler lives in-process; a second replica
doubles the job wiring. The `pg_advisory_lock` in `app/jobs/runner.py` limits the
damage but is not a substitute for one writer.

Both `app` and `monitor` use `restart: unless-stopped`, so the stack survives a
host reboot and hard crashes.

## Known limitations

- **Posting through the official X API is off until credits exist.** It returns
  `402 credits depleted`. Either buy credits or rely on the browser session.
- **The browser cookie session is the single point of failure.** When
  `auth_token`/`ct0` rotate or are invalidated, publishing stops while the
  container still looks healthy. Re-copy both, then
  `docker compose -f compose.prod.yaml up -d app`. The `cookie-session` and
  `post-freshness` checks are what turn this into a notification instead of a
  silent outage.
- **Post IDs stored for browser-published posts are synthetic**
  (`brave_<timestamp>_0`, `url` NULL). Analytics, reply threading and duplicate
  detection cannot see the real post until that is fixed.
- **Throughput is bounded by the policy gate, not by ambition.** With the
  cadence values above (`DRAFT_INTERVAL_MINUTES=45`, `PUBLISH_INTERVAL_MINUTES=15`,
  `MAX_POSTS_PER_RUN=1`, `MIN_HOURS_BETWEEN_POSTS=0.8`) the ceiling is roughly one
  post per hour, and `MAX_ORIGINAL_POSTS_PER_DAY=24` caps it at 24/day, which
  `PolicyGate` enforces. Loosening the gate instead of buying credits or fixing
  quality is what gets accounts flagged.
- **Approval quality is thin.** Long-form editorial judgement is not enforced
  end to end, so a slice of what ships reads like a single dense sentence.
