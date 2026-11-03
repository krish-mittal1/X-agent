"""Shared script bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.logging_config import configure_logging, get_logger  # noqa: E402


def prepare() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    return settings


__all__ = ["prepare", "get_logger", "Settings"]
