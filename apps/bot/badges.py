"""The VIP badge (D-063): `✅ 👑 VIP` after a VIP's nickname on profile cards and in the
People lists, and `✅` after it in notices that name them. Shown exactly while VIP is
active. It marks VIP bought in RealChat, never a verified identity.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.payments import service as payments
from backend.modules.users import service as users

CARD_BADGE = "✅ 👑 VIP"
NOTICE_MARK = "✅"


def card_name(nickname: str, vip: bool) -> str:
    """A nickname as shown on a card or in a People list."""
    return f"{nickname} {CARD_BADGE}" if vip else nickname


def notice_name(nickname: str, vip: bool) -> str:
    """A nickname as shown in a notice."""
    return f"{nickname} {NOTICE_MARK}" if vip else nickname


async def vip_ids(session: AsyncSession, user_ids: Iterable[int]) -> set[int]:
    """Which of these users are VIP now."""
    return await payments.vip_users(session, user_ids, datetime.now(UTC))


async def is_vip(session: AsyncSession, user_id: int) -> bool:
    return user_id in await vip_ids(session, (user_id,))


async def notice_nickname(session: AsyncSession, user_id: int) -> str:
    """The user's nickname for a notice, marked while they are VIP."""
    nickname = str((await users.get_profile(session, user_id)).nickname)
    return notice_name(nickname, await is_vip(session, user_id))
