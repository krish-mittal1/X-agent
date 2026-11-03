"""Deterministic candidate scoring.

Signals (weights from configuration, normalised to sum to 1):
  freshness            exponential decay with a configurable half-life
  technical_relevance  persona-interest keyword coverage + technical term density
  novelty              1 - cosine similarity to recent published posts (injected)
  source_reliability   static source heuristic (never model-generated)
  evidence_quality     primary-source flag, publication date, body length, specifics
  audience_usefulness  actionable developer vocabulary minus marketing fluff

Scores are bounded to [0, 1]; the breakdown and human-readable reasons are stored
with each source document, so ranking decisions are auditable.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from app.config import Settings
from app.memory.models import SourceDocument
from app.schemas.research import (
    CandidateClassification,
    CandidateScore,
    CandidateSignals,
)

_TECH_TERMS = {
    # India Core, Politics & Institutions
    "india", "indian", "delhi", "bengaluru", "mumbai", "hyderabad", "chennai", "kolkata",
    "modi", "bjp", "congress", "rahul gandhi", "aap", "opposition", "india bloc", "nda",
    "parliament", "lok sabha", "rajya sabha", "supreme court", "high court", "cabinet",
    "election", "assembly", "poll", "cm", "chief minister", "minister", "governor", "babus",
    # Economy, Finance, Taxes & Regulation
    "rbi", "sebi", "gdp", "inflation", "gst", "rupee", "taxation", "budget", "fiscal",
    "trade", "export", "import", "tariff", "manufacturing", "economy", "reform", "market",
    # Indian Tech, AI, Startups & Space
    "tech", "startup", "founder", "ai", "artificial intelligence", "semiconductor", "fab",
    "upi", "ondc", "aadhaar", "dpi", "fintech", "saas", "deeptech", "unicorn", "funding",
    "isro", "drdo", "space", "defense", "missile", "satellite", "ev", "electronics",
    # Infrastructure, Giants & High-Stakes Domestic Events
    "railways", "vande bharat", "expressway", "infra", "port", "airport", "adani", "reliance",
    "tata", "infosys", "bill", "ordinance", "protest", "strike", "cbi", "ed", "scam",
    "corruption", "probe", "arrest", "bail", "verdict", "controversy", "border", "lac",
}
_ACTIONABLE_TERMS = {
    "breaking", "just in", "update", "shocking", "big", "crackdown", "arrest", "arrested",
    "raid", "raids", "cbi", "ed", "scam", "fraud", "supreme court", "high court", "pulls up",
    "slams", "warning", "banned", "bans", "directive", "penalty", "seized", "crore", "crores",
    "scandal", "probe", "crash", "crashes", "protest", "strike", "resigns", "suspended",
    "digital arrest", "cyber fraud", "consumer court", "summons", "clash", "fired", "controversy",
    "tariff", "ban", "statement", "talks", "vote", "election", "resolution", "military", "trade",
}
_VIRAL_CONTROVERSY_TERMS = {
    # Spicy statements, political fights & sharp digs
    "slams", "hits back", "sparks row", "backlash", "spat", "furious", "outrage",
    "accuses", "clash", "faceoff", "mock", "mocks", "takes dig", "dig at", "shuts down",
    "unfiltered", "revolt", "boycott", "allegations", "warns", "defiant", "walkout",
    "heckled", "hypocrisy", "double standard", "uproar", "controversy", "sparks outrage",
    "blasts", "rips into", "calls bluff", "war of words", "counter attack", "sharp jab",
    # Bizarre, funny, absurd or shocking real-life events & blunders
    "bizarre", "absurd", "ironic", "unbelievable", "caught", "scam", "digital arrest",
    "conman", "busted", "fake officer", "fake cbi", "fake judge", "raided", "cash stack",
    "blunder", "goof up", "hilarious", "banned", "illegal", "viral video", "stunt",
    "trap", "extortion", "caught red handed", "red-handed",
    # Relatable consumer outrage & public friction
    "charging port", "train delayed", "tatkal", "irctc", "refund", "uber cancel",
    "ola scam", "swiggy fee", "zomato fee", "bangalore rent", "security deposit",
    "70 hour", "70 hours", "toxic boss", "work culture", "layoff", "layoffs",
    "tax notice", "income tax notice", "gst on", "toll plaza", "fastag", "challan",
    "fine imposed", "consumer court", "misleading ad", "fake claims",
    # Sensational busts & celebrity/influencer drama
    "betting app", "mahadev app", "influencer raided", "money laundering", "hawala",
    "ed summons", "cbi summons", "lookout notice", "fir lodged",
}
_BORING_TERMS = {
    "annual report", "panel discussion", "webinar", "seminar", "workshop", "proceedings adjourned",
    "subcommittee review", "courtesy call", "valedictory address", "academic symposium",
    "experience centre", "experience centres", "customer acquisition", "brand transformation",
    "press release", "showroom", "showrooms", "encyclopedia", "britannica", "wikipedia",
    "historical archive", "q1 results", "q2 results", "q3 results", "q4 results",
    # Dry official / bureaucratic routine announcements
    "inauguration ceremony", "inaugurates bypass", "foundation stone", "lays foundation",
    "held meeting with", "delegation arrives", "bilateral talks", "courtesy visit",
    "calls on governor", "calls on cm", "reviews preparedness", "disaster review",
    "chaired a meeting", "chairs meeting", "presides over", "mou signed", "signs mou",
    "memorandum of understanding", "statistical bulletin", "routine transfer",
    "transferred and posted", "obituary", "condolences", "pays homage", "pays tributes",
    "weather forecast", "light rainfall expected", "maximum temperature", "aqi worsens to",
    "horoscope", "astrology", "zodiac", "rashifal", "traffic advisory",
}
_MARKETING_TERMS = {
    "revolutionary", "game-changing", "game changer", "unleash", "supercharge",
    "transform your", "10x", "effortless", "seamless", "cutting-edge",
    "state-of-the-art", "next-gen", "world-class", "disrupt", "in today's rapidly changing world",
}
_NUMBER_OR_CODE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|x|billion|million|trillion|km|miles|dollars|rupees|rubles|yuan|tonnes)\b")
_WORD = re.compile(r"[a-z0-9][a-z0-9+#._-]*")


def freshness_signal(published_at: datetime | None, now: datetime, half_life_hours: float) -> float:
    if published_at is None:
        return 0.4
    age_hours = (now - published_at).total_seconds() / 3600.0
    if age_hours <= 0:
        return 1.0
    if half_life_hours <= 0:
        return 1.0
    return max(0.0, min(1.0, math.exp(-age_hours / half_life_hours)))


def _term_hits(text: str, terms: set[str]) -> int:
    words = set(_WORD.findall(text.lower()))
    hits = 0
    for term in terms:
        if " " in term:
            if term in text.lower():
                hits += 1
        elif term in words:
            hits += 1
    return hits


def technical_relevance_signal(text: str, interests: tuple[str, ...]) -> float:
    lowered = text.lower()
    interest_hits = _term_hits(lowered, {i.lower() for i in interests})
    interest_score = min(1.0, interest_hits / 3.0)
    density = min(1.0, _term_hits(lowered, _TECH_TERMS) / 6.0)
    # Priority boost for domestic Indian affairs, politics, tech, and institutions
    india_keywords = ("india", "indian", "delhi", "modi", "bengaluru", "bjp", "congress", "lok sabha", "isro", "rbi", "sebi")
    india_boost = 0.25 if any(k in lowered for k in india_keywords) else 0.0
    return round(min(1.0, 0.45 * interest_score + 0.35 * density + india_boost), 4)


def evidence_quality_signal(document: SourceDocument) -> float:
    score = 0.2
    if document.reliability_score >= 0.75:
        score += 0.15
    if document.metadata_.get("is_primary_source"):
        score += 0.2
    if document.published_at is not None:
        score += 0.1
    if len(document.content) >= 300:
        score += 0.15
    if _NUMBER_OR_CODE.search(document.content):
        score += 0.1
    if document.excerpt and len(document.excerpt) > 60:
        score += 0.05
    return round(min(1.0, score), 4)


def audience_usefulness_signal(document: SourceDocument) -> float:
    text = f"{document.title} {document.content}".lower()
    actionable = min(1.0, _term_hits(text, _ACTIONABLE_TERMS) / 4.0)
    viral_controversy = min(1.0, _term_hits(text, _VIRAL_CONTROVERSY_TERMS) / 2.0)
    marketing = min(1.0, _term_hits(text, _MARKETING_TERMS) / 3.0)
    boring = 0.45 if any(b in text for b in _BORING_TERMS) else 0.0
    specificity = 0.15 if _NUMBER_OR_CODE.search(document.content) else 0.0
    title_viral_boost = 0.2 if any(t in document.title.lower() for t in _VIRAL_CONTROVERSY_TERMS) else 0.0
    score = 0.45 * actionable + 0.35 * viral_controversy + title_viral_boost + specificity + 0.10 - 0.4 * marketing - boring
    return round(max(0.0, min(1.0, score)), 4)


class CandidateScorer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def score_document(
        self,
        document: SourceDocument,
        *,
        interests: tuple[str, ...],
        novelty: float | None,
        now: datetime | None = None,
        refinement: CandidateClassification | None = None,
    ) -> CandidateScore:
        now = now or datetime.now(UTC)
        text = f"{document.title}\n{document.excerpt}\n{document.content}"
        published = document.published_at or document.retrieved_at
        relevance = technical_relevance_signal(text, interests)
        usefulness = audience_usefulness_signal(document)
        if refinement is not None:
            # Blend; deterministic signals keep at least 70% of the weight.
            relevance = round(0.7 * relevance + 0.3 * refinement.technical_relevance, 4)
            usefulness = round(0.7 * usefulness + 0.3 * refinement.audience_usefulness, 4)
        signals = CandidateSignals(
            freshness=freshness_signal(
                published, now, self.settings.freshness_half_life_hours
            ),
            technical_relevance=relevance,
            novelty=round(
                0.5 if novelty is None else max(0.0, min(1.0, novelty)), 4
            ),
            source_reliability=round(max(0.0, min(1.0, document.reliability_score)), 4),
            evidence_quality=evidence_quality_signal(document),
            audience_usefulness=usefulness,
        )
        weights = self.settings.scoring_weights
        total = sum(
            getattr(signals, name) * weight for name, weight in weights.items()
        )
        if refinement is not None and refinement.is_marketing_fluff:
            total *= 0.85
        explanation = [
            f"freshness={signals.freshness:.2f} (half-life "
            f"{self.settings.freshness_half_life_hours:g}h, source "
            f"{document.source_type})",
            f"technical_relevance={signals.technical_relevance:.2f} against "
            f"{len(interests)} persona interests",
            f"novelty={signals.novelty:.2f}"
            + (" (embedding unavailable; neutral prior)" if novelty is None else " vs recent posts"),
            f"source_reliability={signals.source_reliability:.2f} (static source heuristic)",
            f"evidence_quality={signals.evidence_quality:.2f} "
            f"(primary_source={bool(document.metadata_.get('is_primary_source'))}, "
            f"published={'yes' if document.published_at else 'no'})",
            f"audience_usefulness={signals.audience_usefulness:.2f}",
            "weighted sum: "
            + ", ".join(f"{name}={weight:.2f}" for name, weight in weights.items()),
        ]
        if refinement is not None:
            explanation.append(
                "llm refinement applied to relevance/usefulness only: "
                f"{refinement.reasoning[:200]}"
            )
            if refinement.is_marketing_fluff:
                explanation.append("marketing-fluff penalty applied (0.85x)")
        return CandidateScore(
            score=round(max(0.0, min(1.0, total)), 4),
            signals=signals,
            weights=weights,
            explanation=explanation,
            llm_refinement=refinement.model_dump() if refinement is not None else None,
        )
