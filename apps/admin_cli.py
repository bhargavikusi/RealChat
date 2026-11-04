"""Server-side admin command line: the only way to change roles (D-034).

Run on the server, with the same configuration as the bot:

    uv run python -m apps.admin_cli set-role <telegram_user_id> <user|moderator|admin>
    uv run python -m apps.admin_cli revoke <telegram_user_id>
    uv run python -m apps.admin_cli list
    uv run python -m apps.admin_cli coins <telegram_user_id> <amount> --reason "<why>" [--key <id>]
    uv run python -m apps.admin_cli balance <telegram_user_id>

`coins` grants coins (a positive amount) or corrects a balance (a negative amount), for
promotions or support (D-050); the reason is kept with the ledger entry. With --key,
running the same adjustment again (for example after an unclear failure) changes nothing.

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
    parser = argparse.ArgumentParser(
        prog="admin_cli", description="Manage RealChat roles and coins."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_role = commands.add_parser("set-role", help="give an account a role")
    set_role.add_argument("telegram_user_id", type=int)
    set_role.add_argument("role", choices=ROLES)
    revoke = commands.add_parser("revoke", help="set an account back to the user role")
    revoke.add_argument("telegram_user_id", type=int)
    commands.add_parser("list", help="list moderators and admins")
    adjust = commands.add_parser("coins", help="grant (+) or correct (-) an account's coins")
    adjust.add_argument("telegram_user_id", type=int)
    adjust.add_argument("amount", type=int)
    adjust.add_argument("--reason", required=True, help="why, kept with the ledger entry")
    adjust.add_argument(
        "--key", help="an id for this adjustment; running it again with the same key does nothing"
    )
    show = commands.add_parser("balance", help="show an account's coins")
    show.add_argument("telegram_user_id", type=int)
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
        if args.command in ("coins", "balance"):
            try:
                applied = True
                if args.command == "coins":
                    balance, applied = await service.adjust_coins(
                        session, args.telegram_user_id, args.amount, args.reason, args.key
                    )
                else:
                    balance = await service.coin_balance(session, args.telegram_user_id)
            except AdminError as error:
                print(error.message, file=sys.stderr)
                return 1
            await session.commit()
            if not applied:
                print("That key was already used, so nothing changed.")
            print(
                f"{args.telegram_user_id}: {balance.total} coins "
                f"({balance.held} held, {balance.available} available)"
            )
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
    # Parsed before the configuration is loaded, so --help and usage errors work
    # without a database configured. run() parses the same arguments again.
    _parser().parse_args(argv)
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
