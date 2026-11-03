"""API dependencies and authentication."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.memory.database import session_scope


def get_request_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def require_admin(request: Request) -> str:
    """Bearer-token auth for the local administration API.

    The admin API is disabled (503) unless ADMIN_API_TOKEN is configured, so it
    can never be exposed unauthenticated by accident.
    """
    settings: Settings = request.app.state.settings
    if settings.admin_api_token is None:
        # Deliberately generic: the operator-facing hint lives in the README and
        # startup logs, not in an HTTP response that may be internet-visible.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin API is disabled on this instance",
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(token, settings.admin_api_token.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid admin token"
        )
    return "admin"


async def get_db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


AdminAuth = Annotated[str, Depends(require_admin)]
SettingsDep = Annotated[Settings, Depends(get_request_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
