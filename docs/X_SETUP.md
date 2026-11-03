# Connecting the agent to X

The agent posts through the **official X API v2** using **OAuth 1.0a user context**.
There is no scraping, no browser automation and no cookie reuse anywhere in the
codebase — `app/publishing/x_client.py` signs each request with HMAC-SHA1.

You need four values: consumer key, consumer secret, access token, access token
secret. Everything below is done in the X developer portal; nothing is done in this
repository until step 5.

---

## 1. Create the app

1. Go to <https://developer.x.com> and sign in with the account that will post.
2. **Projects & Apps → Overview → Create Project**, then create an App inside it.
   The app must belong to the account you want to post as.
3. Open the app → **Settings → User authentication settings → Set up**.
   - App permissions: **Read and write**
   - Type of App: **Web App, Automated App or Bot**
   - Callback URI / Website URL: any `https://` URL you control. OAuth 1.0a
     posting does not use a callback, but the portal requires a value here.
4. **Save.** The portal will warn that changing permissions invalidates existing
   tokens. That is fine — you have not generated any yet.

> **The mistake everyone makes:** generate the Access Token *before* setting
> **Read and write**, and posting returns `403 Forbidden` forever. The token
> carries the permissions the app had when it was created. If you already did
> this, change the permission, then **regenerate** the Access Token and Secret.

## 2. Copy the keys

In the app's **Keys and tokens** tab:

| Portal label | Goes into |
|---|---|
| API Key | `X_CONSUMER_KEY` |
| API Key Secret | `X_CONSUMER_SECRET` |
| Access Token | `X_ACCESS_TOKEN` |
| Access Token Secret | `X_ACCESS_TOKEN_SECRET` |

Two things worth knowing:

- The **API Key Secret** is shown once. If you lost it, regenerate the pair and
  update both `X_CONSUMER_KEY` and `X_CONSUMER_SECRET`.
- The Access Token must be the one belonging to the posting account, not a
  different team member's. OAuth 1.0a posts as whoever owns the token.

`X_BEARER_TOKEN` is **only** used for reads (mention ingestion, analytics). It is
app-only auth and cannot post. Leave it empty unless you enable
`X_READS_ENABLED=true`; the write path does not need it.

## 3. Set the account handle

```bash
X_PERSONA_HANDLE=your_handle      # no @
```

This resolves the local `accounts` row that every draft belongs to. It must match
the account that owns the access token, or you will publish from the wrong identity.

## 4. Keep the kill switches in their safe position

```bash
PUBLISHING_ENABLED=false
DRY_RUN=true
SCHEDULER_ENABLED=false
```

`DRY_RUN=true` means the full policy gate runs and the outcome is logged and stored,
but no HTTP call is made to X. This is how you test the whole path for free.

## 5. Recreate the container and verify configuration

```bash
docker compose up -d
docker compose exec app python -m scripts.bootstrap   # idempotent
curl -s localhost:8000/ready
```

`/ready` reports `"publishing": {"ok": true, "detail": "enabled=False dry_run=True"}`.
Credentials are reported only as `set` / `unset` — no secret is ever printed.

## 6. Exercise the gate without posting

Flip publishing on while keeping the dry run:

```bash
PUBLISHING_ENABLED=true
DRY_RUN=true
```

```bash
docker compose up -d
docker compose exec app python -m scripts.cycle
```

A draft that passes every gate is recorded as **`simulated`** in the cycle report
and in `publishing_jobs`. Nothing reaches X. If it comes back `blocked`, read the
gate output — that is the real reason it would have failed live:

```bash
docker compose exec app python -m scripts.cycle >/dev/null
TOKEN=$(grep -E '^ADMIN_API_TOKEN=' .env | cut -d= -f2-)
curl -s -H "Authorization: Bearer $TOKEN" "localhost:8000/drafts?limit=5" | python -m json.tool
```

## 7. First real post

Only after a `simulated` run, and only with a draft you have actually read:

```bash
DRY_RUN=false
PUBLISHING_ENABLED=true
MAX_POSTS_PER_RUN=1          # one post for the first live run
MIN_HOURS_BETWEEN_POSTS=1.5
```

```bash
docker compose up -d
docker compose exec app python -m scripts.cycle
```

Confirm it landed:

```bash
docker compose exec db psql -U techx -d techx -c \
  "select id, x_post_id, url, text from posts order by id desc limit 1"
```

## 8. Then, and only then, the scheduler

```bash
SCHEDULER_ENABLED=true
```

The scheduler runs ingest → rank → draft → publish on the configured intervals. At
that point the account posts without you watching, so the budgets become the real
control surface: `MAX_ORIGINAL_POSTS_PER_DAY`, `MIN_HOURS_BETWEEN_POSTS`,
`EMERGENCY_PAUSE`.

---

## What each error actually means

| Symptom | Cause | Fix |
|---|---|---|
| `401 Unauthorized` | key/secret/token wrong, or a stray space or newline in `.env` | re-copy the values; no quotes around them in `.env` |
| `403 Forbidden` on `POST /2/tweets` | app permission is Read-only, or the token predates the permission change | set Read and write, then **regenerate** the access token |
| `403` "duplicate content" | the same text was posted in the last 24h | X rejects exact repeats; the duplicate gate should have caught it first |
| `429 Too Many Requests` | X's own post rate limit (per user / per app) | the client honours `retry-after`; slow down `MIN_HOURS_BETWEEN_POSTS` |
| `402 Payment Required` | the plan has no posting allowance left this month | check the developer portal's usage page |
| `status=needs_reconciliation` | a timeout mid-request: the post may or may not exist | check the account timeline before doing anything else — the app will not auto-retry, deliberately |

## Costs

Gemini's free tier and local embeddings cost nothing. X's API is not free in general:
the Free plan historically allows a small number of posts per month and is enough to
run a low-volume account, but the quotas change and are enforced per app. Check the
current numbers on your own project's page in the developer portal rather than
trusting a blog post — including this document.

If you only ever post a handful of times a day, the Free plan is sufficient. If you
want thread posting at volume, you are on a paid tier and should decide that
deliberately.

## What the agent will never do, regardless of configuration

- Post about X trending topics through automation.
- Send unsolicited replies based on keyword matches. The reply path requires a
  recorded inbound interaction and stays draft-only unless a human approves *and*
  both reply switches are on.
- Like, follow, repost or DM anything.
- Scrape X or drive a browser.
- Post through an LLM's claim that it is allowed: every outbound action passes the
  deterministic gate in `app/publishing/policy_gate.py`.
