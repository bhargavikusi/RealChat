"""Server-side admin command line: the only way to change roles (D-034).

Run on the server, with the same configuration as the bot:

    uv run python -m apps.admin_cli set-role <telegram_user_id> <user|moderator|admin>
    uv run python -m apps.admin_cli revoke <telegram_user_id>
    uv run python -m apps.admin_cli list
    uv run python -m apps.admin_cli coins <telegram_user_id> <amount> --reason "<why>" [--key <id>]
    uv run python -m apps.admin_cli balance <telegram_user_id>
    uv run python -m apps.admin_cli purchases <telegram_user_id>
    uv run python -m apps.admin_cli refund <purchase_id> --reason "<why>"
    uv run python -m apps.admin_cli reconcile
    uv run python -m apps.admin_cli record-payment <charge_id>

`coins` grants coins (a positive amount) or corrects a balance (a negative amount), for
promotions or support (D-050); the reason is kept with the ledger entry. With --key,
running the same adjustment again (for example after an unclear failure) changes nothing.

`purchases` lists an account's Stars purchases, newest first, with their ids. `refund`
refunds one (D-065): it takes back the purchase's coins (refused if any were spent) and a
VIP pass's days now, and the worker returns the Stars within about a minute,
retrying until Telegram accepts. Running it again changes nothing.

`reconcile` compares stored payments with Telegram's list of the bot's Stars
transactions and reports every difference (D-066). It changes nothing, and needs the
bot token, like the bot. `record-payment` records a payment Telegram has that was never
recorded, as its confirmation would have (D-068), and sends the user the usual receipt;
it also needs the bot token.

The account must already exist, i.e. the person has started the bot. Every change is
written to the admin audit log with no actor account, meaning "the server CLI".
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import StarTransaction, TransactionPartnerUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot import payments as bot_payments
from apps.bot.main import create_bot
from backend.config.settings import BotSettings, SettingsError, load_bot_settings, load_settings
from backend.database.engine import create_engine, create_session_factory
from backend.modules.admin import service
from backend.modules.admin.service import AdminError
from backend.modules.payments.reconcile import (
    Difference,
    Report,
    TelegramTransaction,
    reconcile,
)
from backend.modules.payments.service import Outcome, Purchase
from backend.modules.users.models import ROLES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin_cli", description="Manage RealChat roles, coins, and purchases."
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
        "--key",
        help="an id for this one adjustment; running it again with the same key does nothing",
    )
    show = commands.add_parser("balance", help="show an account's coins")
    show.add_argument("telegram_user_id", type=int)
    bought = commands.add_parser("purchases", help="list an account's Stars purchases")
    bought.add_argument("telegram_user_id", type=int)
    refund = commands.add_parser("refund", help="refund a Stars purchase")
    refund.add_argument("purchase_id", type=int)
    refund.add_argument("--reason", required=True, help="why, kept with the purchase")
    commands.add_parser(
        "reconcile", help="compare stored payments with Telegram's Stars transactions"
    )
    record = commands.add_parser(
        "record-payment", help="record a payment Telegram has that was never recorded"
    )
    record.add_argument("charge_id")
    return parser


def _purchase_line(purchase: Purchase) -> str:
    what = (
        f"VIP {purchase.days} days + {purchase.coins} coins"
        if purchase.product == "vip"
        else f"{purchase.coins} coins"
    )
    status = purchase.status
    if purchase.refund_reason is not None:
        status += f" ({purchase.refund_reason})"
    if purchase.refund_shortfall:
        status += f", {purchase.refund_shortfall} coins not recovered"
    return (
        f"#{purchase.payment_id}\t{purchase.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC\t"
        f"{purchase.stars} Stars\t{what}\t{status}"
    )


async def _purchases(session: AsyncSession, args: argparse.Namespace) -> int:
    try:
        if args.command == "purchases":
            found = await service.purchases(session, args.telegram_user_id)
            for purchase in found:
                print(_purchase_line(purchase))
            if not found:
                print("No purchases.")
            return 0
        taken = await service.refund_purchase(
            session, args.purchase_id, args.reason, datetime.now(UTC)
        )
    except AdminError as error:
        print(error.message, file=sys.stderr)
        return 1
    await session.commit()
    if taken is None:
        print("That purchase is already refunded or being refunded. Nothing changed.")
        return 0
    parts = [f"Purchase #{args.purchase_id}: {taken.coins} coins taken back"]
    if taken.vip_days_removed:
        parts.append("its VIP days removed")
    print(", ".join(parts) + ". The worker will return the Stars.")
    return 0


# getStarTransactions returns at most this many per call.
TRANSACTIONS_PAGE = 100


def _user_transaction(transaction: StarTransaction) -> TelegramTransaction | None:
    """A user's payment (incoming, for an invoice) or a refund to a user (outgoing, under
    the payment's id); None for anything else."""
    if (
        isinstance(transaction.source, TransactionPartnerUser)
        and transaction.source.transaction_type == "invoice_payment"
    ):
        partner, refund = transaction.source, False
    elif isinstance(transaction.receiver, TransactionPartnerUser):
        # Gifts and the like have ids of their own, so they match no payment.
        partner, refund = transaction.receiver, True
    else:
        return None
    return TelegramTransaction(
        transaction.id,
        partner.user.id,
        transaction.amount,
        refund,
        transaction.date.astimezone(UTC),
        None if refund else partner.invoice_payload,
    )


