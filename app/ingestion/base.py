"""Source adapter protocol and result container."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.schemas.research import IngestedItem


@dataclass
class AdapterResult:
    items: list[IngestedItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rejected: int = 0

    def extend(self, other: AdapterResult) -> None:
        self.items.extend(other.items)
        self.errors.extend(other.errors)
        self.rejected += other.rejected


class SourceAdapter(Protocol):
    name: str

    async def fetch(self) -> AdapterResult: ...


class SourceAdapterError(RuntimeError):
    """Adapter failure that should be logged, not crash discovery."""
