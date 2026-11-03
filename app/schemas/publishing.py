"""Policy decision and publish result schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["hard", "soft", "info"]


class PolicyCheck(BaseModel):
    name: str
    passed: bool
    severity: Severity = "hard"
    detail: str = ""


class PolicyDecision(BaseModel):
    allowed: bool
    checks: list[PolicyCheck] = Field(default_factory=list)
    hard_failures: list[str] = Field(default_factory=list)
    soft_failures: list[str] = Field(default_factory=list)

    @classmethod
    def from_checks(cls, checks: list[PolicyCheck]) -> PolicyDecision:
        hard = [c.detail or c.name for c in checks if not c.passed and c.severity == "hard"]
        soft = [c.detail or c.name for c in checks if not c.passed and c.severity == "soft"]
        return cls(allowed=not hard and not soft, checks=checks, hard_failures=hard, soft_failures=soft)


class PublishResult(BaseModel):
    status: Literal[
        "published",
        "simulated",
        "local_saved",
        "blocked",
        "skipped",
        "already_published",
        "failed",
        "unknown",
    ]
    draft_id: int
    post_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    policy: PolicyDecision | None = None
