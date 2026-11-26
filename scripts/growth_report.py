"""Read observed public metrics without treating missing values as zero."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.memory.database import dispose_engine, get_session_factory, init_engine
from scripts._common import prepare


async def main() -> None:
    init_engine(prepare())
    try:
        async with get_session_factory()() as session:
            rows = await session.execute(text("""
                SELECT DISTINCT ON (a.details->>'x_post_id')
                    a.details->>'x_post_id' AS x_post_id,
                    a.details->'observed_counts' AS observed_counts,
                    a.details->'missing_fields' AS missing_fields,
                    a.created_at AS observed_at, p.published_at, p.kind,
                    d.policy_report->>'format' AS format,
                    EXTRACT(EPOCH FROM (a.created_at-p.published_at))/3600 AS age_hours
                FROM action_logs a
                JOIN posts p ON p.id=(a.details->>'post_id')::bigint
                LEFT JOIN drafts d ON d.id=p.draft_id
                WHERE a.action='analytics.metrics_observed'
                ORDER BY a.details->>'x_post_id', a.created_at DESC
            """))
            posts = [dict(row) for row in rows.mappings()]
            for post in posts:
                age = float(post.get("age_hours") or 0)
                post["checkpoint"] = (
                    "1h" if age < 2 else
                    "24h" if age < 36 else
                    "48h" if age < 72 else
                    "later"
                )
            print(json.dumps({
                "semantics": "public counts; not unique reach or attributable follows",
                "checkpoints": {
                    checkpoint: sum(1 for post in posts if post["checkpoint"] == checkpoint)
                    for checkpoint in ("1h", "24h", "48h", "later")
                },
                "posts": posts,
            }, default=str, indent=2))
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
