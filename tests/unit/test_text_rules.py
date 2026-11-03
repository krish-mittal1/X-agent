from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.publishing.text_rules import (
    find_duplicate,
    jaccard_similarity,
    normalize_typography,
    validate_text_rules,
    x_weighted_length,
)


def make_settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    values = {"_env_file": None, "max_post_length": 280}
    values.update(overrides)
    return Settings(**values)


def test_url_counts_as_23_characters() -> None:
    url = "https://example.com/a/very/long/path/that/would/be/way/too/long/otherwise"
    assert x_weighted_length(url) == 23
    assert x_weighted_length(f"see {url} now") == 23 + len("see  now")


def test_wide_characters_count_double() -> None:
    assert x_weighted_length("你好") == 4
    assert x_weighted_length("ok") == 2


def test_over_length_part_is_rejected() -> None:
    report = validate_text_rules(make_settings(max_post_length=20), ["x" * 21])
    assert report.passed is False
    assert any("limit is 20" in issue for issue in report.issues)


def test_banned_phrase_and_first_person_experience_blocked() -> None:
    settings = make_settings()
    forbidden = validate_text_rules(settings, ["Follow for more inference tips"])
    assert forbidden.passed is False
    experience = validate_text_rules(settings, ["I tested the new inference engine on my GPU."])
    assert experience.passed is False
    assert any("personal experience" in issue for issue in experience.issues)


def test_non_https_link_and_hashtag_limits() -> None:
    settings = make_settings(max_links_per_post=1, max_hashtags_per_post=1)
    insecure = validate_text_rules(settings, ["new release http://example.com/post"])
    assert insecure.passed is False
    hashtags = validate_text_rules(settings, ["#ai #ml #dev new release notes"])
    assert hashtags.passed is False


def test_valid_post_passes() -> None:
    candidate = (
        "pgvector 0.8 adds iterative index scans, which fixes recall loss on filtered "
        "queries. Worth re-running your benchmarks before switching indexes. "
        "https://github.com/pgvector/pgvector/releases"
    )
    report = validate_text_rules(make_settings(), [candidate])
    assert report.passed is True
    assert report.links == 1
    assert report.weighted_length <= 280


def test_thread_part_limit() -> None:
    report = validate_text_rules(make_settings(max_thread_parts=2), ["a", "b", "c"])
    assert report.passed is False
    assert any("maximum is 2" in issue for issue in report.issues)


def test_jaccard_ignores_stopwords_and_short_tokens() -> None:
    assert jaccard_similarity("the new inference engine", "the new inference engine") == 1.0
    assert jaccard_similarity("new inference engine", "completely different topic") == 0.0


def test_duplicate_detection_by_embedding_and_tokens() -> None:
    settings = make_settings(duplicate_similarity_threshold=0.9, duplicate_jaccard_threshold=0.8)
    text = "paged kv cache reduces memory fragmentation during long context inference"
    by_tokens = find_duplicate(
        text=text,
        recent_texts=[text],
        settings=settings,
        embedding_similarity=None,
    )
    assert by_tokens.duplicate is True

    by_embedding = find_duplicate(
        text="something unrelated entirely",
        recent_texts=[],
        settings=settings,
        embedding_similarity=0.97,
    )
    assert by_embedding.duplicate is True
    assert any("embedding similarity" in reason for reason in by_embedding.reasons)

    distinct = find_duplicate(
        text="a genuinely different observation about compiler diagnostics",
        recent_texts=[text],
        settings=settings,
        embedding_similarity=0.2,
    )
    assert distinct.duplicate is False


def test_duplicate_reports_closest_text_for_review() -> None:
    settings = make_settings()
    report = find_duplicate(
        text="paged kv cache improves long context inference memory",
        recent_texts=["paged kv cache improves long context inference memory a lot"],
        settings=settings,
        embedding_similarity=None,
    )
    assert report.closest_text is not None
    assert "paged" in report.closest_text
    assert report.max_jaccard_similarity > 0


def test_staleness_boundary() -> None:
    from app.publishing.text_rules import is_stale

    now = datetime.now(UTC)
    assert is_stale(now - timedelta(days=3), stale_days=14, now=now) is False
    assert is_stale(now - timedelta(days=20), stale_days=14, now=now) is True
    assert is_stale(None, stale_days=14, now=now) is False


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_parts_rejected(text: str) -> None:
    report = validate_text_rules(make_settings(), [text])
    assert report.passed is False


def test_typography_is_folded_to_ascii() -> None:
    assert normalize_typography("it\u2019s a \u201cbig\u201d deal") == 'it\'s a "big" deal'
    assert normalize_typography("sub\u2011task") == "sub-task"
    assert normalize_typography("a\u2014b") == "a - b"
    assert normalize_typography("wait\u2026") == "wait..."
    assert normalize_typography("x\u00a0y") == "x y"
    assert normalize_typography("\u200bzero width") == "zero width"


def test_typography_normalisation_tidies_spacing() -> None:
    assert normalize_typography("  spaced   out  ") == "spaced out"
    assert normalize_typography("a \u2014 b") == "a - b"


def test_first_person_rule_is_scoped_to_reporting_formats() -> None:
    settings = make_settings()
    sentence = ["i tested this on my own gpu and it was fine"]
    assert validate_text_rules(settings, sentence).passed is False
    assert validate_text_rules(settings, sentence, format="explainer").passed is False
    assert validate_text_rules(settings, sentence, format="relatable").passed is True
