from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.memory.models import SourceDocument
from app.research.scoring import CandidateScorer, freshness_signal
from app.schemas.research import CandidateClassification


def make_settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {"_env_file": None, "freshness_half_life_hours": 24.0}
    values.update(overrides)
    return Settings(**values)


def make_document(**overrides) -> SourceDocument:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    values = {
        "account_id": 1,
        "source_type": "github",
        "source_identifier": "acme/widget",
        "url": "https://github.com/acme/widget/releases/tag/v2",
        "url_hash": "hash",
        "canonical_url": "https://github.com/acme/widget/releases/tag/v2",
        "title": "widget v2: faster inference for coding agents",
        "content": (
            "Adds a paged KV cache. Benchmark shows 1,200 tokens/s on one CPU core. "
            "The release also deprecates the old scheduler API and documents migration."
        ),
        "content_hash": "hash",
        "excerpt": "Adds a paged KV cache and improves throughput.",
        "published_at": now - timedelta(hours=24),
        "retrieved_at": now,
        "reliability_score": 0.85,
        "metadata_": {"is_primary_source": True},
    }
    values.update(overrides)
    return SourceDocument(**values)


def test_freshness_decay_half_life() -> None:
    now = datetime.now(UTC)
    assert freshness_signal(now, now, 24) == 1.0
    assert freshness_signal(now - timedelta(hours=24), now, 24) == pytest.approx(0.3679, abs=0.001)
    assert freshness_signal(now - timedelta(hours=240), now, 24) < 0.001
    assert freshness_signal(None, now, 24) == 0.4
    assert freshness_signal(now + timedelta(hours=2), now, 24) == 1.0


def test_score_is_bounded_and_explained() -> None:
    scorer = CandidateScorer(make_settings())
    score = scorer.score_document(
        make_document(),
        interests=("ai engineering", "developer tools", "open source"),
        novelty=0.8,
    )
    assert 0.0 <= score.score <= 1.0
    assert score.signals.novelty == 0.8
    assert pytest.approx(sum(score.weights.values())) == 1.0
    assert len(score.explanation) >= 5
    assert any("weighted sum" in line for line in score.explanation)


def test_primary_source_scores_higher_evidence_quality() -> None:
    scorer = CandidateScorer(make_settings())
    primary = scorer.score_document(
        make_document(), interests=("ai engineering",), novelty=0.5
    )
    secondary = scorer.score_document(
        make_document(
            source_type="rss",
            metadata_={"is_primary_source": False},
            reliability_score=0.5,
        ),
        interests=("ai engineering",),
        novelty=0.5,
    )
    assert primary.signals.evidence_quality > secondary.signals.evidence_quality
    assert primary.score > secondary.score


def test_marketing_fluff_is_penalised() -> None:
    scorer = CandidateScorer(make_settings())
    fluff = scorer.score_document(
        make_document(
            title="Revolutionary game-changing AI will supercharge your workflow",
            content="Unleash effortless transformation with our cutting-edge 10x platform. "
            "World-class seamless innovation.",
            excerpt="Unleash the future.",
        ),
        interests=("ai engineering",),
        novelty=0.5,
    )
    concrete = scorer.score_document(
        make_document(), interests=("ai engineering",), novelty=0.5
    )
    assert concrete.signals.audience_usefulness > fluff.signals.audience_usefulness


def test_missing_novelty_is_neutral_and_documented() -> None:
    scorer = CandidateScorer(make_settings())
    score = scorer.score_document(make_document(), interests=(), novelty=None)
    assert score.signals.novelty == 0.5
    assert any("neutral prior" in line for line in score.explanation)


def test_llm_refinement_blends_and_persists_reasoning() -> None:
    scorer = CandidateScorer(make_settings())
    refinement = CandidateClassification(
        technical_relevance=1.0,
        audience_usefulness=1.0,
        reasoning="Concrete benchmark and migration path.",
        is_marketing_fluff=False,
    )
    refined = scorer.score_document(
        make_document(), interests=("ai engineering",), novelty=0.5, refinement=refinement
    )
    assert refined.llm_refinement is not None
    assert refined.llm_refinement["reasoning"].startswith("Concrete benchmark")
    assert any("llm refinement" in line for line in refined.explanation)
    assert 0 <= refined.score <= 1


def test_marketing_fluff_refinement_applies_penalty() -> None:
    scorer = CandidateScorer(make_settings())
    fluff = CandidateClassification(
        technical_relevance=0.1,
        audience_usefulness=0.1,
        reasoning="Mostly promotional.",
        is_marketing_fluff=True,
    )
    plain = scorer.score_document(make_document(), interests=("ai engineering",), novelty=0.5)
    refined = scorer.score_document(
        make_document(), interests=("ai engineering",), novelty=0.5, refinement=fluff
    )
    assert refined.score < plain.score
    assert refined.signals.source_reliability == plain.signals.source_reliability
    assert any("marketing-fluff" in line for line in refined.explanation)


def test_stale_items_rank_low() -> None:
    scorer = CandidateScorer(make_settings())
    now = datetime.now(UTC)
    stale = scorer.score_document(
        make_document(published_at=now - timedelta(days=30)),
        interests=("ai engineering",),
        novelty=0.5,
        now=now,
    )
    fresh = scorer.score_document(make_document(), interests=("ai engineering",), novelty=0.5, now=now)
    assert stale.score < fresh.score
