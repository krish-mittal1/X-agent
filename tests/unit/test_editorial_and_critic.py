from __future__ import annotations

from app.agent.critic import EditorialCriticService
from app.agent.editorial_engine import EditorialCandidate
from app.config import Settings
from app.schemas.content import ClaimCheck, VerificationReport


def make_settings(**overrides) -> Settings:
    values = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)


def test_critic_flags_generic_ai_cliches() -> None:
    critic = EditorialCriticService()
    draft = "This ₹76,000 crore semiconductor investment is a testament to India's ambition. Buckle up, it's a game changer!"
    result = critic.evaluate(draft_text=draft, topic="Semiconductor Fab", angle="analytical")

    assert "generic_ai_phrase" in result.flags
    assert result.originality_score < 70.0
    assert any("testament to" in s for s in result.suggestions)
    assert any("game changer" in s for s in result.suggestions)


def test_critic_rewards_grounded_contrarian_post() -> None:
    critic = EditorialCriticService()
    draft = "While the ministry announced a ₹76,000 crore outlay for fab units, the ground reality is only ₹4,200 crore has been disbursed across 18 months. Karnataka's power allocation remains unresolved."
    v_report = VerificationReport(
        passed=True,
        verified_facts=2,
        unverified_facts=0,
        claims=[
            ClaimCheck(claim="₹76,000 crore outlay announced", claim_type="fact", status="verified", confidence=0.9),
            ClaimCheck(claim="₹4,200 crore disbursed", claim_type="fact", status="verified", confidence=0.85),
        ],
    )
    result = critic.evaluate(draft_text=draft, claims=[], verification_report=v_report, topic="Semicon", angle="contrarian")

    assert result.verdict == "approved"
    assert result.overall_score >= 75.0
    assert result.factual_score >= 90.0
    assert "generic_ai_phrase" not in result.flags


def test_critic_rejects_on_contradiction_hard_stop() -> None:
    critic = EditorialCriticService()
    draft = "Semiconductor outlay is ₹150,000 crore, doubling previous projections."
    v_report = VerificationReport(
        passed=False,
        contradicted_claims=1,
        hard_stop_triggered=True,
        hard_stop_reasons=["Contradiction in outlay figures"],
    )
    result = critic.evaluate(draft_text=draft, verification_report=v_report, topic="Semicon", angle="analytical")

    assert result.verdict == "rejected"
    assert result.factual_score == 0.0
    assert "factual_contradiction" in result.flags


def test_editorial_candidate_sorting() -> None:
    cand_low = EditorialCandidate(
        angle="witty",
        hook="Low score draft",
        parts=["Low score draft text"],
        rationale="witty",
        claims=[],
    )
    cand_high = EditorialCandidate(
        angle="contrarian",
        hook="High score draft",
        parts=["High score draft text"],
        rationale="contrarian",
        claims=[],
    )
    critic = EditorialCriticService()
    cand_low.critic_score = critic.evaluate("Buckle up, game changer! In a world where tech happens.", angle="witty")
    cand_high.critic_score = critic.evaluate("SEBI circular tightened index derivatives: ₹76,000 crore exposure limited while brokers rebalance margin.", angle="contrarian")

    candidates = [cand_low, cand_high]
    candidates.sort(key=lambda c: (c.critic_score.overall_score if c.critic_score else 0), reverse=True)

    assert candidates[0].angle == "contrarian"
    assert candidates[0].critic_score.overall_score > candidates[1].critic_score.overall_score
