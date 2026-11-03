from __future__ import annotations

import random

import pytest

from app.agent.formats import (
    ALL_FORMATS,
    DEFAULT_FORMAT_WEIGHTS,
    choose_format,
    is_comedic,
    is_factual,
    normalise_format,
    parse_mix,
    requires_numeric_evidence,
)
from app.config import load_settings


def make_settings(**overrides: object):
    base = {
        "app_env": "test",
        "llm_provider": "disabled",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "rss_feeds": "",
        "github_repositories": "",
    }
    base.update(overrides)
    return load_settings(**base)


def test_parse_mix_normalises_weights() -> None:
    weights = parse_mix("shitpost:3,explainer:1")
    assert weights == {"shitpost": 0.75, "explainer": 0.25}


def test_parse_mix_drops_unknown_and_invalid_entries() -> None:
    weights = parse_mix("shitpost:1,nonsense:5,hot_take:abc,relatable:0")
    assert weights == {"shitpost": 1.0}


def test_parse_mix_falls_back_to_defaults_when_nothing_parses() -> None:
    assert parse_mix("") == pytest.approx(DEFAULT_FORMAT_WEIGHTS)
    assert parse_mix("garbage") == pytest.approx(DEFAULT_FORMAT_WEIGHTS)


def test_choose_format_respects_a_single_entry_mix() -> None:
    choice = choose_format(make_settings(post_format_mix="shitpost:1"))
    assert choice.format == "shitpost"


def test_choose_format_only_returns_configured_formats() -> None:
    settings = make_settings(post_format_mix="relatable:1,news_jab:1")
    rng = random.Random(1234)
    picks = {choose_format(settings, rng=rng).format for _ in range(200)}
    assert picks <= {"relatable", "news_jab"}
    assert picks == {"relatable", "news_jab"}


def test_recent_format_is_down_weighted_not_eliminated() -> None:
    settings = make_settings(post_format_mix="shitpost:1,explainer:1")
    choice = choose_format(settings, ["shitpost"])
    assert choice.weights["explainer"] > choice.weights["shitpost"]
    rng = random.Random(7)
    picks = [choose_format(settings, ["shitpost"], rng=rng).format for _ in range(500)]
    # Penalised, still reachable: a single-format account would look broken.
    assert picks.count("explainer") > picks.count("shitpost")
    assert picks.count("shitpost") > 0


def test_single_format_ignores_recent_penalty() -> None:
    choice = choose_format(make_settings(post_format_mix="hot_take:1"), ["hot_take"])
    assert choice.format == "hot_take"
    assert choice.weights["hot_take"] == pytest.approx(1.0)


def test_format_classification() -> None:
    assert is_factual("explainer")
    assert is_factual("news_jab")
    assert not is_factual("shitpost")
    assert is_comedic("relatable")
    assert is_comedic("hot_take")
    assert {"shitpost", "hot_take", "relatable", "news_jab", "explainer"}.issubset(ALL_FORMATS)


def test_numeric_evidence_is_required_for_takes_and_reporting_only() -> None:
    assert requires_numeric_evidence("explainer")
    assert requires_numeric_evidence("news_jab")
    assert requires_numeric_evidence("hot_take")
    # Hyperbole is the whole format in these two.
    assert not requires_numeric_evidence("shitpost")
    assert not requires_numeric_evidence("relatable")


def test_normalise_format_maps_unknown_to_safe_default() -> None:
    assert normalise_format("Shitpost") == "shitpost"
    assert normalise_format(None) == "straight_news"
    assert normalise_format("press release") == "straight_news"


def test_default_mix_prioritises_straight_news_but_keeps_explainers() -> None:
    weights = DEFAULT_FORMAT_WEIGHTS
    assert weights["straight_news"] == max(weights.values())
    assert weights["explainer"] > 0
    assert sum(weights.values()) == pytest.approx(1.0)
