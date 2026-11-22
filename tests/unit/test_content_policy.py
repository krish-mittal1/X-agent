from __future__ import annotations

from app.content_policy import is_political_content


def test_political_content_is_detected() -> None:
    assert is_political_content("BJP and Congress clash before Lok Sabha election")
    assert is_political_content("Prime Minister addresses Parliament")
    assert is_political_content("India signs bilateral defence treaty")
    assert is_political_content("Nepal PM arrives for first bilateral trip")
    assert is_political_content("US ambassador discusses a trilateral summit")
    assert is_political_content("MP attacks Meta during party protest")


def test_all_rounder_content_is_allowed() -> None:
    assert not is_political_content("India wins a dramatic cricket final")
    assert not is_political_content("Bengaluru startup launches a gaming handheld")
    assert not is_political_content("A viral dog interrupts a wedding dance")
