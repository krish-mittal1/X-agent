import sys
sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

from app.agent.writer import ContentWriter
from app.config import get_settings
from app.memory.database import get_session_factory, init_engine, session_scope
from app.memory.models import (
    Account,
    Draft,
    EditorialEvaluation,
    Event,
    EventDevelopment,
    EventPostLink,
    ResearchReport,
    SourceDocument,
)
from app.providers.factory import LLMService, build_provider
from sqlalchemy import select, text

# 24 Diverse Candidate IDs covering Tech, Economy, Geopolitics, Science, Infrastructure, and Follow-ups
PILOT_CANDIDATES = [
    # 1. Technology & Digital Crime / AI
    1842, # Bengaluru techie loses 1.2 Crore in 48 hour digital arrest scam
    2534, # Digi Yatra trials start at 7 airports for international passengers
    3176, # Beyond disaster relief: India-US Tiger Triumph exercise brings cyber & AI
    2632, # Mahadev betting app scam: Key accused Saurabh Chandrakar extradited
    3024, # Mahadev scam accused extradition exposes protection money
    
    # 2. Business & Economy
    2799, # Supreme Court raps Centre for stalling enforcement of Vedanta arbitration
    1844, # ED seizes 15 luxury cars and 50 crore cash in massive real estate scam
    3005, # INDIA bloc rally: Delhi Traders claim ₹11,000 cr Diwali season loss
    2410, # "Who Will Compensate?" Traders Claim Rs 11,000 Crore Loss Due To INDIA
    3074, # Why India inked ₹53,866 crore deal for 8 new warships
    
    # 3. Geopolitics & National Security / Politics
    2979, # Rahul Gandhi challenges PM Modi to open debate on 'foreign help'
    3121, # 'Gaddar': BJP accuses Rahul Gandhi of treason, calls him 'Bhasmasur'
    1930, # '40% of Ramlila Maidan vacant': Sambit Patra questions INDIA bloc's 10
    2496, # Priyank Kharge alleges attempt to use ED, I-T agencies against Congress
    2616, # Rahul Gandhi being used by foreign powers to destabilise India: Nitin
    
    # 4. Science & Space / Energy
    2585, # Why ISRO’s latest success matters more than it appears
    2971, # Clean energy investments helped India soften West Asia crisis impact
    2956, # Is India On Track To Meet Its Green Energy Goals?
    1908, # Why Elon Musk is weighing a Tesla-SpaceX merger
    
    # 5. Infrastructure & Mobility
    2634, # Indian Railways bans night charging ports from 11 PM to 5 AM to prevent fire hazards
    2493, # Congress demands scrapping of Siang hydropower project in Arunachal
    3275, # Three including drivers injured after omnibus hits lorry on Chennai-Bengaluru
    2498, # Raids on Power TV boss Rakesh Shetty’s residence, office in Bengaluru
    3271, # Bombay HC asks BMC to re-upload Dia Mirza firecracker appeal
]

