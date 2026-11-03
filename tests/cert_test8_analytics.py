import asyncio
import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text

from app.analytics.performance import PerformanceAnalyticsService
from app.config import Settings
from app.memory.database import init_engine, session_scope


async def run_analytics_truth_audit():
    print("=== TEST 8: ANALYTICS TRUTHFULNESS & FABRICATION AUDIT ===")
    settings = Settings(x_publishing_mode="local_draft", publishing_enabled=False, dry_run=True)
    init_engine(settings)

    async with session_scope() as session:
        # 1. Query for an account with zero published posts (Account 2: adversary_account)
        acc2_id = (await session.execute(text("SELECT id FROM accounts WHERE handle = 'adversary_account'"))).scalar_one()
        service = PerformanceAnalyticsService(session)
        report_empty = await service.generate_report(account_id=acc2_id, days=30)
        
        print("8A. Report on Account with 0 posts:")
        print(f"    total_posts_analyzed: {report_empty.total_posts_analyzed}")
        print(f"    total_impressions: {report_empty.total_impressions}")
        print(f"    insufficient_data: {report_empty.insufficient_data}")
        print(f"    insights count: {len(report_empty.insights)}")

        assert report_empty.total_posts_analyzed == 0
        assert report_empty.total_impressions == 0
        assert report_empty.total_engagements == 0
        assert report_empty.insufficient_data is True
        assert len(report_empty.insights) == 0, "Should not fabricate insights when insufficient data!"

        # 2. Check post_metrics in database for any unverified/synthetic numbers
        metrics_rows = (await session.execute(text("SELECT id, post_id, impressions, likes, reposts, replies, bookmarks FROM post_metrics"))).all()
        print(f"\n8B. Checking {len(metrics_rows)} post_metrics rows for engagement metrics integrity:")
        for m in metrics_rows:
            print(f"    Metric ID {m.id} (post={m.post_id}): imp={m.impressions}, likes={m.likes}, reposts={m.reposts}")
            # Ensure no hardcoded synthetic 120 impressions or 6 likes
            assert not (m.impressions == 120 and m.likes == 6), f"Metric {m.id} has synthetic engagement constants!"

    print("\nTEST 8 RESULT: PASS (Truthful analytics verified, zero-data properly reports insufficient_data, no synthetic engagement)")

if __name__ == "__main__":
    asyncio.run(run_analytics_truth_audit())
