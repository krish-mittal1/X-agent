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
from app.memory.models import Draft, EditorialEvaluation

ADMIN_TOKEN = "test-cert-admin-token-12345"

async def run_auth_tenant_audit():
    print("=== TEST 2: AUTHENTICATION & TENANT ISOLATION AUDIT ===")
    
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
    
    # Setup test accounts & drafts in DB
    async with session_scope() as session:
        # Ensure account 1 (primary persona: ssh_bugs)
        acc1 = await session.execute(text("SELECT id FROM accounts WHERE handle = 'ssh_bugs'"))
        acc1_row = acc1.first()
        if not acc1_row:
            acc1_id = (await session.execute(
                text("INSERT INTO accounts (handle, is_active) VALUES ('ssh_bugs', true) RETURNING id")
            )).scalar_one()
        else:
            acc1_id = acc1_row[0]

        # Ensure account 2 (tenant 2: adversary_account)
        acc2 = await session.execute(text("SELECT id FROM accounts WHERE handle = 'adversary_account'"))
        acc2_row = acc2.first()
        if not acc2_row:
            acc2_id = (await session.execute(
                text("INSERT INTO accounts (handle, is_active) VALUES ('adversary_account', true) RETURNING id")
            )).scalar_one()
        else:
            acc2_id = acc2_row[0]

        import uuid
        run_uid = uuid.uuid4().hex[:8]
        # Insert draft for Account 1
        d1 = Draft(
            account_id=acc1_id,
            kind="original",
            status="staged",
            idempotency_key=f"cert-test-d1-key-{run_uid}",
            topic="Account 1 Topic",
            body="Account 1 post body",
            tone="analytical",
            policy_report={
                "alternative_angles": [
                    {"angle": "contrarian", "body": "Contrarian angle body text for cert test.", "rationale": "Different view."}
                ]
            }
        )
        session.add(d1)
        await session.flush()
        d1_id = d1.id

        # Insert evaluation for draft 1
        ev1 = EditorialEvaluation(
            draft_id=d1_id,
            overall_score=8.5,
            verdict="approved",
            critic_reasoning="Strong factual hook."
        )
        session.add(ev1)

        # Insert draft for Account 2 (Tenant 2)
        d2 = Draft(
            account_id=acc2_id,
            kind="original",
            status="staged",
            idempotency_key=f"cert-test-d2-key-{run_uid}",
            topic="Account 2 Private Topic",
            body="Account 2 private post body",
            tone="analytical",
            policy_report={
                "alternative_angles": [
                    {"angle": "contrarian", "body": "Adversary contrarian text.", "rationale": "Adversary rationale"}
                ]
            }
        )
        session.add(d2)
        await session.flush()
        d2_id = d2.id

        await session.commit()

    print(f"Test Fixture Ready: Account 1 (id={acc1_id}, draft={d1_id}), Account 2 (id={acc2_id}, draft={d2_id})")

    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    bad_headers = {"Authorization": "Bearer wrong-unauthorized-token"}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 2A. Unauthenticated Requests (Expect 401)
        r_eval_noauth = await client.get(f"/api/editorial/evaluations/{d1_id}")
        print(f"GET /api/editorial/evaluations/{d1_id} (No Auth): {r_eval_noauth.status_code}")
        assert r_eval_noauth.status_code == 401

        r_switch_noauth = await client.post("/api/editorial/switch-angle", json={"draft_id": d1_id, "target_angle": "contrarian"})
        print(f"POST /api/editorial/switch-angle (No Auth): {r_switch_noauth.status_code}")
        assert r_switch_noauth.status_code == 401

        r_insights_noauth = await client.get("/api/analytics/performance/insights")
        print(f"GET /api/analytics/performance/insights (No Auth): {r_insights_noauth.status_code}")
        assert r_insights_noauth.status_code == 401

        # 2B. Bad Credentials (Expect 403)
        r_eval_bad = await client.get(f"/api/editorial/evaluations/{d1_id}", headers=bad_headers)
        print(f"GET /api/editorial/evaluations/{d1_id} (Bad Token): {r_eval_bad.status_code}")
        assert r_eval_bad.status_code == 403

        r_switch_bad = await client.post("/api/editorial/switch-angle", headers=bad_headers, json={"draft_id": d1_id, "target_angle": "contrarian"})
        print(f"POST /api/editorial/switch-angle (Bad Token): {r_switch_bad.status_code}")
        assert r_switch_bad.status_code == 403

        r_insights_bad = await client.get("/api/analytics/performance/insights", headers=bad_headers)
        print(f"GET /api/analytics/performance/insights (Bad Token): {r_insights_bad.status_code}")
        assert r_insights_bad.status_code == 403

        # 2C. Valid Admin Auth on Own Draft (Expect 200)
        r_eval_ok = await client.get(f"/api/editorial/evaluations/{d1_id}", headers=admin_headers)
        print(f"GET /api/editorial/evaluations/{d1_id} (Admin Auth): {r_eval_ok.status_code}")
        assert r_eval_ok.status_code == 200
        assert r_eval_ok.json()["draft_id"] == d1_id
        assert r_eval_ok.json()["evaluation"]["overall_score"] == 8.5

        # 2D. Tenant Isolation: Account 1 accessing Account 2's Draft (Expect 404)
        r_eval_acc2 = await client.get(f"/api/editorial/evaluations/{d2_id}", headers=admin_headers)
        print(f"GET /api/editorial/evaluations/{d2_id} (Account 1 trying to read Account 2): {r_eval_acc2.status_code}")
        assert r_eval_acc2.status_code == 404, f"Expected 404 tenant isolation, got {r_eval_acc2.status_code}"

        r_switch_acc2 = await client.post(
            "/api/editorial/switch-angle",
            headers=admin_headers,
            json={"draft_id": d2_id, "target_angle": "contrarian"}
        )
        print(f"POST /api/editorial/switch-angle (Account 1 trying to mutate Account 2): {r_switch_acc2.status_code}")
        assert r_switch_acc2.status_code == 404, f"Expected 404 tenant isolation, got {r_switch_acc2.status_code}"

        # 2E. Non-Existent Draft ID (Expect 404)
        r_eval_nonexistent = await client.get("/api/editorial/evaluations/999999", headers=admin_headers)
        print(f"GET /api/editorial/evaluations/999999: {r_eval_nonexistent.status_code}")
        assert r_eval_nonexistent.status_code == 404

        # 2F. Performance Insights with Admin Auth (Expect 200)
        r_insights_ok = await client.get("/api/analytics/performance/insights", headers=admin_headers)
        print(f"GET /api/analytics/performance/insights (Admin Auth): {r_insights_ok.status_code}")
        assert r_insights_ok.status_code == 200

        # 2G. Route Security Inventory across all FastAPI routes
        print("\n--- Route Security Inventory ---")
        unprotected_api_routes = []
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", set())
            endpoint = getattr(route, "endpoint", None)
            dependencies = getattr(route, "dependencies", [])
            # Check if AdminAuth is in dependencies
            has_auth = any("require_admin" in str(dep) for dep in dependencies)
            if endpoint:
                import inspect
                sig = inspect.signature(endpoint)
                for param in sig.parameters.values():
                    if "require_admin" in str(param.default) or "AdminAuth" in str(param.annotation):
                        has_auth = True
            
            is_monitoring = path in ["/health", "/ready", "/metrics", "/docs", "/openapi.json"] or (path and path.startswith("/dashboard"))
            status_desc = "AUTHENTICATED (AdminAuth)" if has_auth else ("PUBLIC (Monitoring/UI)" if is_monitoring else "UNPROTECTED API")
            if not has_auth and not is_monitoring and path and path.startswith("/api"):
                unprotected_api_routes.append(path)
            if path and not path.startswith("/static"):
                print(f"[{','.join(methods)}] {path:40} -> {status_desc}")

        print(f"Unprotected API routes found: {len(unprotected_api_routes)}")
        assert len(unprotected_api_routes) == 0, f"Unprotected API routes: {unprotected_api_routes}"

    print("\nTEST 2 RESULT: PASS (Strict Authentication & Multi-Tenant Isolation Verified)")

if __name__ == "__main__":
    asyncio.run(run_auth_tenant_audit())
