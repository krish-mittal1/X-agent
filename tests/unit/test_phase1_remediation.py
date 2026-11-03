"""Regression test suite for Phase 1: Core Runtime Stability remediation.

Verifies:
1. `publish_reply` constructs and passes `parts` properly without NameError.
2. `PublishResult` schema accepts `local_saved` status.
3. `fast_refresh_rss` handles missing imports, logs errors, and returns accurate counts.
4. `app.providers.factory` and `app.agent.reply_drafter` type annotations with `Any` are valid.
5. Policy gate kill switch (`publishing_enabled=False`) blocks even with `dry_run=True`.
6. Policy gate blocks live publishing (`dry_run=False`) when credentials are missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.agent.reply_drafter as rd
import app.providers.factory as f
from app.config import Settings
from app.memory.models import ConversationThread, Draft, Interaction
from app.publishing.policy_gate import evaluate_original_post
from app.publishing.publisher import Publisher
from app.schemas.publishing import PolicyDecision, PublishResult


def make_test_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "llm_provider": "disabled",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "publishing_enabled": True,
        "dry_run": True,
        "x_publishing_mode": "local_draft",
        "x_api_enabled": False,
        "ai_reply_approval_obtained": True,
        "ai_reply_publishing_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_publish_result_accepts_local_saved() -> None:
    """Verify that PublishResult status accepts 'local_saved' without validation errors."""
    decision = PolicyDecision(allowed=True, checks=[])
    res = PublishResult(
        status="local_saved",
        draft_id=42,
        post_ids=[],
        reasons=["saved to local review queue"],
        policy=decision,
    )
    assert res.status == "local_saved"
    assert res.draft_id == 42
    assert res.reasons == ["saved to local review queue"]


def test_factory_and_reply_drafter_have_any_in_namespace() -> None:
    """Verify app/providers/factory.py and app/agent/reply_drafter.py have Any defined."""
    assert hasattr(f, "Any")
    assert f.Any is Any
    assert hasattr(rd, "Any")
    assert rd.Any is Any


def test_kill_switch_always_blocks_in_policy_gate() -> None:
    """Master kill switch (publishing_enabled=False) must block even if dry_run=True."""
    settings = make_test_settings(publishing_enabled=False, dry_run=True)
    draft = Draft(
        id=1,
        account_id=1,
        kind="original",
        status="ready",
        topic="Security",
        body="Security update for coding agents.",
        source_document_ids=[1],
        verification_report={"passed": True, "unverified_facts": 0},
        policy_report={"duplicate": {"duplicate": False}},
    )
    decision = evaluate_original_post(
        settings=settings,
        draft=draft,
        account_active=True,
        posts_today=0,
        last_published_at=None,
    )
    assert decision.allowed is False
    assert any("publishing is disabled" in s for s in decision.soft_failures)


def test_live_publishing_without_credentials_blocked_defensively() -> None:
    """Live publishing (dry_run=False) must be blocked defensively if credentials missing."""
    settings = make_test_settings(publishing_enabled=True).model_copy(update={"dry_run": False})
    draft = Draft(
        id=2,
        account_id=1,
        kind="original",
        status="ready",
        topic="Security",
        body="Security update for coding agents.",
        source_document_ids=[1],
        verification_report={"passed": True, "unverified_facts": 0},
        policy_report={"duplicate": {"duplicate": False}},
    )
    decision = evaluate_original_post(
        settings=settings,
        draft=draft,
        account_active=True,
        posts_today=0,
        last_published_at=None,
    )
    assert decision.allowed is False
    assert any("credentials" in f for f in decision.hard_failures)


@pytest.mark.asyncio
async def test_publish_reply_passes_parts_without_name_error() -> None:
    """publish_reply must construct parts and pass to provider.publish without NameError."""
    settings = make_test_settings(x_publishing_mode="local_draft")
    mock_provider = MagicMock()
    mock_provider.publish = AsyncMock(
        return_value=PublishResult(status="local_saved", draft_id=99)
    )

    draft = Draft(
        id=99,
        account_id=1,
        kind="reply",
        status="approved",
        in_reply_to_post_id="post_123",
        conversation_thread_id=5,
        target_author_id="author_456",
        body="Reply body text",
        thread_parts=[],
        source_document_ids=[10],
        verification_report={"passed": True, "unverified_facts": 0},
        policy_report={"duplicate": {"duplicate": False}},
    )

    interaction = Interaction(
        account_id=1,
        x_post_id="post_123",
        author_x_user_id="author_456",
        author_handle="devperson",
        direction="inbound",
        text="What about latency?",
        occurred_at=datetime.now(UTC),
    )

    thread = ConversationThread(id=5, account_id=1, x_conversation_id="conv_1", topic="latency")

    async def mock_get(entity, ident):
        if entity is Draft:
            return draft
        if entity is ConversationThread:
            return thread
        return None

    async def mock_scalar(stmt):
        stmt_str = str(stmt).lower()
        if "interaction" in stmt_str:
            return interaction
        return None

    async def mock_scalars(stmt):
        stmt_str = str(stmt).lower()
        res = MagicMock()
        if "posts" in stmt_str:
            res.all.return_value = []
        elif "interactions" in stmt_str:
            res.all.return_value = [interaction]
        else:
            res.all.return_value = []
        return res

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.get = AsyncMock(side_effect=mock_get)
    mock_session.scalar = AsyncMock(side_effect=mock_scalar)
    mock_session.scalars = AsyncMock(side_effect=mock_scalars)

    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__.return_value = mock_session
    mock_session_factory.return_value.__aexit__.return_value = None

    publisher = Publisher(settings, mock_session_factory, provider=mock_provider)
    result = await publisher.publish_reply(99)

    assert result.status == "local_saved"
    mock_provider.publish.assert_awaited_once()
    args, kwargs = mock_provider.publish.call_args
    passed_draft, passed_parts = args
    assert passed_draft.id == 99
    assert passed_parts == ["Reply body text"]


@pytest.mark.asyncio
async def test_fast_refresh_rss_accuracy_and_resilience() -> None:
    """fast_refresh_rss must return accurate counts and gracefully handle malformed feeds."""
    from app.api.dashboard import fast_refresh_rss

    settings = make_test_settings(rss_feeds="https://example.com/feed.xml")
    mock_db = AsyncMock()

    sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Tech News</title>
        <link>https://example.com</link>
        <item>
          <title>Test Valid Article</title>
          <link>https://example.com/valid-article-1</link>
          <description>Valid summary text about regional tech.</description>
        </item>
        <item>
          <title>Malformed Missing Link</title>
          <description>No link present.</description>
        </item>
      </channel>
    </rss>
    """

    with (
        patch("httpx.AsyncClient.get") as mock_get,
        patch("app.memory.repositories.SourceRepository.store_item") as mock_store,
        patch("app.memory.repositories.SourceRepository.list_unscored", return_value=[]),
        patch("app.api.dashboard.load_persona") as mock_persona,
    ):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = sample_rss
        mock_get.return_value = mock_response

        mock_doc = MagicMock()
        mock_doc.id = 1
        mock_doc.title = "Test Valid Article"
        mock_doc.content = "Valid summary text about regional tech."
        mock_doc.published_at = datetime.now(UTC)
        mock_doc.reliability_score = 0.8
        mock_store.return_value = (mock_doc, True)
        mock_persona.return_value = MagicMock(interests=["tech"])

        stats = await fast_refresh_rss(settings, account_id=1, db=mock_db)

        assert stats["fetched"] == 2
        assert stats["stored"] == 1
        assert stats["skipped"] >= 1  # The malformed item with missing link is skipped
        assert stats["failed"] == 0
