import sys

sys.path.insert(0, ".")
import asyncio

from app.config import get_settings
from app.memory.database import init_engine, session_scope
from app.memory.embeddings import build_embedding_provider
from app.memory.models import ResearchReport
from app.research.event_service import EventIntelligenceService


class BrokenEmbedder:
    dimensions = 384
    async def embed(self, texts):
        raise RuntimeError("Embedding service connection timed out")

async def run_event_audit():
    settings = get_settings()
    init_engine(settings)
    embedder = build_embedding_provider(settings)

    print("=== PART 4: EVENT INTELLIGENCE AUDIT SCENARIOS ===")

    async with session_scope() as session:
        # Clean up any previous audit test events created in earlier test runs
        from sqlalchemy import text
        await session.execute(text("DELETE FROM event_developments WHERE event_id IN (SELECT id FROM events WHERE title LIKE 'Tata%' OR title LIKE 'ISRO%' OR title LIKE 'Indian Railways%')"))
        await session.execute(text("DELETE FROM event_post_links WHERE event_id IN (SELECT id FROM events WHERE title LIKE 'Tata%' OR title LIKE 'ISRO%' OR title LIKE 'Indian Railways%')"))
        await session.execute(text("DELETE FROM events WHERE title LIKE 'Tata%' OR title LIKE 'ISRO%' OR title LIKE 'Indian Railways%'"))
        await session.commit()

        service = EventIntelligenceService(settings, embedder=embedder)
        account_id = 1

        # Scenario 1: Initial Tata Semiconductor Event
        print("\n--- 1. Creating Initial Tata Semiconductor Event ---")
        rep1 = ResearchReport(
            account_id=account_id,
            topic="Tata Electronics Semiconductor Fab in Dholera Gujarat",
            executive_summary="Tata Electronics partners with Powerchip to construct a ₹91,000 crore semiconductor manufacturing fab in Dholera.",
            what_happened="Tata Electronics and PSMC announced cleanroom construction for India's first commercial semiconductor fab.",
            key_entities=[{"name": "Tata Electronics", "type": "company", "role": "builder"}, {"name": "PSMC", "type": "company", "role": "partner"}],
            verified_facts=["Investment of ₹91,000 crore approved by Cabinet", "Cleanroom construction underway"],
            unresolved_claims=[],
            conflicting_info=[],
            historical_context="Part of India Semiconductor Mission.",
            stakeholder_analysis=[],
            potential_implications=["Domestic chip supply for automotive and defense"],
            confidence_score=0.90,
            status="completed",
        )
        session.add(rep1)
        await session.flush()

        ev1, is_new1, delta1 = await service.match_or_create_event(session, account_id, rep1, category="technology")
        print(f"Event 1 ID: {ev1.id} | New: {is_new1} | Title: {ev1.title} | Delta: {delta1}")
        assert is_new1 is True

        # Scenario 1b: Follow-up Tata Semiconductor development matching existing event
        print("\n--- 1b. Follow-up Development on Tata Fab ---")
        rep1_followup = ResearchReport(
            account_id=account_id,
            topic="Tata Dholera Fab Begins Cleanroom Construction and Equipment Tooling",
            executive_summary="Tata Electronics begins cleanroom tooling for its Dholera fab with first batch of Taiwanese engineers arriving.",
            what_happened="Engineers from Taiwan arrive at Dholera site as cleanroom construction advances.",
            key_entities=[{"name": "Tata Electronics", "type": "company", "role": "builder"}, {"name": "Taiwan", "type": "country", "role": "support"}],
            verified_facts=["Taiwanese engineers on site", "Cleanroom tooling commencing"],
            unresolved_claims=[],
            conflicting_info=[],
            historical_context="Follow up to fab construction.",
            stakeholder_analysis=[],
            potential_implications=[],
            confidence_score=0.88,
            status="completed",
        )
        session.add(rep1_followup)
        await session.flush()

        ev1_followup, is_new1b, delta1b = await service.match_or_create_event(session, account_id, rep1_followup, category="technology")
        print(f"Follow-up Matched Event ID: {ev1_followup.id} (Expected {ev1.id}) | New: {is_new1b} | Delta: {delta1b}")
        assert ev1_followup.id == ev1.id, f"Expected match to Event {ev1.id}, got {ev1_followup.id}"
        assert is_new1b is False

        # Scenario 2: Unrelated ISRO development creating a separate event
        print("\n--- 2. Unrelated ISRO Rocket Launch Event ---")
        rep2 = ResearchReport(
            account_id=account_id,
            topic="ISRO Successfully Launches NVS-02 Navigation Satellite via GSLV-F12",
            executive_summary="ISRO launches the second-generation navigation satellite into geostationary transfer orbit from Sriharikota.",
            what_happened="GSLV-F12 lifted off from Satish Dhawan Space Centre carrying the 2,232 kg satellite.",
            key_entities=[{"name": "ISRO", "type": "organization", "role": "agency"}, {"name": "Sriharikota", "type": "location", "role": "launchpad"}],
            verified_facts=["Satellite deployed into GTO orbit"],
            unresolved_claims=[],
            conflicting_info=[],
            historical_context="NavIC constellation expansion.",
            stakeholder_analysis=[],
            potential_implications=[],
            confidence_score=0.95,
            status="completed",
        )
        session.add(rep2)
        await session.flush()

        ev2, is_new2, delta2 = await service.match_or_create_event(session, account_id, rep2, category="space")
        print(f"Event 2 ID: {ev2.id} (Expected != {ev1.id}) | New: {is_new2} | Title: {ev2.title}")
        assert ev2.id != ev1.id, f"ISRO wrongly merged with Tata semiconductor! 2={ev2.id}, 1={ev1.id}"
        assert is_new2 is True

        # Scenario 3: Two similar headlines referring to different underlying events
        print("\n--- 3. Similar Headlines Referring to Different Events ---")
        rep3a = ResearchReport(
            account_id=account_id,
            topic="Indian Railways Conducts High-Speed Trials on Mumbai-Ahmedabad Bullet Train Track",
            executive_summary="National High Speed Rail Corporation begins trial runs on the Surat-Bilimora section of the bullet train.",
            what_happened="Bullet train tracks tested at 320 km/h in Gujarat section.",
            key_entities=[{"name": "NHSRCL", "type": "organization", "role": "operator"}, {"name": "Bullet Train", "type": "transit", "role": "infrastructure"}],
            verified_facts=["High-speed trials conducted in Surat-Bilimora"],
            unresolved_claims=[],
            conflicting_info=[],
            historical_context="",
            stakeholder_analysis=[],
            potential_implications=[],
            confidence_score=0.85,
            status="completed",
        )
        session.add(rep3a)
        await session.flush()

        ev3a, is_new3a, delta3a = await service.match_or_create_event(session, account_id, rep3a, category="infrastructure")
        print(f"Event 3a ID: {ev3a.id} | Title: {ev3a.title}")

        rep3b = ResearchReport(
            account_id=account_id,
            topic="Indian Railways Inaugurates Dedicated Freight Corridor Connecting Ludhiana to Sonnagar",
            executive_summary="Dedicated Freight Corridor Corporation opens 400km eastern freight corridor to eliminate coal transit bottlenecks.",
            what_happened="Eastern Dedicated Freight Corridor freight operations commence.",
            key_entities=[{"name": "DFCCIL", "type": "organization", "role": "operator"}, {"name": "Freight Corridor", "type": "transit", "role": "freight"}],
            verified_facts=["Eastern DFC opened for commercial operations"],
            unresolved_claims=[],
            conflicting_info=[],
            historical_context="",
            stakeholder_analysis=[],
            potential_implications=[],
            confidence_score=0.85,
            status="completed",
        )
        session.add(rep3b)
        await session.flush()

        ev3b, is_new3b, delta3b = await service.match_or_create_event(session, account_id, rep3b, category="infrastructure")
        print(f"Event 3b ID: {ev3b.id} | Title: {ev3b.title} (Should be separate from Event 3a: {ev3a.id})")
        assert ev3a.id != ev3b.id, f"Wrongly merged two distinct railway projects! 3a={ev3a.id}, 3b={ev3b.id}"
        assert is_new3b is True

        # Scenario 4: Embedding Provider Failure Fallback
        print("\n--- 4. Embedding Provider Failure Fallback ---")
        broken_service = EventIntelligenceService(settings, embedder=BrokenEmbedder())
        rep4 = ResearchReport(
            account_id=account_id,
            topic="Tata Electronics Hires 2,000 Technicians for Dholera Fab Operations",
            executive_summary="Tata Electronics expands hiring in Gujarat for its Dholera semiconductor plant.",
            what_happened="Over 2,000 engineers and technicians recruited for upcoming wafer fabrication operations.",
            key_entities=[{"name": "Tata Electronics", "type": "company", "role": "builder"}, {"name": "Dholera", "type": "location", "role": "site"}],
            verified_facts=["2,000 technicians recruited for Dholera plant"],
            unresolved_claims=[],
            conflicting_info=[],
            historical_context="",
            stakeholder_analysis=[],
            potential_implications=[],
            confidence_score=0.85,
            status="completed",
        )
        session.add(rep4)
        await session.flush()

        ev4, is_new4, delta4 = await broken_service.match_or_create_event(session, account_id, rep4, category="technology")
        print(f"Fallback Event Match ID: {ev4.id} (Expected {ev1.id} via entity & keyword overlap) | New: {is_new4} | Delta: {delta4}")
        assert ev4.id == ev1.id, f"Fallback failed to match event {ev1.id}, got {ev4.id}"
        assert is_new4 is False

        # Scenario 5: Duplicate Development Ingestion
        print("\n--- 5. Duplicate Development Ingestion Prevention ---")
        ev5, is_new5, delta5 = await service.match_or_create_event(session, account_id, rep1_followup, category="technology")
        print(f"Duplicate Ingestion Result: Event ID={ev5.id}, is_new={is_new5}, Delta='{delta5}'")
        assert "Duplicate" in delta5 or delta5 == "no_significant_development", f"Expected duplicate detection, got '{delta5}'"

        # Chronological ordering & memory context verification
        context = await service.retrieve_event_memory_context(session, account_id, ev1.id)
        print(f"\nHistorical Memory Context for Event {ev1.id}:")
        print(f"Title: {context.get('event_title')}")
        print(f"Total Timeline Entries: {len(context.get('timeline', []))}")
        for t_item in context.get('timeline', []):
            print(f"  - {t_item}")

        prompt_block = service.format_memory_prompt_block(context)
        print(f"\nMemory Prompt Block Output:\n{prompt_block}")

        print("\nALL 5 AUDIT SCENARIOS PASSED WITH EXACT MATCHING ACCURACY!")

if __name__ == "__main__":
    asyncio.run(run_event_audit())
