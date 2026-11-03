"""Create the account + default persona (idempotent).

Usage: python -m scripts.bootstrap --handle techbrief [--x-user-id 12345]
"""

from __future__ import annotations

import argparse

from app.agent.persona import ensure_default_persona
from app.memory.database import dispose_engine, init_engine, session_scope
from app.memory.repositories import AccountRepository
from scripts._common import prepare


async def main(handle: str | None, x_user_id: str | None) -> None:
    settings = prepare()
    init_engine(settings)
    try:
        resolved = (handle or settings.x_persona_handle or "techbrief").strip().lstrip("@")
        async with session_scope() as session:
            account = await AccountRepository.ensure_account(session, resolved, x_user_id)
            persona = await ensure_default_persona(session, account.id)
            print(
                f"account id={account.id} handle=@{account.handle} "
                f"persona={persona.name} v{persona.version}"
            )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handle", default=None)
    parser.add_argument("--x-user-id", default=None)
    args = parser.parse_args()
    import asyncio

    asyncio.run(main(args.handle, args.x_user_id))
