import asyncio
import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text

from app.config import Settings
from app.memory.database import init_engine, session_scope
from app.memory.models import ResearchClaim, ResearchReport, ResearchSource
from app.research.verification import ClaimVerifier, SourceView
from app.schemas.content import DraftClaim


async def run_evidence_audit():
    print("=== TEST 3: MULTI-SOURCE EVIDENCE & FACT-CHECKING AUDIT ===")
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
    )
    init_engine(settings)

    # 1. Setup 5-source scenario in database
    async with session_scope() as session:
        # Create ResearchReport
        report = ResearchReport(
            account_id=1,
            topic="Quantum Computing Breakthrough 2026",
            executive_summary="Scientists achieve 1000-qubit fault-tolerant quantum error correction.",
            what_happened="IBM and National Lab announced the 1000-qubit milestone.",
            key_entities=[{"name": "National Lab"}, {"name": "IBM"}],
            confidence_score=0.92,
            conflicting_info=[
                {
                    "claim": "The processor operates at 100 millikelvin temperature",
                    "contradiction_detail": "Primary spec states it requires 15 millikelvin dilution refrigeration."
                }
            ]
        )
        session.add(report)
        await session.flush()
        report_id = report.id

        # Source 1: Initial News Wire
        s1 = ResearchSource(
            report_id=report_id,
            url="https://newswire.example.com/tech/quantum-1000",
            title="Quantum Chip Sets 1,000 Qubit Milestone",
            publisher="TechWire Global",
            source_type="news",
            is_primary=False,
            reliability_score=0.75,
        )
        # Source 2: Primary Official Whitepaper/Lab Announcement
        s2 = ResearchSource(
            report_id=report_id,
            url="https://nature.example.org/articles/quantum-error-correction-1000",
            title="Fault-tolerant quantum error correction on a 1000-qubit architecture",
            publisher="Nature Science Press",
            source_type="paper",
            is_primary=True,
            reliability_score=0.98,
        )
        # Source 3: Independent Secondary Investigative Source
        s3 = ResearchSource(
            report_id=report_id,
            url="https://independent-review.org/quantum-analysis",
            title="Analysis: The Real Hardware Limits Behind the 1000 Qubit Claim",
            publisher="Quantum Hardware Review",
            source_type="analysis",
            is_primary=False,
            reliability_score=0.88,
        )
        # Source 4: Syndicated Clone of Source 1 (Same text & hash)
        s4 = ResearchSource(
            report_id=report_id,
            url="https://syndicated-aggregator.net/wire/quantum-1000",
            title="Quantum Chip Sets 1,000 Qubit Milestone (Reprint)",
            publisher="Aggregator Clone Network",
            source_type="syndicated",
            is_primary=False,
            reliability_score=0.50,
        )
        # Source 5: Contradictory Source (Conflicting Temperature)
        s5 = ResearchSource(
            report_id=report_id,
            url="https://critics-tech.com/quantum-debunked",
            title="Debunking the Cooling Claims of the 1000 Qubit Chip",
            publisher="Tech Skeptic Journal",
            source_type="news",
            is_primary=False,
            reliability_score=0.70,
        )
        session.add_all([s1, s2, s3, s4, s5])
        await session.flush()

        # Link ResearchClaim with valid source_id FK
        c1 = ResearchClaim(
            report_id=report_id,
            source_id=s2.id,
            claim_text="The quantum processor achieves 1000 logical qubits with fault-tolerant thresholds.",
            claim_type="fact",
            confidence=0.95,
            context_notes="Directly stated in Nature paper abstract.",
            is_disputed=False,
        )
        c2 = ResearchClaim(
            report_id=report_id,
            source_id=s5.id,
            claim_text="The processor operates at 100 millikelvin temperature",
            claim_type="fact",
            confidence=0.40,
            context_notes="Contradicted by primary architecture specifications.",
            is_disputed=True,
        )
        session.add_all([c1, c2])
        await session.commit()

        # Test 3A: Verify DB persistence and Foreign Key linkage
        c1_persisted = (await session.execute(
            text(f"SELECT id, source_id, report_id, claim_text FROM research_claims WHERE id = {c1.id}")
        )).first()
        print(f"3A. Persisted Claim 1 FK linkage: id={c1_persisted.id}, source_id={c1_persisted.source_id} (matches s2.id={s2.id})")
        assert c1_persisted.source_id == s2.id
        assert c1_persisted.report_id == report_id

    # Test 3B: Syndication Deduping in ClaimVerifier
    from types import SimpleNamespace
    verifier = ClaimVerifier(settings)

    shared_quote = "The quantum processor achieves 1000 logical qubits under fault-tolerant thresholds."
    sources = [
        SourceView(SimpleNamespace(
            id=1,
            url="https://newswire.example.com/tech/quantum-1000",
            content=f"Breaking: {shared_quote} This marks a new era.",
            syndication_hash="hash-clone-group-abc",
            is_primary=False,
            reliability_score=0.75,
            source_type="news",
            fetch_status="ok",
        )),
        SourceView(SimpleNamespace(
            id=2,
            url="https://nature.example.org/articles/quantum-error-correction-1000",
            content=f"Abstract: {shared_quote} Experimental data confirms logical coherence.",
            syndication_hash="hash-nature-unique-xyz",
            is_primary=True,
            reliability_score=0.98,
            source_type="paper",
            fetch_status="ok",
        )),
        SourceView(SimpleNamespace(
            id=3,
            url="https://syndicated-aggregator.net/wire/quantum-1000",
            content=f"Reprint: Breaking: {shared_quote} This marks a new era.",
            syndication_hash="hash-clone-group-abc",  # Duplicate hash of Source 1
            is_primary=False,
            reliability_score=0.50,
            source_type="syndicated",
            fetch_status="ok",
        )),
        SourceView(SimpleNamespace(
            id=4,
            url="https://mirror-newswire.com/tech/quantum-1000",
            content=f"Wire mirror: {shared_quote}",
            syndication_hash="hash-clone-group-abc",  # Another duplicate hash
            is_primary=False,
            reliability_score=0.50,
            source_type="syndicated",
            fetch_status="ok",
        )),
    ]

    indep_count = verifier._count_independent_sources(shared_quote, sources)
    print(f"3B. Independent sources counted (out of 4 sources containing quote, 3 are clones): {indep_count}")
    # Source 1, 3, 4 share syndication hash "hash-clone-group-abc". Source 2 has unique hash.
    # Total independent non-syndicated sources should be exactly 2!
    assert indep_count == 2, f"Expected 2 independent sources, got {indep_count} (syndication deduping failed!)"

    # Test 3C: Contradiction Detection & Blocking
    # Claim with conflicting numerical assertions or reported contradiction
    claims_with_contradiction = [
        DraftClaim(
            claim="The processor achieves 1000 logical qubits with high fidelity.",
            claim_type="fact",
            supporting_quote="The quantum processor achieves 1000 logical qubits",
            source_url="https://nature.example.org/articles/quantum-error-correction-1000",
        ),
        DraftClaim(
            claim="The processor operates at 100 millikelvin temperature during runtime.",
            claim_type="fact",
            supporting_quote="The processor operates at 100 millikelvin temperature",
            source_url="https://critics-tech.com/quantum-debunked",
        )
    ]

    # Verify against research report conflicting claims
    report_contradictions = [
        {
            "claim": "The processor operates at 100 millikelvin temperature",
            "contradiction_detail": "Requires 15 millikelvin dilution refrigeration."
        }
    ]

    sources_with_cooling = list(sources) + [
        SourceView(SimpleNamespace(
            id=5,
            url="https://critics-tech.com/quantum-debunked",
            content="Critique: The processor operates at 100 millikelvin temperature which causes decoherence.",
            syndication_hash="hash-critic-123",
            is_primary=False,
            reliability_score=0.70,
            source_type="news",
            fetch_status="ok",
        ))
    ]

    verification_result = verifier.verify(
        claims=claims_with_contradiction,
        sources=sources_with_cooling,
        conflicting_claims=report_contradictions,
    )

    print(f"3C. Verification Passed: {verification_result.passed}")
    print(f"3C. Contradicted Claims Count: {verification_result.contradicted_claims}")
    print(f"3C. Hard Stop Reasons: {verification_result.hard_stop_reasons}")
    
    assert verification_result.passed is False, "Draft verification should FAIL when contradictions are present!"
    assert verification_result.contradicted_claims >= 1, "Expected at least 1 contradicted claim"
    assert any("Contradiction identified" in r for r in verification_result.hard_stop_reasons), "Hard stop reason missing contradiction details"

    print("\nTEST 3 RESULT: PASS (Multi-source evidence, FK linkage, syndication deduping, and contradiction blocking verified)")

if __name__ == "__main__":
    asyncio.run(run_evidence_audit())
