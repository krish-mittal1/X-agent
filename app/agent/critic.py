"""Independent Editorial Critic & Quality Scoring Engine (Feature 5).

Evaluates candidate drafts against India Pulse newsroom standards:
- Factual grounding and attribution
- News clarity and strong opening
- Information density
- Indian relevance and format fit
- Naturalness
- Originality without forced contrarianism

Deterministic anti-pattern detection flags AI clichés, forced hot takes,
unattributed allegations, rumour-as-fact, repetitive hooks, and irrelevant images.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.schemas.content import DraftClaim, VerificationReport

_BANNED_AI_CLICHES = {
    r"\ba testament to\b": "a testament to",
    r"\bin a world where\b": "in a world where",
    r"\bit remains to be seen\b": "it remains to be seen",
    r"\bbuckle up\b": "buckle up",
    r"\bgame[ -]?changer\b": "game changer",
    r"\bdelve into\b": "delve into",
    r"\bdelving\b": "delving",
    r"\bnavigating the (?:digital )?landscape\b": "navigating the landscape",
    r"\blet that sink in\b": "let that sink in",
    r"\bat the end of the day\b": "at the end of the day",
    r"\bonly time will tell\b": "only time will tell",
    r"\bdouble-edged sword\b": "double-edged sword",
    r"\bparadigm shift\b": "paradigm shift",
    r"\bplays? a vital role\b": "plays a vital role",
    r"\bbeacon of hope\b": "beacon of hope",
    r"\brich tapestry\b": "rich tapestry",
    r"\bunpack this\b": "unpack this",
    r"\bhere'?s the kicker\b": "here's the kicker",
    r"\bbreaking down the\b": "breaking down the",
}

_BANNED_FORCED_HOT_TAKES = {
    r"\beveryone is (?:framing|ignoring|cheering)\b": "everyone is framing/ignoring/cheering",
    r"\bthe real story is\b": "the real story is",
    r"\bthis exposes\b": "this exposes",
    r"\bwhat nobody is talking about\b": "what nobody is talking about",
    r"\bmasks (?:the |a )?(?:deeper |severe )?problem\b": "masks the deeper problem",
    r"\ban admission of (?:infrastructure )?failure\b": "an admission of failure",
    r"\bthe uncomfortable truth\b": "the uncomfortable truth",
}

_REPETITIVE_HOOKS = [
    r"^here'?s why (?:this|it) matters",
    r"^let'?s dive in",
    r"^thread:? 🧵?",
    r"^a quick thread",
    r"^read on to find out",
]

_ACADEMIC_CONNECTIVES = [
    r"\bfurthermore\b",
    r"\bmoreover\b",
    r"\bin conclusion\b",
    r"\bit is noteworthy that\b",
    r"\bhhenceforth\b",
    r"\bnotwithstanding\b",
]

_HYPE_OPENERS = (
    r"^breaking\b",
    r"^shocking\b",
    r"^biggest\b",
    r"^huge\b",
)

_EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]", flags=re.UNICODE
)

_ATTRIBUTION_MARKERS = (
    "according to",
    "announced",
    "stated",
    "reported",
    "said",
    "says",
    "confirmed",
    "directed",
    "alleged",
    "alleges",
    "told",
    "claimed",
)

_INDIAN_MARKERS = (
    "india",
    "indian",
    "delhi",
    "mumbai",
    "bengaluru",
    "bangalore",
    "rbi",
    "sebi",
    "pib",
    "crore",
    "lakh",
    "isro",
    "drdo",
    "railways",
    "court",
    "parliament",
    "ministry",
    "cabinet",
    "bjp",
    "congress",
    "modi",
    "gujarat",
    "karnataka",
)


class EditorialEvaluationResult(BaseModel):
    overall_score: float = Field(ge=0.0, le=100.0)
    factual_score: float = Field(ge=0.0, le=100.0)
    originality_score: float = Field(ge=0.0, le=100.0)
    insight_score: float = Field(ge=0.0, le=100.0)
    naturalness_score: float = Field(ge=0.0, le=100.0)
    persona_score: float = Field(ge=0.0, le=100.0)
    clarity_score: float = Field(ge=0.0, le=100.0)
    verdict: str = "approved"  # approved, needs_revision, rejected
    flags: list[str] = Field(default_factory=list)
    critic_reasoning: str = ""
    suggestions: list[str] = Field(default_factory=list)


class EditorialCriticService:
    """Evaluates draft quality, editorial persona fit, and flags AI anti-patterns."""

    def evaluate(
        self,
        draft_text: str,
        claims: list[DraftClaim] | None = None,
        verification_report: VerificationReport | None = None,
        topic: str = "",
        angle: str = "straight_news",
        news_format: str = "",
        image_url: str | None = None,
        image_relevant: bool | None = None,
        image_verified: bool | None = None,
    ) -> EditorialEvaluationResult:
        flags: list[str] = []
        suggestions: list[str] = []
        claims = claims or []
        lowered = draft_text.lower()
        first_line = draft_text.strip().split("\n", 1)[0].lower()

        # 1. ACCURACY AND ATTRIBUTION (25%)
        if verification_report is not None:
            if verification_report.contradicted_claims > 0 or verification_report.hard_stop_triggered:
                factual_score = 0.0
                flags.append("factual_contradiction")
                suggestions.append("Resolve contradictory claims or unverified numerical figures.")
            elif verification_report.unverified_facts > 0:
                ratio = verification_report.verified_facts / max(
                    1, verification_report.verified_facts + verification_report.unverified_facts
                )
                factual_score = round(ratio * 70.0, 1)
                flags.append("unverified_assertions")
                suggestions.append("Back unverified claims with exact quotes from source articles.")
            else:
                factual_score = 95.0 if verification_report.verified_facts > 0 else 80.0
        else:
            factual_score = 75.0

        allegation_hit = bool(
            re.search(r"\b(?:alleg(?:es|ed|ation)|accused|scam|corrupt(?:ion)?)\b", lowered)
        )
        has_attribution = (
            any(w in lowered for w in _ATTRIBUTION_MARKERS)
            or '"' in draft_text
            or "“" in draft_text
        )
        if allegation_hit and not has_attribution:
            factual_score = min(factual_score, 45.0)
            flags.append("unattributed_allegation")
            suggestions.append("Attribute allegations to the person or organisation making them.")

        rumour_hit = bool(re.search(r"\b(?:rumour|rumor|unconfirmed|viral claim)\b", lowered))
        presented_as_confirmed = bool(
            re.search(r"\b(?:confirmed|breaking|has (?:happened|occurred)|is now fact)\b", lowered)
        )
        if rumour_hit and presented_as_confirmed:
            factual_score = min(factual_score, 40.0)
            flags.append("rumour_as_fact")
            suggestions.append("Do not present a rumour as a confirmed event.")

        factual_score = max(0.0, min(100.0, factual_score))

        # 2. ORIGINALITY WITHOUT FORCED CONTRARIANISM (20%)
        originality_score = 95.0
        matched_hot_takes = [
            name for pat, name in _BANNED_FORCED_HOT_TAKES.items() if re.search(pat, lowered)
        ]
        if matched_hot_takes:
            originality_score -= len(matched_hot_takes) * 25.0
            flags.append("forced_hot_take")
            suggestions.append(
                "Remove forced hot-take construction: "
                f"{', '.join(matched_hot_takes)}. Report the verified news directly."
            )

        matched_cliches = [name for pat, name in _BANNED_AI_CLICHES.items() if re.search(pat, lowered)]
        if matched_cliches:
            originality_score -= len(matched_cliches) * 15.0
            flags.append("generic_ai_phrase")
            suggestions.append(f"Remove generic AI phrase(s): {', '.join(matched_cliches)}")

        if any(re.search(pat, lowered) for pat in _REPETITIVE_HOOKS):
            originality_score -= 15.0
            flags.append("repetitive_hook")
            suggestions.append("Use a specific opening instead of a generic thread intro.")

        # Do not reward aggression or controversy for its own sake.
        if re.search(r"\b(?:destroy|destroyed|humiliated|owned|meltdown)\b", lowered):
            originality_score -= 10.0
            flags.append("aggressive_framing")
            suggestions.append("Do not reward aggressive or manufactured controversy.")

        originality_score = max(10.0, min(100.0, originality_score))

        # 3. INFORMATION DENSITY (20%)
        insight_score = 80.0
        has_metrics = bool(
            re.search(
                r"(?:₹|\$|€)\s*\d+|\b\d+(?:[.,]\d+)?\s*(?:%|crore|lakh|bn|million|gw|mw|km|km/h)\b|\b\d{2,}\b",
                draft_text,
                re.IGNORECASE,
            )
        )
        has_institution = any(
            w in lowered
            for w in [
                "railways",
                "isro",
                "drdo",
                "rbi",
                "sebi",
                "court",
                "ministry",
                "parliament",
                "cabinet",
                "police",
                "commission",
            ]
        )
        if has_metrics:
            insight_score += 10.0
        if has_attribution:
            insight_score += 5.0
        if has_institution:
            insight_score += 5.0
        if not has_metrics and not has_attribution and len(draft_text.split()) < 12:
            insight_score -= 20.0
            flags.append("low_information_density")
            suggestions.append("Include specific verified details, numbers, or attribution.")
        if news_format in {"political_news", "explainer"} and not has_attribution:
            insight_score -= 10.0
            flags.append("missing_critical_context")
            suggestions.append("Add attribution or the next confirmed step.")
        insight_score = max(10.0, min(100.0, insight_score))

        # 4. NATURALNESS (15%)
        naturalness_score = 85.0
        academic_hits = [c for c in _ACADEMIC_CONNECTIVES if re.search(c, lowered)]
        if academic_hits:
            naturalness_score -= len(academic_hits) * 12.0
            flags.append("academic_stiffness")
            clean_acad = [c.replace(r"\b", "") for c in academic_hits]
            suggestions.append(
                f"Replace formal connectives ({', '.join(clean_acad)}) with conversational flow."
            )

        emojis = _EMOJI_PATTERN.findall(draft_text)
        if len(emojis) > 2:
            naturalness_score -= 15.0
            flags.append("excessive_emojis")
            suggestions.append("Limit emojis; do not decorate ordinary news.")
        naturalness_score = max(10.0, min(100.0, naturalness_score))

        # 5. INDIAN RELEVANCE AND FORMAT FIT (10%)
        persona_score = 80.0
        marker_hits = sum(1 for m in _INDIAN_MARKERS if m in lowered)
        if marker_hits >= 1:
            persona_score += 15.0
        if angle in ("straight_news", "context_explainer", "engaging_angle"):
            persona_score += 5.0
        if news_format == "quote_led" and '"' not in draft_text and "“" not in draft_text:
            persona_score -= 20.0
            flags.append("format_mismatch")
            suggestions.append("Quote-led posts must use an exact verified quotation.")
        if news_format == "business_economy" and not has_metrics:
            persona_score -= 10.0
            flags.append("format_mismatch")
            suggestions.append("Business posts should include a verified figure with units.")
        persona_score = max(20.0, min(100.0, persona_score))

        # 6. NEWS CLARITY, OPENING, LENGTH, IMAGE (10%)
        clarity_score = 90.0
        parts = [part for part in draft_text.split("\n\n") if part.strip()] or [draft_text]
        longest_part = max(map(len, parts))
        if longest_part > 280:
            clarity_score -= 20.0
            flags.append("length_overflow")
            suggestions.append(
                f"Post part is {longest_part} chars (exceeds the 280-char X limit)."
            )
        if any(re.search(pat, first_line) for pat in _HYPE_OPENERS) and news_format not in {
            "breaking_news",
            "video_viral",
        }:
            clarity_score -= 15.0
            flags.append("overhyped_hook")
            suggestions.append("Match the hook to the importance of the development.")
        if image_url and image_relevant is False:
            clarity_score -= 20.0
            flags.append("irrelevant_image")
            suggestions.append("Do not attach an unrelated or unprovenanced image.")
        if image_url and image_verified is False and image_relevant is False:
            flags.append("unverified_image_label")
        if re.search(r"verified editorial photo", lowered):
            flags.append("invented_image_credit")
            clarity_score -= 15.0
            suggestions.append("Never invent image credits or unverified photo labels.")
        clarity_score = max(20.0, min(100.0, clarity_score))

        overall_score = round(
            0.25 * factual_score
            + 0.20 * originality_score
            + 0.20 * insight_score
            + 0.15 * naturalness_score
            + 0.10 * persona_score
            + 0.10 * clarity_score,
            1,
        )

        if (verification_report and verification_report.hard_stop_triggered) or factual_score < 40.0:
            verdict = "rejected"
            critic_reasoning = (
                "Draft rejected by critic: unverified factual claims or hard safety stop triggered."
            )
        elif "rumour_as_fact" in flags or "unattributed_allegation" in flags:
            verdict = "needs_revision"
            critic_reasoning = (
                "Draft needs revision: allegation or rumour is not attributed as such."
            )
        elif "forced_hot_take" in flags:
            verdict = "needs_revision"
            critic_reasoning = (
                "Draft needs revision: forced contrarian hot take detected "
                f"({', '.join(suggestions)})."
            )
        elif "generic_ai_phrase" in flags or "repetitive_hook" in flags:
            verdict = "needs_revision"
            critic_reasoning = (
                "Draft needs revision: generic AI phrases or repetitive hooks detected "
                f"({', '.join(suggestions)})."
            )
        elif overall_score >= 70.0:
            verdict = "approved"
            critic_reasoning = (
                f"Strong editorial candidate ({angle.replace('_', ' ').capitalize()}). "
                "Clear reporting, attribution, and newsroom voice."
            )
        elif overall_score >= 50.0:
            verdict = "needs_revision"
            critic_reasoning = (
                "Draft is acceptable but could be sharpened for clarity and attribution."
            )
        else:
            verdict = "rejected"
            critic_reasoning = "Draft fell below editorial quality threshold. Requires human rework."

        return EditorialEvaluationResult(
            overall_score=overall_score,
            factual_score=factual_score,
            originality_score=originality_score,
            insight_score=insight_score,
            naturalness_score=naturalness_score,
            persona_score=persona_score,
            clarity_score=clarity_score,
            verdict=verdict,
            flags=flags,
            critic_reasoning=critic_reasoning,
            suggestions=suggestions,
        )
