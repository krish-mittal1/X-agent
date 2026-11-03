import asyncio
import sys

sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr

from app.config import Settings
from app.jobs.tasks import JobContext, publishing
from app.memory.models import Draft
from app.publishing.policy_gate import evaluate_original_post
from app.publishing.provider import LocalDraftProvider, get_publishing_provider
from app.publishing.publisher import Publisher

# Outbound HTTP instrumentation counter
outbound_call_counter = 0

def fake_outbound_call(*args, **kwargs):
    global outbound_call_counter
    outbound_call_counter += 1
    raise RuntimeError(f"VIOLATION: Outbound network call attempted: {args} {kwargs}")

async def run_publishing_safety_audit():
    global outbound_call_counter
    print("=== TEST 1: PUBLISHING SAFETY AUDIT ===")

    # 1. Inspect effective config
    settings = Settings(
        x_publishing_mode="local_draft",
        publishing_enabled=False,
        dry_run=True,
        x_auth_token="mock_x_auth_secret",
        x_ct0="mock_ct0_secret",
        x_api_key="mock_api_key",
        x_api_secret=SecretStr("mock_api_secret"),
        x_access_token="mock_access_token",
        x_access_token_secret=SecretStr("mock_access_token_secret"),
    )
    print(f"Effective Config: mode='{settings.x_publishing_mode}', publishing_enabled={settings.publishing_enabled}, dry_run={settings.dry_run}")
    print(f"Zero Cost Mode: {settings.is_zero_cost_mode}")
    print(f"Has write credentials present: {settings.x_write_credentials_present}")

    # Patch httpx and socket to intercept any outbound call
    with patch("httpx.AsyncClient.send", side_effect=fake_outbound_call), \
         patch("httpx.AsyncClient.get", side_effect=fake_outbound_call), \
         patch("httpx.AsyncClient.post", side_effect=fake_outbound_call), \
         patch("socket.socket.connect", side_effect=fake_outbound_call):

        # Test 1A: Provider Factory
        mock_factory = MagicMock()
        provider = get_publishing_provider(settings, mock_factory)
        print(f"Provider Factory Result: {type(provider).__name__}")
        assert isinstance(provider, LocalDraftProvider), "BraveCDPPublisher or external provider returned!"
        status = await provider.get_status()
        print(f"Provider Status: {status}")
        assert status["zero_cost"] is True
        assert status["outbound_requests_allowed"] is False

        # Test 1B: Background Publishing Scheduler Task
        context = JobContext(
            settings=settings,
            session_factory=mock_factory,
            x_client=MagicMock(),
            embedder=MagicMock(),
            llm=MagicMock(),
        )
        bg_result = await publishing(context)
        print(f"Background Publishing Result: {bg_result}")
        assert bg_result["status"] == "skipped"

        # Even if someone sets publishing_enabled=False and calls publishing task:
        settings_enabled_flag = Settings(
            x_publishing_mode="local_draft",
            publishing_enabled=False,
            dry_run=True,
        )
        context.settings = settings_enabled_flag
        bg_result2 = await publishing(context)
        assert bg_result2["status"] == "skipped"

        # Test 1C: Publisher Orchestration (Original Post)
        mock_session = AsyncMock()
        draft = Draft(
            id=101,
            account_id=1,
            kind="original",
            status="approved",
            topic="Test Topic",
            body="Test original post content for safety check.",
            tone="analysis",
            candidate_score=0.9,
            source_document_ids=[1, 2],
            verification_report={"passed": True, "unverified_facts": 0},
            policy_report={"duplicate": {"duplicate": False}, "trending": False},
        )
        from app.memory.models import Account
        account = Account(id=1, handle="ssh_bugs", is_active=True)
        async def mock_get(model_cls, ident):
            if model_cls is Draft or getattr(model_cls, "__name__", "") == "Draft":
                return draft
            if model_cls is Account or getattr(model_cls, "__name__", "") == "Account":
                return account
            return None
        mock_session.get.side_effect = mock_get
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_session.scalars.return_value = mock_scalars
        mock_session.scalar.return_value = None
        
        settings_publisher = Settings(
            x_publishing_mode="local_draft",
            publishing_enabled=True,
            dry_run=True,
        )
        publisher = Publisher(settings_publisher, mock_factory, x_client=MagicMock(), provider=provider)
        mock_factory.return_value.__aenter__.return_value = mock_session
        mock_factory.return_value.__aexit__.return_value = None
        
        # Test original publishing
        orig_result = await publisher.publish_original(draft.id)
        print(f"Publisher.publish_original Status: {orig_result.status}, reasons: {orig_result.reasons}")
        assert orig_result.status == "local_saved"

        # Test 1D: Publisher Orchestration (Reply Post)
        from app.memory.models import ConversationThread, Interaction
        thread = ConversationThread(id=1, account_id=1, root_post_id="99999")
        interaction = Interaction(id=1, account_id=1, conversation_thread_id=1, text="Hello agent", x_post_id="99999")
        
        reply_draft = Draft(
            id=102,
            account_id=1,
            kind="reply",
            status="approved",
            topic="Test Reply",
            body="Test reply content.",
            candidate_score=0.9,
            in_reply_to_post_id="99999",
            conversation_thread_id=1,
            tone="reply",
            source_document_ids=[1],
            verification_report={"passed": True, "unverified_facts": 0},
            policy_report={"duplicate": {"duplicate": False}, "trending": False},
        )
        async def mock_get_reply(model_cls, ident):
            if model_cls is Draft or getattr(model_cls, "__name__", "") == "Draft":
                return reply_draft
            if model_cls is Account or getattr(model_cls, "__name__", "") == "Account":
                return account
            if model_cls is ConversationThread or getattr(model_cls, "__name__", "") == "ConversationThread":
                return thread
            return None
        mock_session.get.side_effect = mock_get_reply
        async def mock_scalars_fn(query):
            mock_res = MagicMock()
            q_str = str(query)
            if "posts" in q_str.lower():
                mock_res.all.return_value = []
            else:
                mock_res.all.return_value = [interaction]
            return mock_res
        mock_session.scalars.side_effect = mock_scalars_fn
        async def mock_scalar_fn(query):
            q_str = str(query)
            if "opt_out" in q_str.lower():
                return None
            if "interaction" in q_str.lower():
                return interaction
            return None
        mock_session.scalar.side_effect = mock_scalar_fn
        
        settings_publisher = Settings(
            x_publishing_mode="local_draft",
            publishing_enabled=True,
            dry_run=True,
            ai_reply_approval_obtained=True,
            ai_reply_publishing_enabled=True,
        )
        publisher = Publisher(settings_publisher, mock_factory, x_client=MagicMock(), provider=provider)
        reply_result = await publisher.publish_reply(reply_draft.id)
        print(f"Publisher.publish_reply Status: {reply_result.status}, reasons: {reply_result.reasons}")
        assert reply_result.status == "local_saved"

        # Test 1E: Settings & PolicyGate Defensive Live Blocking
        # 1. Attempting to enable live publishing in zero-cost mode is rejected at validation
        import pydantic
        try:
            Settings(
                x_publishing_mode="local_draft",
                publishing_enabled=True,
                dry_run=False,
            )
            settings_live_blocked = False
        except pydantic.ValidationError:
            settings_live_blocked = True
        print(f"Settings Defensive Live Block (local_draft + dry_run=False rejected): {settings_live_blocked}")
        assert settings_live_blocked is True

        # 2. PolicyGate halts all publishing if emergency_pause is set
        settings_paused = Settings(
            x_publishing_mode="local_draft",
            publishing_enabled=True,
            dry_run=True,
            emergency_pause=True,
        )
        decision_paused = evaluate_original_post(
            settings=settings_paused,
            draft=draft,
            account_active=True,
            posts_today=0,
            last_published_at=None,
        )
        print(f"PolicyGate Emergency Pause Block: allowed={decision_paused.allowed}, failures={decision_paused.soft_failures}")
        assert decision_paused.allowed is False

    print(f"\nExact Outbound Publishing Network Calls Made: {outbound_call_counter}")
    assert outbound_call_counter == 0, f"Expected 0 outbound calls, got {outbound_call_counter}"
    print("TEST 1 RESULT: PASS (All publishing entry points strictly contained in local_draft)")

if __name__ == "__main__":
    asyncio.run(run_publishing_safety_audit())
