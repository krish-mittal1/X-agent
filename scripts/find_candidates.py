import sys
sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
from sqlalchemy import text
from app.memory.database import init_engine, session_scope
from app.config import get_settings

async def find_diverse_candidates():
    settings = get_settings()
    init_engine(settings)
    queries = {
        'Technology': ['tech', 'ai', 'semiconductor', 'software', 'chip', 'cyber', 'data'],
        'Business & Economy': ['rbi', 'sebi', 'market', 'economy', 'budget', 'gdp', 'inflation', 'stock', 'startup', 'crore'],
        'Geopolitics': ['china', 'us', 'diplomacy', 'pakistan', 'defence', 'border', 'un', 'foreign', 'minister'],
        'Science': ['isro', 'space', 'satellite', 'quantum', 'research', 'scientific', 'energy'],
        'Infrastructure': ['railway', 'expressway', 'metro', 'freight', 'airport', 'highway', 'power', 'grid']
    }
    candidates_by_cat = {}
    async with session_scope() as session:
        for cat, kws in queries.items():
            print(f'=== Category: {cat} ===')
            like_clauses = ' OR '.join([f"lower(title) LIKE '%{kw}%'" for kw in kws])
            query = f"SELECT id, title, source_type, candidate_score FROM source_documents WHERE ({like_clauses}) AND candidate_score IS NOT NULL ORDER BY candidate_score DESC LIMIT 6"
            rows = (await session.execute(text(query))).fetchall()
            candidates_by_cat[cat] = [r[0] for r in rows]
            for r in rows:
                print(f' [{r[0]}] score={r[3]:.3f} | {r[1][:70]}')
    return candidates_by_cat

if __name__ == '__main__':
    asyncio.run(find_diverse_candidates())