async def fetch_transactions(bot: Bot) -> list[TelegramTransaction]:
    """Every user payment and refund Telegram lists for the bot, page by page."""
    found: list[TelegramTransaction] = []
    offset = 0
    while True:
        page = (
            await bot.get_star_transactions(offset=offset, limit=TRANSACTIONS_PAGE)
        ).transactions
        found += [t for t in map(_user_transaction, page) if t is not None]
        if len(page) < TRANSACTIONS_PAGE:
            return found
        offset += len(page)


def _count(number: int, noun: str) -> str:
    return f"{number or 'no'} {noun}{'' if number == 1 else 's'}"


def _print_report(report: Report) -> None:
    for found in report.differences:
        detail = f" ({found.detail})" if found.detail else ""
        print(f"{found.charge_id}\tuser {found.telegram_user_id}\t{found.difference}{detail}")
    print(
        f"Checked {_count(report.payments, 'payment')} at Telegram: "
        f"{_count(len(report.differences), 'difference')}, "
        f"{_count(report.refunds_owed, 'refund')} still owed for the worker to send."
    )
    if any(found.difference is Difference.NOT_RECORDED for found in report.differences):
        print(
            "A payment confirmed in the last few minutes may show as not recorded until "
            "the bot records it. Otherwise, record one with: record-payment <charge_id>"
        )


async def _reconcile(session: AsyncSession, bot: Bot) -> int:
    try:
        transactions = await fetch_transactions(bot)
    except TelegramAPIError as error:
        print(f"Telegram refused the request: {error.message}", file=sys.stderr)
        return 1
    _print_report(await reconcile(session, transactions))
    # Read-only: the session is closed without committing.
    return 0


async def _record_payment(session: AsyncSession, bot: Bot, charge_id: str) -> int:
    try:
        transactions = await fetch_transactions(bot)
    except TelegramAPIError as error:
        print(f"Telegram refused the request: {error.message}", file=sys.stderr)
        return 1
    found = [t for t in transactions if t.charge_id == charge_id]
    payment = next((t for t in found if not t.refund), None)
    if payment is None:
        print("Telegram doesn't list that charge as a user's payment.", file=sys.stderr)
        return 1
    if any(t.refund for t in found):
        print(
            "Telegram already refunded that payment, so there is nothing to record.",
            file=sys.stderr,
        )
        return 1
    try:
        fulfilled = await service.record_payment(session, payment, datetime.now(UTC))
    except AdminError as error:
        print(error.message, file=sys.stderr)
        return 1
    await session.commit()
    if fulfilled.outcome == Outcome.CREDITED:
        granted = (
            f"VIP until {fulfilled.vip_until:%Y-%m-%d %H:%M} UTC and {fulfilled.coins} coins"
            if fulfilled.vip_until is not None
            else f"{fulfilled.coins} coins"
        )
        print(f"Recorded as purchase #{fulfilled.payment_id}: {granted} granted.")
    else:
        print(
            f"Recorded as purchase #{fulfilled.payment_id}: the account was deleted, so the "
            "Stars are owed back and are being refunded."
        )
    try:
        await bot_payments.settle(
            bot, session, fulfilled, payment.telegram_user_id, payment.charge_id, payment.stars
        )
    except TelegramAPIError as error:
        # Recorded either way; a refund still owed is retried by the worker.
        print(f"Couldn't tell the user: {error.message}", file=sys.stderr)
    return 0


async def run(
    argv: Sequence[str], sessions: async_sessionmaker[AsyncSession], bot: Bot | None = None
) -> int:
    """Run one command; returns the process exit code. `reconcile` and `record-payment`
    need `bot`."""
    args = _parser().parse_args(argv)
    async with sessions() as session:
        if args.command in ("reconcile", "record-payment"):
            if bot is None:
                raise ValueError(f"{args.command} needs a bot")
            if args.command == "record-payment":
                return await _record_payment(session, bot, args.charge_id)
            return await _reconcile(session, bot)
        if args.command in ("purchases", "refund"):
            return await _purchases(session, args)
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
    reconciling = _parser().parse_args(argv).command in ("reconcile", "record-payment")
    try:
        settings = load_bot_settings() if reconciling else load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_engine(settings.database_url.get_secret_value())
    bot = create_bot(settings) if isinstance(settings, BotSettings) else None
    try:
        return await run(argv, create_session_factory(engine), bot)
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
