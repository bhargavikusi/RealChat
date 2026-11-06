"""Profile cards: a profile as other users see it (D-029).

Shared by /profile, the chat partner's card, and People (D-051), so every card shows
the same details in the same way.
"""

from datetime import UTC, date, datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from apps.bot.avatars import Avatars
from backend.modules.users import service
from backend.modules.users.countries import country_name
from backend.modules.users.models import Profile
from backend.modules.users.validation import Gender


def profile_caption(profile: Profile, today: date) -> str:
    """The profile as other users see it: nickname, age, gender, place, and bio."""
    age = service.profile_age(profile, today)
    gender = profile.gender.capitalize() if profile.gender else ""
    place = country_name(profile.country_code) if profile.country_code else ""
    if profile.city:
        place = f"{profile.city}, {place}"
    lines = [f"👤 {profile.nickname}", f"🎂 {age} · {gender}", f"📍 {place}"]
    if profile.bio:
        lines += ["", profile.bio]
    return "\n".join(lines)


async def send_card(
    bot: Bot,
    avatars: Avatars,
    chat_id: int,
    profile: Profile,
    header: str,
    reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
) -> None:
    """Send `profile` as others see it, with the photo or the default avatar."""
    caption = f"{header}\n\n{profile_caption(profile, datetime.now(UTC).date())}"
    if profile.photo_file_id is not None:
        await bot.send_photo(
            chat_id, profile.photo_file_id, caption=caption, reply_markup=reply_markup
        )
        return
    # Onboarding guarantees a gender; "other" is a safe fallback for the avatar only.
    gender = Gender(profile.gender) if profile.gender else Gender.OTHER
    await avatars.send(bot, chat_id, gender, caption, reply_markup)
