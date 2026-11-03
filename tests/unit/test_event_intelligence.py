from __future__ import annotations

import pytest

from app.config import Settings
from app.research.event_service import (
    EventIntelligenceService,
    _cosine_similarity,
    _normalize_name,
)


def test_cosine_similarity_identifies_identical_vectors() -> None:
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    assert _cosine_similarity(v1, v2) == pytest.approx(1.0)

    v3 = [0.0, 1.0, 0.0]
    assert _cosine_similarity(v1, v3) == pytest.approx(0.0)

    assert _cosine_similarity(None, v1) == 0.0
    assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0


def test_normalize_name_removes_punctuation_and_casing() -> None:
    assert _normalize_name("Tata Electronics Pvt. Ltd.") == "tataelectronicspvtltd"
    assert _normalize_name("Govt. of India (MeitY)") == "govtofindiameity"


def test_format_memory_prompt_block_creates_clear_injection() -> None:
    settings = Settings(_env_file=None)
    service = EventIntelligenceService(settings)
    mem = {
        "event_title": "Dholera Semiconductor Fab",
        "active_since": "2026-03-01",
        "timeline": [
            "• [01 Mar 10:00] (INCEPTION) Groundbreaking ceremony held",
            "• [02 Nov 14:00] (ESCALATION) Equipment import clearance received",
        ],
        "previous_posts": [
            "• \"India's chip dream isn't about hype, it's about whether Dholera can deliver clean water...\"",
        ],
        "previous_angles": ["analytical (cynical_supply_chain)"],
    }
    block = service.format_memory_prompt_block(mem)
    assert "EVENT INTELLIGENCE & TIMELINE MEMORY" in block
    assert "Dholera Semiconductor Fab" in block
    assert "Groundbreaking ceremony held" in block
    assert "DO NOT repeat the same angle" in block

