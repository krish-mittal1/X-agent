"""The model must be told the numeric length limit, not just 'fits the limit'.

A regression here silently produced 429-character posts against a 280-character
cap, which the text-rule gate then blocked — every draft came out unusable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.persona import DEFAULT_PERSONA
from app.agent.prompts import build_draft_messages, build_reply_messages
from app.config import Settings

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_draft_prompt_states_the_numeric_length_limit() -> None:
    settings = make_settings(max_post_length=280, max_thread_parts=3)
    messages = build_draft_messages(
        persona=DEFAULT_PERSONA,
        documents=[],
        memories=[],
        recent_posts=[],
        settings=settings,
        now=NOW,
    )
    user = messages[1].content
    assert "at most 280 weighted characters" in user
    assert "260 characters" in user  # asks for margin under the cap
    assert "over the limit is rejected" in user


def test_draft_prompt_limit_follows_configuration() -> None:
    settings = make_settings(max_post_length=140, max_thread_parts=2)
    messages = build_draft_messages(
        persona=DEFAULT_PERSONA,
        documents=[],
        memories=[],
        recent_posts=[],
        settings=settings,
        now=NOW,
    )
    assert "at most 140 weighted characters" in messages[1].content


def test_reply_prompt_states_the_numeric_length_limit() -> None:
    messages = build_reply_messages(
        persona=DEFAULT_PERSONA,
        conversation_context="dev: did the API change?",
        memories=[],
        now=NOW,
        handle="dev",
        max_post_length=280,
    )
    assert "at most 280 weighted characters" in messages[1].content


def test_reply_prompt_includes_recent_posts_and_consistency() -> None:
    recent = ["bhai postgres best hai baaki sab moh maya hai", "2am debugging broke prod"]
    messages = build_reply_messages(
        persona=DEFAULT_PERSONA,
        conversation_context="dev: what DB should I use?",
        memories=[],
        now=NOW,
        handle="dev",
        max_post_length=280,
        recent_posts=recent,
    )
    user_content = messages[1].content
    assert "YOUR RECENT POSTS & OPINIONS (STRICT ANTI-CONTRADICTION RULE):" in user_content
    assert "bhai postgres best hai baaki sab moh maya hai" in user_content
    assert "STRICT OPINION & PERSONALITY CONSISTENCY" in user_content
    assert "NEVER contradict what you previously said" in user_content


def test_draft_prompt_includes_anti_contradiction_rule() -> None:
    settings = make_settings(max_post_length=280)
    messages = build_draft_messages(
        persona=DEFAULT_PERSONA,
        documents=[],
        memories=[],
        recent_posts=["postgres supremacy always"],
        settings=settings,
        now=NOW,
    )
    user_content = messages[1].content
    assert "YOUR RECENT POSTS & OPINIONS (STRICT ANTI-CONTRADICTION RULE):" in user_content
    assert "NEVER contradict your previously expressed stances" in user_content
