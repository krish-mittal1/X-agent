from __future__ import annotations

from app.config import Settings
from app.growth.commentary import assess_originality


def test_repeated_generic_opening_is_held_for_review() -> None:
    report = assess_originality(
        text="Absolute madness what this launch says about Bengaluru rent.",
        settings=Settings(),
        source_headline="New startup launch draws attention",
    )
    assert not report.passed
    assert any("generic opening template" in issue for issue in report.issues)


def test_operator_written_draft_keeps_manual_override() -> None:
    report = assess_originality(
        text="Absolute madness what this launch says about Bengaluru rent.",
        settings=Settings(),
        source_headline="New startup launch draws attention",
        is_operator=True,
    )
    assert report.passed
