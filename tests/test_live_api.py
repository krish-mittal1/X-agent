import httpx
import pytest

from app.config import get_settings


def test_endpoints():
    try:
        with httpx.Client(base_url="http://localhost:8000", timeout=2.0) as check_client:
            check_client.get("/health")
    except Exception:
        pytest.skip("Live server on http://localhost:8000 is not running; skipping live API test.")

    settings = get_settings()
    token = settings.admin_api_token.get_secret_value() if settings.admin_api_token else "test_admin_token"
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url="http://localhost:8000", timeout=15.0) as client:
        # Health check
        r_health = client.get("/health")
        print("Health:", r_health.status_code, r_health.json())
        assert r_health.status_code == 200

        # Ready check
        r_ready = client.get("/ready")
        print("Ready:", r_ready.status_code, r_ready.json())
        assert r_ready.status_code == 200

        # Review queue drafts
        r_drafts = client.get("/drafts?limit=5", headers=headers)
        print("Drafts status:", r_drafts.status_code)
        assert r_drafts.status_code == 200
        drafts_data = r_drafts.json()
        drafts = drafts_data.get("items", [])
        print(f"Total drafts returned: {len(drafts)}")

        if drafts:
            d = drafts[0]
            draft_id = d["id"]
            print("Draft sample:", {
                "id": draft_id,
                "format": d.get("format"),
                "quality_score": d.get("quality_score"),
                "critic_verdict": d.get("critic_verdict"),
                "alternative_angles_count": d.get("alternative_angles_count"),
                "event_title": d.get("event_title"),
            })

            # Editorial evaluation endpoint
            r_eval = client.get(f"/api/editorial/evaluations/{draft_id}", headers=headers)
            print(f"Evaluation for draft {draft_id}: status={r_eval.status_code}")
            assert r_eval.status_code == 200
            eval_data = r_eval.json()
            print("Evaluation payload keys:", list(eval_data.keys()))
            print("Critic verdict:", eval_data.get("evaluation", {}).get("verdict"))
            print("Critic overall_score:", eval_data.get("evaluation", {}).get("overall_score"))
            print("Alternatives count:", len(eval_data.get("alternative_angles", [])))

        # Performance insights endpoint
        r_perf = client.get("/api/analytics/performance/insights", headers=headers)
        print("Performance insights: status=", r_perf.status_code)
        assert r_perf.status_code == 200
        perf_data = r_perf.json()
        print("Performance insights summary:", {
            "critic_approval_rate": perf_data.get("critic_approval_rate"),
            "top_performing_angles": list((perf_data.get("angle_performance") or {}).keys()),
            "insights_count": len(perf_data.get("insights", [])),
        })

if __name__ == "__main__":
    test_endpoints()
