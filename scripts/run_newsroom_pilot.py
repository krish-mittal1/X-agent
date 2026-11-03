"""Run the authorised India Pulse ten-story pilot without any X publishing.

This script deliberately has no defaults that can begin a real-provider run.  It
requires an operator-supplied manifest, a dedicated pilot database, an explicit
free-tier acknowledgement, a zero-dollar ceiling, and a model allowlist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select

from app.agent.writer import ContentWriter
from app.config import Settings, get_settings
from app.memory.database import get_session_factory, init_engine
from app.memory.models import (
    Account,
    Draft,
    EditorialEvaluation,
    ResearchReport,
    ResearchSource,
    SourceDocument,
)
from app.providers.factory import LLMService
from app.providers.gemini_provider import GeminiProvider
from app.schemas.content import ContentGenerationError


REQUIRED_CATEGORIES = (
    "indian_politics", "state_governance", "indian_economy", "indian_business",
    "geopolitics", "defence", "technology_science", "infrastructure", "sports",
    "public_statement",
)


class PilotStopError(RuntimeError):
    """A fail-closed pilot limit or provider failure."""


@dataclass(frozen=True)
class Candidate:
    category: str
    source_document_id: int


class CappedGeminiProvider(GeminiProvider):
    """Counts actual chat attempts; pilot settings disable provider retries."""

    def __init__(self, settings: Settings, max_requests: int) -> None:
        super().__init__(settings)
        self.max_requests = max_requests
        self.request_count = 0

    async def _chat(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.request_count >= self.max_requests:
            raise PilotStopError(f"pilot request cap reached ({self.max_requests})")
        self.request_count += 1
        return await super()._chat(*args, **kwargs)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise PilotStopError(f"{name} must be set explicitly for a real pilot")
    return value


def _pilot_settings() -> tuple[Settings, int]:
    base = get_settings()
    if base.x_publishing_mode != "local_draft" or base.publishing_enabled or not base.dry_run:
        raise PilotStopError("pilot requires local_draft, PUBLISHING_ENABLED=false, and DRY_RUN=true")
    if base.scheduler_enabled:
        raise PilotStopError("stop the scheduler before a pilot; SCHEDULER_ENABLED must be false")
    if _required_env("PILOT_OPERATOR_AUTHORIZATION") != "AUTHORIZED_FREE_TIER_PILOT":
        raise PilotStopError("operator has not granted free-tier pilot authorization")
    if _required_env("PILOT_FREE_TIER_ONLY").lower() != "true":
        raise PilotStopError("PILOT_FREE_TIER_ONLY=true is required")
    if _required_env("PILOT_MAX_COST_USD") not in {"0", "0.0", "0.00"}:
        raise PilotStopError("pilot maximum permitted cost must be exactly $0.00")
    max_requests = int(_required_env("PILOT_MAX_REQUESTS"))
    if not 1 <= max_requests <= 100:
        raise PilotStopError("PILOT_MAX_REQUESTS must be between 1 and 100")
    allowed_models = {
        item.strip()
        for item in _required_env("PILOT_ALLOWED_MODELS").split(",")
        if item.strip()
    }
    if base.gemini_model not in allowed_models:
        raise PilotStopError("configured Gemini model is not in PILOT_ALLOWED_MODELS")
    pilot_url = _required_env("PILOT_DATABASE_URL")
    database_name = urlparse(pilot_url).path.lstrip("/").split("?", 1)[0]
    if not database_name.startswith("x_agent_pilot") or pilot_url == base.database_url:
        raise PilotStopError("PILOT_DATABASE_URL must name a distinct x_agent_pilot database")
    if not base.gemini_api_key:
        raise PilotStopError("Gemini credentials are not configured")
    # Retry is zero so each counted provider attempt represents at most one request.
    pilot_settings = base.model_copy(
        update={
            "database_url": pilot_url,
            "scheduler_enabled": False,
            "llm_max_retries": 0,
        }
    )
    return pilot_settings, max_requests


def _load_manifest(path: Path) -> list[Candidate]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    candidates = [
        Candidate(str(item["category"]), int(item["source_document_id"]))
        for item in raw["candidates"]
    ]
    categories = tuple(item.category for item in candidates)
    if len(candidates) != 10 or set(categories) != set(REQUIRED_CATEGORIES) or len(set(categories)) != 10:
        raise PilotStopError("manifest must contain exactly one candidate for each required category")
    return candidates


def _policy_summary(draft: Draft) -> dict[str, object]:
    policy = dict(draft.policy_report or {})
    return {
        "draft_status": draft.status,
        "format": policy.get("format"),
        "editorial_treatment": draft.tone,
        "blocking_reasons": policy.get("blocking", policy.get("blocking_reasons", [])),
        "duplicate": policy.get("duplicate"),
        "image": {
            key: policy.get(key)
            for key in (
                "image_source_url",
                "image_relevant",
                "image_verified",
                "image_reason",
            )
        },
        "policy_gate": "not invoked: publishing is disabled and this pilot never calls Publisher",
    }


async def _result_for_draft(
    session, candidate: Candidate, draft: Draft
) -> dict[str, object]:  # type: ignore[no-untyped-def]
    source = await session.get(SourceDocument, candidate.source_document_id)
    policy = dict(draft.policy_report or {})
    report_id = policy.get("research_report_id")
    report = await session.get(ResearchReport, report_id) if report_id else None
    evidence = []
    if report is not None:
        rows = await session.scalars(
            select(ResearchSource).where(ResearchSource.report_id == report.id)
        )
        evidence = [
            {
                "url": row.url,
                "title": row.title,
                "publisher": row.publisher,
                "primary": row.is_primary,
            }
            for row in rows
        ]
    evaluation = await session.scalar(
        select(EditorialEvaluation).where(EditorialEvaluation.draft_id == draft.id)
    )
    return {
        "category": candidate.category,
        "source_provenance": {
            "id": candidate.source_document_id,
            "title": source.title if source else None,
            "url": source.url if source else None,
        },
        "research_evidence": evidence,
        "draft_id": draft.id,
        "candidate_text": [draft.body, *(draft.thread_parts or [])],
        "claim_verification": draft.verification_report or {},
        "critic": (
            {
                "score": evaluation.overall_score,
                "verdict": evaluation.verdict,
                "reasoning": evaluation.critic_reasoning,
            }
            if evaluation
            else None
        ),
        **_policy_summary(draft),
    }


async def run(manifest: Path, report_path: Path, account_handle: str) -> None:
    settings, max_requests = _pilot_settings()
    candidates = _load_manifest(manifest)
    init_engine(settings)
    session_factory = get_session_factory()
    provider = CappedGeminiProvider(settings, max_requests)
    llm = LLMService(settings, provider, session_factory)
    writer = ContentWriter(settings, llm, session_factory)
    results: list[dict[str, object]] = []
    stopped_reason: str | None = None
    try:
        async with session_factory() as session:
            account = await session.scalar(select(Account).where(Account.handle == account_handle))
            if account is None:
                raise PilotStopError(
                    f"pilot account {account_handle!r} is not present in the pilot database"
                )
            account_id = account.id
        for candidate in candidates:
            try:
                outcome = await writer.create_original_draft(account_id, [candidate.source_document_id])
            except PilotStopError as exc:
                stopped_reason = f"{type(exc).__name__}: {exc}"
                break
            except ContentGenerationError as exc:
                message = str(exc)
                if any(term in message.lower() for term in ("authentication", "quota", "billing", "rate limit")):
                    stopped_reason = f"ContentGenerationError: {message}"
                    break
                results.append(
                    {
                        "category": candidate.category,
                        "source_provenance": {"id": candidate.source_document_id},
                        "pipeline_failure": message,
                    }
                )
                continue
            except Exception as exc:  # Preserve the failure in the factual pilot record.
                results.append(
                    {
                        "category": candidate.category,
                        "source_provenance": {"id": candidate.source_document_id},
                        "pipeline_failure": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            async with session_factory() as session:
                draft = await session.get(Draft, outcome.draft_id)
                if draft is None:
                    raise PilotStopError(f"draft {outcome.draft_id} was not persisted")
                results.append(await _result_for_draft(session, candidate, draft))
    finally:
        await llm.aclose()
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "pilot_status": "stopped" if stopped_reason else "completed",
        "stop_reason": stopped_reason,
        "configured_max_requests": max_requests,
        "observed_provider_requests": provider.request_count,
        "observed_cost_usd": "not observable from Gemini billing; application estimate is logged per request",
        "candidates_processed": len(results),
        "drafts_generated": sum(
            1 for item in results if item.get("draft_status") in {"ready", "needs_review"}
        ),
        "drafts_blocked": sum(1 for item in results if item.get("draft_status") == "blocked"),
        "research_or_pipeline_failures": sum(
            1 for item in results if "pipeline_failure" in item
        ),
        "no_post_worthy_decisions": sum(
            1
            for item in results
            if "NO_POST_WORTHY_ANGLE" in str(item.get("blocking_reasons", []))
        ),
        "duplicate_detections": sum(
            1 for item in results if bool((item.get("duplicate") or {}).get("duplicate"))
        ),
        "results": results,
    }
    report_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    if stopped_reason:
        raise PilotStopError(stopped_reason)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--account-handle", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.manifest, args.report, args.account_handle))


if __name__ == "__main__":
    try:
        main()
    except PilotStopError as exc:
        print(f"PILOT BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
