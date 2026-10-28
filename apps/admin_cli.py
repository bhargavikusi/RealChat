"""Server-side admin command line: the only way to change roles (D-034).

Run on the server, with the same configuration as the bot:

    uv run python -m apps.admin_cli set-role <telegram_user_id> <user|moderator|admin>
    uv run python -m apps.admin_cli revoke <telegram_user_id>
    uv run python -m apps.admin_cli list

The account must already exist, i.e. the person has started the bot. Every change is
written to the admin audit log with no actor account, meaning "the server CLI".
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config.settings import SettingsError, load_settings
from backend.database.engine import create_engine, create_session_factory
from backend.modules.admin import service
from backend.modules.admin.service import AdminError
from backend.modules.users.models import ROLES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admin_cli", description="Manage RealChat roles.")
    commands = parser.add_subparsers(dest="command", required=True)
    set_role = commands.add_parser("set-role", help="give an account a role")
    set_role.add_argument("telegram_user_id", type=int)
    set_role.add_argument("role", choices=ROLES)
    revoke = commands.add_parser("revoke", help="set an account back to the user role")
    revoke.add_argument("telegram_user_id", type=int)
    commands.add_parser("list", help="list moderators and admins")
    return parser


async def run(argv: Sequence[str], sessions: async_sessionmaker[AsyncSession]) -> int:
    """Run one command; returns the process exit code."""
    args = _parser().parse_args(argv)
    async with sessions() as session:
        if args.command == "list":
            staff = await service.list_staff(session)
            for telegram_user_id, role, status in staff:
                print(f"{telegram_user_id}\t{role}\t{status}")
            if not staff:
                print("No moderators or admins.")
            return 0
        role = "user" if args.command == "revoke" else args.role
        try:
            old = await service.set_role(session, args.telegram_user_id, role)
        except AdminError as error:
            print(error.message, file=sys.stderr)
            return 1
        await session.commit()
    if old == role:
        print(f"{args.telegram_user_id} already has the {role} role. Nothing changed.")
    else:
        print(f"{args.telegram_user_id}: {old} -> {role}")
    return 0


async def _main(argv: Sequence[str]) -> int:
    try:
        settings = load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        return await run(argv, create_session_factory(engine))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
