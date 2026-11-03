from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.jobs.tasks import JobContext, publishing
from app.memory.models import Draft
from app.publishing.policy_gate import evaluate_original_post
from app.publishing.provider import LocalDraftProvider, get_publishing_provider


@pytest.mark.asyncio
async def test_local_draft_mode_never_instantiates_brave_cdp():
    """Prove that even if mock Brave CDP credentials and settings are present,

    local_draft mode strictly returns LocalDraftProvider.
    """
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
        x_auth_token="mock_auth_token_123",
        x_ct0="mock_ct0_token_456",
        x_persona_handle="ssh_bugs",
    )
    mock_session_factory = MagicMock()
    provider = get_publishing_provider(settings, mock_session_factory)

    assert isinstance(provider, LocalDraftProvider)
    status = await provider.get_status()
    assert status["provider"] == "local_draft"
    assert status["zero_cost"] is True
    assert status["outbound_requests_allowed"] is False

def test_config_prevents_live_publishing_in_local_draft_mode():
    """Prove Settings validator raises ValidationError if live publishing is attempted with local_draft."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            x_publishing_mode="local_draft",
            publishing_enabled=True,
            dry_run=False,
        )
    assert "Cannot enable live X API publishing" in str(exc_info.value)

@pytest.mark.asyncio
async def test_background_publishing_skips_in_local_draft_mode():
    """Prove that background publishing task returns skipped when local_draft mode is active."""
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
    )
    context = JobContext(
        settings=settings,
        session_factory=MagicMock(),
        x_client=MagicMock(),
        embedder=MagicMock(),
        llm=MagicMock(),
    )
    res = await publishing(context)
    assert res["status"] == "skipped"
    assert "publishing disabled" in res["reason"] or "local_draft mode active" in res["reason"]

def test_policy_gate_allows_credentials_in_local_draft():
    """Prove policy gate recognizes local_draft as not requiring external X write credentials."""
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
        x_api_key="",
        x_api_secret="",
        x_access_token="",
        x_access_token_secret="",
    )
    draft = Draft(
        account_id=1,
        kind="original",
        body="Policy check test draft for local draft mode.",
        candidate_score=0.9,
    )
    decision = evaluate_original_post(
        settings=settings,
        draft=draft,
        account_active=True,
        posts_today=0,
        last_published_at=None,
    )
    # The x_credentials check should pass because local_draft doesn't need live X keys
    cred_checks = [c for c in decision.checks if c.name == "x_credentials"]
    assert len(cred_checks) == 1
    assert cred_checks[0].passed is True
