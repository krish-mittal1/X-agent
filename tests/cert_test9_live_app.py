import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

BASE_URL = "http://127.0.0.1:8000"

def run_live_daemon_audit():
    print("=== TEST 9: LIVE APPLICATION & DAEMON AUDIT ===")
    
    with httpx.Client(base_url=BASE_URL, timeout=5.0) as client:
        # 1. Health check
        r_health = client.get("/health")
        print(f"GET /health: {r_health.status_code} -> {r_health.json()}")
        assert r_health.status_code == 200
        assert r_health.json().get("status") == "ok"

        # 2. Ready probe check (Strict Staging Lockdown Verification)
        r_ready = client.get("/ready")
        print(f"GET /ready: {r_ready.status_code} -> {r_ready.json()}")
        assert r_ready.status_code == 200
        ready_data = r_ready.json()
        assert ready_data.get("status") == "ready"
        pub = ready_data.get("checks", {}).get("publishing", {})
        print(f"   Publishing configuration reported by live daemon: {pub}")
        assert pub.get("mode") == "local_draft", f"Expected mode=local_draft, got {pub.get('mode')}"
        assert "enabled=False" in pub.get("detail", ""), f"Expected enabled=False in detail, got {pub.get('detail')}"
        assert "dry_run=True" in pub.get("detail", ""), f"Expected dry_run=True in detail, got {pub.get('detail')}"
        assert pub.get("outbound_allowed") is False, f"Expected outbound_allowed=false, got {pub.get('outbound_allowed')}"

        # 3. Unauthenticated access to /drafts
        r_drafts = client.get("/drafts")
        print(f"GET /drafts (no auth): {r_drafts.status_code} (detail: {r_drafts.json().get('detail')})")
        # Admin auth protects /drafts or 503 if token unset
        assert r_drafts.status_code in (401, 503)

        # 4. Editorial evaluation without auth
        r_eval = client.get("/api/editorial/evaluations/1")
        print(f"GET /api/editorial/evaluations/1 (no auth): {r_eval.status_code} (detail: {r_eval.json().get('detail')})")
        assert r_eval.status_code in (401, 503)

        # 5. Analytics performance insights without auth
        r_insights = client.get("/api/analytics/performance/insights")
        print(f"GET /api/analytics/performance/insights (no auth): {r_insights.status_code} (detail: {r_insights.json().get('detail')})")
        assert r_insights.status_code in (401, 503)

    print("\nTEST 9 RESULT: PASS (Live daemon verified active on port 8000, /ready probe strictly locks publishing)")

if __name__ == "__main__":
    run_live_daemon_audit()
