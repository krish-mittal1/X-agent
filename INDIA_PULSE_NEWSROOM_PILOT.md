# India Pulse newsroom pilot — Phase 3B preflight

Date: 2026-11-03 (Asia/Kolkata)

## Status

**Superseded 2026-11-04 — the pilot was subsequently executed.** The blockers in
this preflight were cleared: the isolated pilot database was created, the
scheduler was disabled for the pilot process, and authorized generation ran.
Ten candidates were processed in `local_draft` mode; see
[PHASE_3B_4_PILOT_RESULTS.md](PHASE_3B_4_PILOT_RESULTS.md) for the recorded
results, the critic-gate verification, and the remaining open issues.

The section below records the preflight conditions as they stood on
2026-11-03, before that run.

## Safety controls verified

| Control | Observed value | Result |
| --- | --- | --- |
| X publishing mode | `local_draft` | Pass |
| Publishing enabled | `false` | Pass |
| Dry run | `true` | Pass |
| X API calls | none made | Pass |
| Gemini generation calls | none made | Pass |
| Automatic feedback learning | not started | Pass |

## Environment findings

### Windows asyncio / integration tests

The complete pytest run blocks before its first test body at:

`tests/integration/test_analytics.py::test_metrics_are_collected_and_evaluated`

Its session fixture calls `tests/conftest.py::_prepare_database()`, which calls
`asyncio.run(_recreate())`. The 90-second faulthandler trace stops in the
Windows asyncio self-pipe implementation:

`socket.accept -> _fallback_socketpair -> _make_self_pipe -> new_event_loop`

The same condition reproduces with `asyncio.run(asyncio.sleep(0))`; it is not
specific to analytics, newsroom code, PostgreSQL, or a background job. Changing
to a process-local `WindowsSelectorEventLoopPolicy` did not avoid the same
socket-pair failure.

The expected test PostgreSQL endpoint, `localhost:55432`, is also unreachable.
The fixture would normally skip when PostgreSQL is unavailable, but it cannot
reach that handling path while event-loop creation hangs.

### Isolated test environment

- WSL2 commands return `Wsl/E_ACCESS_DENIED`.
- Docker Compose is installed, but the Docker Desktop Linux engine is not
  running, so a pgvector container cannot be started.
- No production database was changed.

## Provider preflight

Configured model: `gemini-3.5-flash-lite`.

- A Gemini credential is configured locally; its value was not read or logged.
- Local application price fields are `0.0`, but they are application metadata,
  not evidence of the Google project’s billing tier or remaining quota.
- Google's current documentation lists a Free tier for Gemini 3.5 Flash-Lite,
  but a credential can still belong to a paid project or have exhausted free
  quota. See [Google's pricing page](https://ai.google.dev/gemini-api/docs/pricing)
  and [the model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).

No request was sent. A request would be the only way to establish entitlement
and quota for this credential, and the instruction prohibits a potentially paid
request without explicit authorization.

## Pilot candidates and outputs

No actual candidate was processed. Consequently, there are no legitimate
records for original source, research sources, generated text, verification,
critic score, image provenance, PolicyGate result, or staging result. Those
fields are intentionally blank rather than fabricated.

When the blockers are cleared, process these ten slots with primary-source
material only, then add the actual pipeline records below:

| Slot | Category | Required primary source | Format / treatment |
| --- | --- | --- | --- |
| 1 | National politics | Election Commission, Parliament, court, or ministry record | `political_news` / straight |
| 2 | State governance | State government order or official release | `political_news` / straight |
| 3 | Economy | RBI, MoSPI, Finance Ministry, or SEBI release | `business_economy` / context |
| 4 | Company/business | Company exchange filing or original company statement | `business_economy` / straight |
| 5 | Geopolitics | MEA statement or joint communiqué | `geopolitics_defence` / context |
| 6 | Defence | Ministry of Defence release | `geopolitics_defence` / straight |
| 7 | Technology/science | ISRO, MeitY, DST, or original company release | `straight_news` / engaging only if supported |
| 8 | Infrastructure | Railways, NHAI, airport, or ministry release | `straight_news` / context |
| 9 | Sports | BCCI, federation, or event organiser release | `human_interest_sports` / straight |
| 10 | Quote-led | Full speech, press transcript, or official video transcript | `quote_led` / exact quote |

For every completed slot, record: source URL and publication date; secondary
sources; exact candidate text and character count per part; source-backed claims
and verifier result; critic score and flags; image source URL/provenance;
PolicyGate result; and `local_draft` staging ID. Do not record engagement
metrics until they are actually collected.

## Human-review rubric

Before approving any local draft, the operator should assess:

1. Each material fact and quote against the original source.
2. Whether the story is newsworthy and current.
3. Source authority and any unresolved contradiction.
4. Natural newsroom writing, rather than generic AI language.
5. Hook strength relative to the development's actual importance.
6. Clear relevance to India.
7. Whether human review would approve publication.

Flag repeated openings, generic phrases, forced controversy, excessive emoji,
or unsupported “why it matters” claims. Do not use this review to automatically
change the persona.

## Preconditions to resume

1. Restore a Windows asyncio environment that can create a socket pair, or grant
   WSL2 access / start Docker Desktop and launch an isolated pgvector test DB.
2. Confirm in writing that this Gemini credential may make up to a specified
   number of **free-tier-only** text requests, with a maximum permitted spend of
   `$0.00`. If free-tier entitlement cannot be established, provide a separate
   paid-call authorization and cap.
3. Supply or authorize retrieval of ten current, primary-source news items.

Only then run the pipeline in `local_draft` mode. Live publishing and automated
feedback learning remain out of scope.
