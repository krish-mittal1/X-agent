import asyncio
import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from app.memory.database import init_engine, session_scope
from app.memory.models import Draft, Post, ResearchReport, ResearchSource, SourceDocument

ADMIN_TOKEN = "test-cert-admin-token-12345"

async def run_angle_switching_audit():
    print("=== TEST 5: ANGLE SWITCHING ADVERSARIAL AUDIT ===")
    
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
        admin_api_token=SecretStr(ADMIN_TOKEN),
        x_persona_handle="ssh_bugs",
    )
    init_engine(settings)
    app = create_app(settings, start_scheduler=False)
    app.state.settings = settings
    transport = ASGITransport(app=app)
    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    async with session_scope() as session:
        # Get account
        acc = (await session.execute(text("SELECT id FROM accounts WHERE handle = 'ssh_bugs'"))).scalar_one()

        # Seed a recent post for duplicate checking
        import uuid
        run_uid = uuid.uuid4().hex[:6]
        p = Post(
            account_id=acc,
            x_post_id=f"post-dup-{run_uid}",
            text="Recent published coverage on railway battery safety rules in India.",
            published_at=text("NOW()"),
        )
        session.add(p)

        # Seed SourceDocument for evidence
        import hashlib
        u_str = f"https://valid-tech-news.org/article-{run_uid}"
        u_hash = hashlib.sha256(u_str.encode()).hexdigest()
        c_text = "Engineers deploy 500 megawatt grid battery storage system across Maharashtra."
        c_hash = hashlib.sha256(c_text.encode()).hexdigest()
        src = SourceDocument(
            account_id=acc,
            source_identifier=f"valid-tech-news-{run_uid}",
            url=u_str,
            url_hash=u_hash,
            canonical_url=u_str,
            title="Clean Energy Battery Storage Breakthrough",
            content=c_text,
            content_hash=c_hash,
            source_type="web",
            reliability_score=0.95,
        )
        session.add(src)
        await session.flush()
        src_id = src.id

        # Seed Draft with 5 different angles:
        # 1. 'analytical': current draft body
        # 2. 'valid_contrarian': valid text, supported claim
        # 3. 'duplicate_angle': identical to recent post
        # 4. 'rule_violation': contains prohibited hashtag #viral
        # 5. 'unsupported_number': claims unverified '9999 gigawatts' financial figure
        # 6. 'contradiction_angle': contradicts report's conflicting claims
        
        rep = ResearchReport(
            account_id=acc,
            topic="Grid Battery Storage 2026",
            executive_summary="Maharashtra deploys 500 MW battery storage.",
            what_happened="Deployment completed.",
            confidence_score=0.9,
            conflicting_info=[
                {
                    "claim": "Maharashtra deploys 9000 megawatt battery storage",
                    "contradiction_detail": "Official capacity is only 500 MW."
                }
            ]
        )
        session.add(rep)
        await session.flush()
        rep_id = rep.id

        rs = ResearchSource(
            report_id=rep_id,
            url="https://valid-tech-news.org/article-1",
            title="Clean Energy Battery Storage Breakthrough",
            source_type="news",
            reliability_score=0.95,
            content_excerpt=c_text,
        )
        session.add(rs)
        await session.flush()

        orig_body = "Maharashtra launches its first 500 MW grid battery system to prevent summer outages."
        d = Draft(
            account_id=acc,
            kind="original",
            status="staged",
            idempotency_key=f"cert-test-angle-switch-draft-{run_uid}",
            topic="Battery Storage",
            body=orig_body,
            tone="analytical",
            source_document_ids=[src_id],
            policy_report={
                "research_report_id": rep_id,
                "alternative_angles": [
                    {
                        "angle": "contrarian",
                        "parts": ["Maharashtra's 500 MW grid battery avoids summer outages, but tariff structures remain unsettled."],
                        "rationale": "Focuses on economic friction.",
                        "claims": [
                            {
                                "claim": "Engineers deploy 500 megawatt grid battery storage system across Maharashtra.",
                                "claim_type": "fact",
                                "supporting_quote": "Engineers deploy 500 megawatt grid battery storage system across Maharashtra.",
                                "source_url": "https://valid-tech-news.org/article-1"
                            }
                        ]
                    },
                    {
                        "angle": "duplicate_angle",
                        "parts": ["Recent published coverage on railway battery safety rules in India."],
                        "rationale": "Duplicate text test.",
                        "claims": []
                    },
                    {
                        "angle": "banned_hashtag",
                        "parts": ["Check out this big battery project in India! #viral #trending #battery"],
                        "rationale": "Hashtag violation test.",
                        "claims": []
                    },
                    {
                        "angle": "unsupported_number",
                        "parts": ["Maharashtra's battery system reaches 9999 gigawatts with ₹99,000 crore private funding."],
                        "rationale": "Unsupported numerical claim test.",
                        "claims": [
                            {
                                "claim": "Maharashtra battery reaches 9999 gigawatts with ₹99,000 crore funding.",
                                "claim_type": "fact",
                                "supporting_quote": "9999 gigawatts with ₹99,000 crore funding.",
                                "source_url": "https://valid-tech-news.org/article-1"
                            }
                        ]
                    },
                    {
                        "angle": "contradiction_angle",
                        "parts": ["Maharashtra deploys 9000 megawatt battery storage according to conflicting reports."],
                        "rationale": "Contradiction test.",
                        "claims": [
                            {
                                "claim": "Maharashtra deploys 9000 megawatt battery storage according to conflicting reports.",
                                "claim_type": "fact",
                                "supporting_quote": "Maharashtra deploys 9000 megawatt battery storage",
                                "source_url": "https://valid-tech-news.org/article-1"
                            }
                        ]
                    }
                ]
            }
        )
        session.add(d)
        await session.commit()
        draft_id = d.id

    print(f"Test Draft created: id={draft_id}, initial tone='analytical'")

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Test 5A: Adversarial Case 1 - Duplicate text angle (Expect HTTP 400)
        r_dup = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": draft_id, "target_angle": "duplicate_angle"}
        )
        print(f"5A. Switch to duplicate angle: {r_dup.status_code} ({r_dup.json().get('detail', '')})")
        assert r_dup.status_code == 400
        assert "duplicate" in r_dup.json().get("detail", "").lower()

        # Test 5B: Adversarial Case 2 - Banned hashtag violation (Expect HTTP 400)
        r_rules = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": draft_id, "target_angle": "banned_hashtag"}
        )
        print(f"5B. Switch to banned hashtag angle: {r_rules.status_code} ({r_rules.json().get('detail', '')})")
        assert r_rules.status_code == 400
        assert "text rules" in r_rules.json().get("detail", "").lower() or "hashtag" in r_rules.json().get("detail", "").lower()

        # Test 5C: Adversarial Case 3 - Unsupported numerical figure (Expect HTTP 400)
        r_num = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": draft_id, "target_angle": "unsupported_number"}
        )
        print(f"5C. Switch to unsupported number angle: {r_num.status_code} ({r_num.json().get('detail', '')})")
        assert r_num.status_code == 400
        assert "claim verification" in r_num.json().get("detail", "").lower()

        # Test 5D: Adversarial Case 4 - Contradictory claim (Expect HTTP 400)
        r_contra = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": draft_id, "target_angle": "contradiction_angle"}
        )
        print(f"5D. Switch to contradictory angle: {r_contra.status_code} ({r_contra.json().get('detail', '')})")
        assert r_contra.status_code == 400
        assert "claim verification" in r_contra.json().get("detail", "").lower()

        # Test 5E: Verify draft remained UNCHANGED after all failed attacks
        async with session_scope() as session:
            current_draft = await session.get(Draft, draft_id)
            print(f"5E. Integrity check after attacks: tone='{current_draft.tone}', body='{current_draft.body[:60]}...'")
            assert current_draft.tone == "analytical"
            assert current_draft.body == orig_body

        # Test 5F: Legitimate Case - Valid angle switch with ResearchReport (Expect HTTP 200)
        r_valid = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": draft_id, "target_angle": "contrarian"}
        )
        print(f"5F. Switch to valid contrarian angle: {r_valid.status_code}")
        assert r_valid.status_code == 200
        body_json = r_valid.json()
        assert body_json["new_angle"] == "contrarian"
        assert "500 MW grid battery" in body_json["new_body"]
        assert body_json["verification_passed"] is True

        # Verify DB persisted new angle and moved old angle to alternatives
        async with session_scope() as session:
            updated_draft = await session.get(Draft, draft_id)
            print(f"5F. Persisted state: tone='{updated_draft.tone}', body='{updated_draft.body[:60]}...'")
            assert updated_draft.tone == "contrarian"
            assert "tariff structures remain unsettled" in updated_draft.body
            alts = (updated_draft.policy_report or {}).get("alternative_angles", [])
            angles_in_alts = [a.get("angle") for a in alts]
            print(f"   Alternatives now contain previous angle: 'analytical' in {angles_in_alts}")
            assert "analytical" in angles_in_alts

        # Test 5G: Draft WITHOUT a ResearchReport (Expect HTTP 200)
        async with session_scope() as session:
            d_norep = Draft(
                account_id=acc,
                kind="original",
                status="staged",
                idempotency_key=f"cert-test-norep-{run_uid}",
                topic="Standalone Topic",
                body="Original standalone text without research report.",
                tone="analytical",
                source_document_ids=[],
                policy_report={
                    "alternative_angles": [
                        {
                            "angle": "contrarian",
                            "parts": ["Contrarian standalone text without research report."],
                            "rationale": "Standalone rationale.",
                            "claims": []
                        }
                    ]
                }
            )
            session.add(d_norep)
            await session.commit()
            norep_draft_id = d_norep.id

        r_norep = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": norep_draft_id, "target_angle": "contrarian"}
        )
        print(f"5G. Switch angle without ResearchReport: {r_norep.status_code}")
        assert r_norep.status_code == 200
        assert r_norep.json()["new_angle"] == "contrarian"

    print("\nTEST 5 RESULT: PASS (All adversarial angles blocked, rollback verified, valid switches with & without report succeed)")

if __name__ == "__main__":
    asyncio.run(run_angle_switching_audit())
