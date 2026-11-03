"""Inspect or update the account persona.

Usage:
  python -m scripts.persona --show
  python -m scripts.persona --file persona.json

The JSON file accepts: name, system_prompt, style_guide, interests[], boundaries[].
A new active version is created and the previous version is kept for audit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.agent.persona import ensure_default_persona, load_persona
from app.memory.database import dispose_engine, init_engine, session_scope
from app.memory.models import AccountPersona
from app.memory.repositories import AccountRepository
from scripts._common import prepare


class PersonaUpdate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=64)
    system_prompt: str = Field(min_length=80)
    style_guide: str = Field(min_length=20)
    interests: list[str] = Field(min_length=1, max_length=30)
    boundaries: list[str] = Field(min_length=1, max_length=30)


async def main(show: bool, file_path: str | None) -> None:
    settings = prepare()
    init_engine(settings)
    try:
        async with session_scope() as session:
            account = await AccountRepository.get_default(session, settings.x_persona_handle)
            if account is None:
                raise SystemExit("no account: run python -m scripts.bootstrap first")
            if show or file_path is None:
                persona = await load_persona(session, account.id)
                print(json.dumps(
                    {
                        "account": account.handle,
                        "name": persona.name,
                        "version": persona.version,
                        "system_prompt": persona.system_prompt,
                        "style_guide": persona.style_guide,
                        "interests": list(persona.interests),
                        "boundaries": list(persona.boundaries),
                    },
                    indent=2,
                ))
                return
            raw = await asyncio.to_thread(Path(file_path).read_text, "utf-8")
            update = PersonaUpdate.model_validate_json(raw)
            active = await session.scalar(
                select(AccountPersona).where(
                    AccountPersona.account_id == account.id,
                    AccountPersona.is_active.is_(True),
                )
            )
            if active is None:
                active = await ensure_default_persona(session, account.id)
            current_max = await session.scalar(
                select(func.coalesce(func.max(AccountPersona.version), 0)).where(
                    AccountPersona.account_id == account.id
                )
            )
            active.is_active = False
            await session.flush()
            session.add(
                AccountPersona(
                    account_id=account.id,
                    name=update.name,
                    version=int(current_max or 0) + 1,
                    system_prompt=update.system_prompt,
                    style_guide=update.style_guide,
                    interests=update.interests,
                    boundaries=update.boundaries,
                    is_active=True,
                )
            )
            await session.flush()
            print(f"persona updated for @{account.handle} (version {int(current_max or 0) + 1})")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--file", dest="file_path", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.show, args.file_path))
