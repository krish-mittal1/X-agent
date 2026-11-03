"""Application configuration.

All configuration is environment driven and validated at import/startup time.
Policy-relevant switches live here as plain values so that they can only be
changed by an operator (environment / .env), never by an LLM response.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application -------------------------------------------------------
    app_name: str = "tech-x-agent"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    admin_api_token: SecretStr | None = None
    timezone: str = "UTC"

    # --- database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://techx:techx@localhost:55432/techx"
    database_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle_seconds: int = 1800

    # --- LLM ---------------------------------------------------------------
    llm_provider: Literal["gemini", "disabled"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-3.5-flash-lite"
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 3
    llm_temperature: float = 0.8
    llm_max_output_tokens: int = 1000
    # Reasoning models (gemini-3.5-flash-lite) spend max_tokens on reasoning before any
    # content. "low" keeps enough budget for valid JSON at fast turnaround.
    llm_reasoning_effort: Literal["", "low", "medium", "high"] = "low"

    # --- LLM rate limits ---------------------------------------------------
    # Defaults tailored for Google Gemini 2.5 Flash:
    # Free tier: 15 RPM / 1,500 RPD / 1,000,000 TPM. Requests are paced locally
    # so a 429 is the exception rather than the normal path.
    llm_rate_limit_enabled: bool = True
    llm_requests_per_minute: int = 15
    llm_requests_per_day: int = 500
    llm_tokens_per_minute: int = 250_000
    llm_tokens_per_day: int = 4_000_000
    llm_chars_per_token: float = 3.5
    llm_rate_limit_safety_ratio: float = 0.85
    llm_input_price_per_mtok: float = 0.0
    llm_output_price_per_mtok: float = 0.0
    llm_daily_budget_usd: float = 0.0  # 0 == unlimited (documented)
    llm_monthly_budget_usd: float = 0.0
    llm_max_requests_per_day: int = 500
    llm_validate_model_on_startup: bool = True

    # --- embeddings --------------------------------------------------------
    embedding_provider: Literal["sentence_transformers", "hashing"] = "sentence_transformers"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    embedding_batch_size: int = 32
    embedding_device: str = "cpu"
    embedding_chunk_words: int = 180
    embedding_chunk_overlap: int = 40
    embedding_min_chunk_words: int = 12
    memory_max_chunks_per_document: int = 25
    memory_conversation_ttl_days: int = 30

    # --- X platform --------------------------------------------------------
    x_api_base_url: str = "https://api.x.com/2"
    x_consumer_key: SecretStr | None = None
    x_consumer_secret: SecretStr | None = None
    x_access_token: SecretStr | None = None
    x_access_token_secret: SecretStr | None = None
    x_bearer_token: SecretStr | None = None
    x_request_timeout_seconds: float = 20.0
    x_max_retries: int = 2
    x_reads_enabled: bool = False
    x_research_enabled: bool = False
    x_research_queries: str = ""
    x_persona_handle: str = ""  # the account handle, e.g. "techbrief"

    # --- Zero-Cost Architecture (Strict ₹0 X API Operation) ----------------
    x_publishing_mode: str = "local_draft"  # "local_draft" | "brave_cdp" | "official_api"
    x_api_enabled: bool = False
    x_auth_token: SecretStr | None = None
    x_ct0: SecretStr | None = None
    enable_reply_scanner: bool = False
    enable_manual_reply_workflow: bool = True
    api_expenditure_usd: float = 0.0

    # --- controls / kill switches -----------------------------------------
    discovery_enabled: bool = True
    drafting_enabled: bool = True
    publishing_enabled: bool = False  # master kill switch for outbound publishing
    emergency_pause: bool = False
    dry_run: bool = True  # simulate publishing instead of calling X
    ai_reply_approval_obtained: bool = False  # prior written approval from X
    ai_reply_publishing_enabled: bool = False
    max_original_posts_per_day: int = 3
    max_replies_per_day: int = 5
    max_posts_per_run: int = 1
    min_hours_between_posts: float = 3.0
    publish_min_score: float = 0.5

    # --- research ----------------------------------------------------------
    rss_feeds: str = ""
    github_repositories: str = ""
    github_search_queries: str = ""
    github_token: SecretStr | None = None
    github_include_prereleases: bool = False
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 2
    http_user_agent: str = "tech-x-agent/0.1 (research bot; contact: operator@example.invalid)"
    source_max_bytes: int = 2_000_000
    max_article_fetches_per_run: int = 10
    freshness_window_hours: int = 24
    freshness_half_life_hours: float = 12.0
    max_candidates_per_run: int = 15
    min_candidate_score: float = 0.3
    per_host_min_interval_seconds: float = 5.0
    accent_color: str = "#7c5cff"

    # --- scoring weights (normalised at use time) --------------------------
    weight_freshness: float = 0.30
    weight_technical_relevance: float = 0.23
    weight_novelty: float = 0.2
    weight_source_reliability: float = 0.15
    weight_evidence_quality: float = 0.12
    weight_audience_usefulness: float = 0.08

    # --- retrieval ---------------------------------------------------------
    retrieval_rrf_k: int = 60
    retrieval_vector_limit: int = 30
    retrieval_fts_limit: int = 30
    retrieval_max_results: int = 8
    retrieval_recency_half_life_days: float = 45.0
    retrieval_min_similarity: float = 0.05
    duplicate_similarity_threshold: float = 0.92
    duplicate_jaccard_threshold: float = 0.8
    recent_posts_for_dedup: int = 50

    # --- publishing policy -------------------------------------------------
    max_post_length: int = 280
    max_links_per_post: int = 2
    max_hashtags_per_post: int = 1
    require_verified_claims: bool = True
    verification_stale_days: int = 14
    verification_min_quote_chars: int = 20
    allow_trending_topics: bool = False
    # Applies to factual formats only: shitpost/relatable joke framing is allowed.
    block_first_person_experience: bool = True
    max_thread_parts: int = 4
    # Kept small on purpose: the free tier allows 8000 tokens/minute, and the
    # prompt plus JSON schema already costs roughly 1200 tokens.
    draft_source_context_chars: int = 2600

    # --- persona / content mix --------------------------------------------
    # Which shapes the account posts and how often. Choose-able per run; the
    # application picks, the model never does.
    post_format_mix: str = (
        "straight_news:0.30,political_news:0.18,business_economy:0.16,"
        "explainer:0.12,geopolitics_defence:0.10,quote_led:0.08,breaking_news:0.06"
    )
    banned_phrases: str = (
        "as an ai language model,"
        "i am an ai,"
        "as an ai,"
        "i personally tested,"
        "i personally use,"
        "when i built,"
        "in my experience of building,"
        "click the link in bio,"
        "drop a follow,"
        "follow for more"
    )

    # --- scheduler ---------------------------------------------------------
    scheduler_enabled: bool = False
    ingest_interval_minutes: int = 60
    rank_interval_minutes: int = 120
    draft_interval_minutes: int = 180
    publish_interval_minutes: int = 30
    reply_draft_interval_minutes: int = 60
    consolidation_interval_hours: int = 24
    analytics_interval_hours: int = 6
    job_lease_seconds: int = 600

    # --- analytics ---------------------------------------------------------
    analytics_enabled: bool = False

    # ------------------------------------------------------------------
    # parsing helpers for comma / newline separated env values
    # ------------------------------------------------------------------
    @staticmethod
    def _split_csv(value: str) -> tuple[str, ...]:
        parts: list[str] = []
        for chunk in value.replace(";", ",").replace("\n", ",").split(","):
            item = chunk.strip()
            if item and item not in parts:
                parts.append(item)
        return tuple(parts)

    @property
    def feed_urls(self) -> tuple[str, ...]:
        return self._split_csv(self.rss_feeds)

    @property
    def repositories(self) -> tuple[str, ...]:
        return self._split_csv(self.github_repositories)

    @property
    def github_queries(self) -> tuple[str, ...]:
        return self._split_csv(self.github_search_queries)

    @property
    def blocked_phrases(self) -> tuple[str, ...]:
        return tuple(p.lower() for p in self._split_csv(self.banned_phrases))

    @property
    def scoring_weights(self) -> dict[str, float]:
        raw = {
            "freshness": self.weight_freshness,
            "technical_relevance": self.weight_technical_relevance,
            "novelty": self.weight_novelty,
            "source_reliability": self.weight_source_reliability,
            "evidence_quality": self.weight_evidence_quality,
            "audience_usefulness": self.weight_audience_usefulness,
        }
        total = sum(raw.values())
        return {k: v / total for k, v in raw.items()}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider == "gemini" and bool(self.gemini_api_key) and bool(self.gemini_model)

    @property
    def is_zero_cost_mode(self) -> bool:
        return self.x_publishing_mode in ("local_draft", "brave_cdp") or not self.x_api_enabled

    @property
    def x_write_credentials_present(self) -> bool:
        if self.is_zero_cost_mode:
            return False
        return all(
            [
                self.x_consumer_key,
                self.x_consumer_secret,
                self.x_access_token,
                self.x_access_token_secret,
            ]
        )

    @property
    def admin_auth_required(self) -> bool:
        return bool(self.admin_api_token)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator("rss_feeds")
    @classmethod
    def _validate_feeds(cls, value: str) -> str:
        for url in cls._split_csv(value):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise ValueError(f"RSS feed must be http(s): {url!r}")
            if not parsed.hostname:
                raise ValueError(f"RSS feed missing hostname: {url!r}")
        return value

    @field_validator("github_repositories")
    @classmethod
    def _validate_repos(cls, value: str) -> str:
        for repo in cls._split_csv(value):
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"GitHub repository must be 'owner/name': {repo!r}")
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the asyncpg driver, e.g. "
                "postgresql+asyncpg://user:pass@host:5432/db"
            )
        return value

    @field_validator("embedding_dimensions")
    @classmethod
    def _validate_dims(cls, value: int) -> int:
        if not 8 <= value <= 4096:
            raise ValueError("embedding_dimensions must be between 8 and 4096")
        return value

    @model_validator(mode="after")
    def _validate_controls(self) -> Settings:
        if self.x_publishing_mode == "local_draft":
            if self.publishing_enabled and not self.dry_run:
                raise ValueError(
                    "Cannot enable live X API publishing (PUBLISHING_ENABLED=true and DRY_RUN=false) "
                    "when running in zero-cost mode (X_PUBLISHING_MODE='local_draft' or X_API_ENABLED=false). "
                    "Zero-cost mode strictly prohibits outbound calls to api.x.com."
                )
        elif self.x_publishing_mode == "official_api":
            if not self.x_api_enabled and self.publishing_enabled and not self.dry_run:
                raise ValueError(
                    "Cannot enable live X API publishing (PUBLISHING_ENABLED=true and DRY_RUN=false) "
                    "when X_API_ENABLED=false."
                )
            if self.publishing_enabled and not self.dry_run and not self.x_write_credentials_present:
                raise ValueError(
                    "Live publishing requires full OAuth 1.0a credentials "
                    "(X_CONSUMER_KEY/SECRET and X_ACCESS_TOKEN/SECRET)"
                )
        if self.is_zero_cost_mode and self.enable_reply_scanner:
            raise ValueError(
                "ENABLE_REPLY_SCANNER cannot be true in zero-cost mode: automatic mention "
                "scanning depends on paid/restricted X API reads. Use ENABLE_MANUAL_REPLY_WORKFLOW=true instead."
            )
        if self.ai_reply_publishing_enabled and not self.ai_reply_approval_obtained:
            raise ValueError(
                "AI_REPLY_PUBLISHING_ENABLED requires AI_REPLY_APPROVAL_OBTAINED=true "
                "(prior written approval from X). Refusing to start."
            )
        if self.app_env == "production" and not self.admin_api_token:
            raise ValueError(
                "ADMIN_API_TOKEN is required in production: the admin API must be authenticated"
            )
        return self

    def redacted_dump(self) -> dict[str, object]:
        """Config snapshot safe for logs/readiness output."""
        secret_fields = {
            "gemini_api_key",
            "admin_api_token",
            "github_token",
            "x_consumer_key",
            "x_consumer_secret",
            "x_access_token",
            "x_access_token_secret",
            "x_bearer_token",
        }
        data = self.model_dump()
        out: dict[str, object] = {}
        for key, value in data.items():
            if key in secret_fields:
                out[key] = "set" if value else "unset"
            else:
                out[key] = value
        return out


def load_settings(**overrides: object) -> Settings:
    """Create settings, allowing explicit overrides (used heavily in tests)."""
    return Settings(**overrides)  # type: ignore[arg-type]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None
