# Growth playbook (pacing, originality, coordination)

Why this exists: a days-old account posting a wire headline every 60 minutes gets
zero reach, and the fix is not more posting — it is looking like a person.
`app/growth/` encodes that as deterministic rules the agent obeys.

## What each piece does

| Module | Question it answers |
|---|---|
| `growth/pacing.py` | *When* may we post, reply or follow today? |
| `growth/ramp.py` | *How much* may this account do today, given its age? |
| `growth/commentary.py` | Does this draft add anything of our own? |
| `growth/alts.py` | Would this action link our accounts together? |
| `growth/planner.py` | The daily plan combining all four. |
| `growth/service.py` | Reads account state, persists the plan, records taken actions. |

## 1. Humanised schedule (`pacing`)

Slots are generated per account per local day from a seeded PRNG:

- the local window is split into `target + 1` random weights, so **every gap
  differs** — no fixed interval, no top-of-the-hour pattern;
- `GROWTH_MIN_POST_GAP_MINUTES` is reserved before the free time is distributed,
  so the floor holds by construction;
- the same account and day always produce the same slots, so a restart cannot
  double-post;
- the daily count varies inside `POSTS_PER_DAY_MIN..POSTS_PER_DAY_MAX`.

`tasks.publishing` refuses to publish outside a due slot. `slot_due` allows a
`GROWTH_SLOT_TOLERANCE_MINUTES` window so a late scheduler wake still fires, but
a slot missed by hours is skipped rather than posted at a random time.

## 2. Account-age ramp (`ramp`)

| Stage | Age | Posts/day | Replies/day | Follows/day |
|---|---|---|---|---|
| `warming` | < `GROWTH_WARMUP_DAYS` (14) | 3 | 10 | 10 |
| `ramping` | 14–28 days | linear climb | linear climb | linear climb |
| `established` | >= 28 days | `POSTS_PER_DAY_MAX` | 15 | 40 |

Unknown creation date is treated as **new** — the conservative stage is always
the default when age cannot be proven. `MAX_ORIGINAL_POSTS_PER_DAY` still caps
the result, so the existing safety switch wins over any growth target.

## 3. Originality gate (`commentary`)

A draft is held when:

- its opening line is >= 85% the words of the source headline;
- its opening adds fewer than `GROWTH_MIN_COMMENTARY_WORDS` words beyond the
  headline;
- it lifts `GROWTH_MAX_VERBATIM_WORDS` (12) or more words verbatim from the
  source body.

Held drafts are reported as `held_for_originality` and stay `ready` — a reviewer
edits them; nothing is silently dropped. Operator-written drafts are exempt.

## 4. Coordination guard (`alts`)

Any planned follow/like/reply is rejected when:

1. another of our accounts touched the same target inside
   `GROWTH_CROSS_ACCOUNT_COOLDOWN_HOURS` (72h) — this is the pattern X uses to
   link accounts;
2. the target already has `GROWTH_MAX_ACTIONS_PER_TARGET_PER_DAY` actions today;
3. the same kind of action is closer than its floor
   (`GROWTH_FOLLOW_MIN_SECONDS`, `GROWTH_LIKE_MIN_SECONDS`,
   `GROWTH_REPLY_MIN_SECONDS`);
4. a like lands within `GROWTH_BURST_WINDOW_SECONDS` of following the same
   target (scripted-engagement signature).

## 5. Media and links

`GROWTH_LINK_SHARE` (0.25) sets how many of today's posts may carry a link;
`GROWTH_REQUIRE_MEDIA` marks media as expected on the plan. Both are advisory
inputs for the writer/reviewer, not silent rewrites.

## Running it

```bash
# today's plan for the default account
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" localhost:8000/growth/plan

# actions the plan queued (nothing is sent by this)
curl -H "Authorization: Bearer $ADMIN_API_TOKEN" localhost:8000/growth/actions

# record an action you actually took, so the guard knows about it
curl -X POST -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"kind":"follow","target_handle":"someone"}' \
  localhost:8000/growth/actions
```

The scheduler also runs `growth_plan` every `GROWTH_PLAN_INTERVAL_MINUTES` (60)
and stores the queued actions in `action_logs`, which is what makes the guard's
history survive restarts.

## What still needs a human

Replies and follows are **planned and queued, never sent**. The reply gate
(`policy_gate.evaluate_reply`) already requires an inbound conversation, operator
approval and recorded approval from X for automated replies; the growth layer
does not weaken any of that. Use the dashboard or `POST /growth/actions` as you
work through the queue.

## Configuration reference

```
GROWTH_ENABLED=true
POSTS_PER_DAY_MIN=3
POSTS_PER_DAY_MAX=6
POSTING_WINDOW_START_HOUR=8
POSTING_WINDOW_END_HOUR=23
GROWTH_MIN_POST_GAP_MINUTES=55
GROWTH_SLOT_TOLERANCE_MINUTES=25
GROWTH_WARMUP_DAYS=14
GROWTH_ESTABLISHED_DAYS=28
GROWTH_WARMUP_POSTS_PER_DAY=3
GROWTH_WARMUP_REPLIES_PER_DAY=10
GROWTH_WARMUP_FOLLOWS_PER_DAY=10
GROWTH_WARMUP_LIKES_PER_DAY=15
GROWTH_TARGET_REPLIES_PER_DAY=15
GROWTH_TARGET_FOLLOWS_PER_DAY=40
GROWTH_TARGET_LIKES_PER_DAY=30
GROWTH_REPLY_MIN_FOLLOWERS=500
GROWTH_REPLY_MAX_FOLLOWERS=100000
GROWTH_MIN_COMMENTARY_WORDS=6
GROWTH_MAX_HEADLINE_OVERLAP=0.85
GROWTH_MAX_VERBATIM_WORDS=12
GROWTH_LINK_SHARE=0.25
GROWTH_REQUIRE_MEDIA=true
GROWTH_FOLLOW_MIN_SECONDS=90
GROWTH_LIKE_MIN_SECONDS=45
GROWTH_REPLY_MIN_SECONDS=120
GROWTH_CROSS_ACCOUNT_COOLDOWN_HOURS=72
GROWTH_MAX_ACTIONS_PER_TARGET_PER_DAY=1
GROWTH_BURST_WINDOW_SECONDS=300
GROWTH_FOLLOW_TARGETS=
GROWTH_PLAN_INTERVAL_MINUTES=60
```