async def run_pilot():
    settings = get_settings()
    # Enforce strict local staging constraints
    assert settings.x_publishing_mode == "local_draft", "Must run in local_draft mode"
    assert settings.publishing_enabled is False, "Publishing must be disabled"
    assert settings.dry_run is True, "Dry-run must be enabled"

    init_engine(settings)
    session_factory = get_session_factory()
    provider = build_provider(settings)
    llm = LLMService(settings, provider, session_factory)
    writer = ContentWriter(settings, llm, session_factory)

    async with session_factory() as session:
        acc = (await session.execute(text("SELECT id FROM accounts WHERE handle = 'ssh_bugs'"))).scalar_one()

    print(f"=== STARTING PHASE 2.8 PILOT EXECUTION ({len(PILOT_CANDIDATES)} CANDIDATES) ===")
    print(f"Account ID: {acc} (@ssh_bugs) | Model: {settings.gemini_model} | Mode: {settings.x_publishing_mode}")

    results = []
    pilot_start_time = time.time()

    for idx, cand_id in enumerate(PILOT_CANDIDATES, 1):
        print(f"\n[{idx}/{len(PILOT_CANDIDATES)}] Processing Candidate ID: {cand_id}...")
        t0 = time.time()
        
        async with session_factory() as session:
            doc = await session.get(SourceDocument, cand_id)
            if not doc:
                print(f"  [SKIP] Document {cand_id} not found in DB.")
                continue
            orig_title = doc.title
            orig_url = doc.url
            source_type = doc.source_type
            reliability = doc.reliability_score

        try:
            # Execute full intelligence pipeline
            outcome = await writer.create_original_draft(acc, [cand_id])
            elapsed = time.time() - t0
            print(f"  Result: Draft ID {outcome.draft_id} -> Status: {outcome.status} ({elapsed:.2f}s)")
            
            # Fetch detailed persisted data from database
            async with session_factory() as session:
                draft = await session.get(Draft, outcome.draft_id)
                policy = draft.policy_report or {}
                verif = draft.verification_report or {}
                
                # Fetch evaluation
                ev = (await session.scalars(
                    select(EditorialEvaluation).where(EditorialEvaluation.draft_id == outcome.draft_id)
                )).first()
                
                # Fetch event linkage
                link = (await session.scalars(
                    select(EventPostLink).where(EventPostLink.draft_id == outcome.draft_id)
                )).first()
                event = await session.get(Event, link.event_id) if link else None
                
                # Fetch research report
                rep_id = policy.get("research_report_id")
                rep = await session.get(ResearchReport, rep_id) if rep_id else None

            # Collect metrics
            cand_result = {
                "candidate_id": cand_id,
                "original_title": orig_title,
                "original_url": orig_url,
                "source_type": source_type,
                "source_reliability": reliability,
                "draft_id": outcome.draft_id,
                "status": outcome.status,
                "latency_seconds": round(elapsed, 2),
                "research": {
                    "report_id": rep.id if rep else None,
                    "confidence_score": rep.confidence_score if rep else None,
                    "sources_count": len(rep.sources) if rep and hasattr(rep, "sources") else 0,
                    "conflicts_count": len(rep.conflicting_info) if rep and rep.conflicting_info else 0,
                },
                "event": {
                    "event_id": event.id if event else None,
                    "event_title": event.title if event else None,
                    "event_status": event.status if event else None,
                    "linked_angle": link.context_angle if link else None,
                },
                "editorial": {
                    "selected_angle": draft.tone,
                    "hook": draft.body.split("\n")[0] if draft.body else "",
                    "body": draft.body,
                    "rationale": draft.rationale,
                    "alternatives": [
                        {"angle": a.get("angle"), "body": a.get("parts", [""])[0]}
                        for a in policy.get("alternative_angles", [])
                    ],
                    "critic": {
                        "overall_score": ev.overall_score if ev else None,
                        "verdict": ev.verdict if ev else None,
                        "critic_reasoning": ev.critic_reasoning if ev else None,
                        "insight_score": ev.insight_score if ev else None,
                        "persona_score": ev.persona_score if ev else None,
                    } if ev else None,
                },
                "verification": {
                    "passed": verif.get("passed", False),
                    "verified_facts": verif.get("verified_facts", 0),
                    "unverified_facts": verif.get("unverified_facts", 0),
                    "contradicted_claims": verif.get("contradicted_claims", 0),
                    "hard_stop_triggered": verif.get("hard_stop_triggered", False),
                    "issues": verif.get("issues", []),
                },
                "policy_decision": {
                    "status": outcome.status,
                    "blocking_reasons": policy.get("blocking_reasons", []),
                }
            }
            results.append(cand_result)

        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [ERROR] Candidate {cand_id} failed: {type(e).__name__}: {e}")
            results.append({
                "candidate_id": cand_id,
                "original_title": orig_title,
                "status": "failed",
                "error": f"{type(e).__name__}: {str(e)}",
                "latency_seconds": round(elapsed, 2),
            })

        # Pacing sleep to strictly respect 15 RPM
        await asyncio.sleep(2.5)

    total_time = time.time() - pilot_start_time

    # Aggregate Statistics
    total_processed = len(results)
    ready_count = sum(1 for r in results if r.get("status") == "ready")
    blocked_count = sum(1 for r in results if r.get("status") == "blocked")
    needs_review_count = sum(1 for r in results if r.get("status") == "needs_review")
    failed_count = sum(1 for r in results if r.get("status") == "failed")
    
    latencies = [r["latency_seconds"] for r in results if "latency_seconds" in r]
    avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0
    p50_latency = round(sorted(latencies)[len(latencies) // 2], 2) if latencies else 0
    p95_latency = round(sorted(latencies)[int(len(latencies) * 0.95)], 2) if latencies else 0

    summary = {
        "pilot_timestamp": datetime.now(UTC).isoformat(),
        "total_candidates_processed": total_processed,
        "ready_drafts": ready_count,
        "blocked_drafts": blocked_count,
        "needs_review_drafts": needs_review_count,
        "failed_jobs": failed_count,
        "total_pilot_duration_seconds": round(total_time, 2),
        "latency_metrics": {
            "mean_seconds": avg_latency,
            "min_seconds": min(latencies) if latencies else 0,
            "max_seconds": max(latencies) if latencies else 0,
            "p50_seconds": p50_latency,
            "p95_seconds": p95_latency,
        },
        "results": results,
    }

    with open("pilot_execution_results.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n=== PILOT EXECUTION COMPLETE ===")
    print(f"Total Processed: {total_processed}")
    print(f"Ready: {ready_count} | Blocked: {blocked_count} | Needs Review: {needs_review_count} | Failed: {failed_count}")
    print(f"Average Latency: {avg_latency}s (p50: {p50_latency}s, p95: {p95_latency}s)")
    print("Full results saved to 'pilot_execution_results.json'.")

if __name__ == "__main__":
    asyncio.run(run_pilot())

