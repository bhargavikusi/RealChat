"""/profile: view and edit the user's own profile (D-019 to D-021).

The same rules as onboarding apply:
* Private chats only.
* Nothing is saved unless the current terms version is accepted.
* The database transaction is ended before any Telegram API call.

Profile management opens once onboarding is complete; until then the user is
sent back to their onboarding step. Which field is being edited is kept in FSM
state; buttons from older messages that no longer match it are answered as
expired and change nothing.
"""

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.avatars import Avatars
from apps.bot.onboarding import (
    COUNTRY_NOT_FOUND,
    COUNTRY_PICK,
    UNDER_AGE_RETRY,
    Context,
    load_context,
    prompt_next,
)
from backend.config.settings import BotSettings
from backend.modules.users import service
from backend.modules.users.countries import country_name, match_country
from backend.modules.users.models import Profile
from backend.modules.users.validation import (
    BIO_MAX_LENGTH,
    NICKNAME_MAX_LENGTH,
    NICKNAME_MIN_LENGTH,
    Gender,
    ProfileValidationError,
    parse_age,
)


class ProfileField(StrEnum):
    NICKNAME = "nickname"
    AGE = "age"
    GENDER = "gender"
    COUNTRY = "country"
    CITY = "city"
    BIO = "bio"
    PHOTO = "photo"


class ProfileEdit(StatesGroup):
    nickname = State()
    age = State()
    gender = State()
    country = State()
    city = State()
    bio = State()
    photo = State()


class EditField(CallbackData, prefix="pedit"):
    field: ProfileField


class RemoveField(CallbackData, prefix="premove"):
    field: ProfileField


class CancelEdit(CallbackData, prefix="pcancel"):
    field: ProfileField


class PickGender(CallbackData, prefix="pgender"):
    value: Gender


class PickCountry(CallbackData, prefix="pcountry"):
    code: str  # empty string means "none of these"


EDIT_STATES = {
    ProfileField.NICKNAME: ProfileEdit.nickname,
    ProfileField.AGE: ProfileEdit.age,
    ProfileField.GENDER: ProfileEdit.gender,
    ProfileField.COUNTRY: ProfileEdit.country,
    ProfileField.CITY: ProfileEdit.city,
    ProfileField.BIO: ProfileEdit.bio,
    ProfileField.PHOTO: ProfileEdit.photo,
}
COOLDOWN_FIELDS = (ProfileField.AGE, ProfileField.GENDER)
REMOVABLE_FIELDS = (ProfileField.CITY, ProfileField.BIO, ProfileField.PHOTO)

BUTTON_LABELS = {
    ProfileField.NICKNAME: "✏️ Nickname",
    ProfileField.AGE: "🎂 Age",
    ProfileField.GENDER: "⚧ Gender",
    ProfileField.COUNTRY: "🌍 Country",
    ProfileField.CITY: "📍 City",
    ProfileField.BIO: "📝 Bio",
    ProfileField.PHOTO: "🖼 Photo",
}
EDIT_PROMPTS = {
    ProfileField.NICKNAME: (
        f"Send your new nickname ({NICKNAME_MIN_LENGTH}-{NICKNAME_MAX_LENGTH} characters)."
    ),
    ProfileField.AGE: "Send your age as a number.",
    ProfileField.GENDER: "Choose your gender.",
    ProfileField.COUNTRY: "Type your country's name, e.g. India.",
    ProfileField.CITY: "Send your city.",
    ProfileField.BIO: (
        f"Send a short bio (up to {BIO_MAX_LENGTH} characters). "
        "Links and @usernames aren't allowed."
    ),
    ProfileField.PHOTO: "Send a new profile photo.",
}
PROFILE_HEADER = "Your profile, as others see it:"
SAVED = "✅ Saved."
REMOVED = "✅ Removed."
CANCELLED = "Cancelled. Nothing was changed."
NOTHING_TO_REMOVE = "There's nothing to remove."
EXPIRED = "This button has expired. Send /profile to start again."
TEXT_EXPECTED = "Please send text, or tap Cancel."
CHOICE_EXPECTED = "Please tap one of the options, or Cancel."
PHOTO_EXPECTED = "Please send a photo, or tap Cancel."


def cooldown_note(field: ProfileField, days: int) -> str:
    return f"You can change your {field.value} once every {days} days."


def cooldown_active(field: ProfileField, available_at: datetime) -> str:
    day = available_at.astimezone(UTC)
    return f"You can change your {field.value} again on {day.day} {day:%B %Y} (UTC)."


def unchanged(field: ProfileField) -> str:
    return f"That's already your {field.value}."


def _now() -> datetime:
    return datetime.now(UTC)


def _cooldown(settings: BotSettings) -> timedelta:
    return timedelta(days=settings.profile_change_cooldown_days)


def _button(text: str, data: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def _keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _edit_keyboard() -> InlineKeyboardMarkup:
    def edit(field: ProfileField) -> InlineKeyboardButton:
        return _button(BUTTON_LABELS[field], EditField(field=field))

    return _keyboard(
        [edit(ProfileField.NICKNAME), edit(ProfileField.AGE), edit(ProfileField.GENDER)],
        [edit(ProfileField.COUNTRY), edit(ProfileField.CITY), edit(ProfileField.BIO)],
        [edit(ProfileField.PHOTO)],
    )


def _cancel_row(field: ProfileField) -> list[InlineKeyboardButton]:
    return [_button("Cancel", CancelEdit(field=field))]


def _current_value(profile: Profile, field: ProfileField) -> str | None:
    values = {
        ProfileField.CITY: profile.city,
        ProfileField.BIO: profile.bio,
        ProfileField.PHOTO: profile.photo_file_id,
    }
    return values.get(field)


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


async def _send_card(
    bot: Bot, chat_id: int, profile: Profile, avatars: Avatars, header: str
) -> None:
    caption = f"{header}\n\n{profile_caption(profile, _now().date())}"
    markup = _edit_keyboard()
    if profile.photo_file_id is not None:
        await bot.send_photo(chat_id, profile.photo_file_id, caption=caption, reply_markup=markup)
        return
    # Onboarding guarantees a gender; "other" is a safe fallback for the avatar only.
    gender = Gender(profile.gender) if profile.gender else Gender.OTHER
    await avatars.send(bot, chat_id, gender, caption, markup)


async def _ready(
    ctx: Context, bot: Bot, chat_id: int, session: AsyncSession, state: FSMContext, s: BotSettings
) -> bool:
    """True once the user has accepted the current terms and finished onboarding.

    Otherwise the user is shown the current terms or their onboarding step.
    """
    if ctx.terms_accepted and ctx.profile.onboarding_completed_at is not None:
        return True
    await prompt_next(bot, chat_id, session, ctx, state, s)
    return False


async def _editing(state: FSMContext, field: ProfileField) -> bool:
    return await state.get_state() == EDIT_STATES[field].state


# --- Viewing -------------------------------------------------------------------------


async def show_profile(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    if not await _ready(ctx, bot, message.chat.id, session, state, settings):
        return
    await state.clear()
    await _send_card(bot, message.chat.id, ctx.profile, avatars, PROFILE_HEADER)


# --- Starting, cancelling, and removing ----------------------------------------------


async def on_edit(
    query: CallbackQuery,
    bot: Bot,
    callback_data: EditField,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    chat_id = query.from_user.id
    ctx = await load_context(session, query.from_user)
    if not await _ready(ctx, bot, chat_id, session, state, settings):
        await query.answer()
        return
    field = callback_data.field
    prompt = EDIT_PROMPTS[field]
    if field in COOLDOWN_FIELDS:
        available_at = await service.change_available_at(
            session, ctx.user.id, field.value, _cooldown(settings)
        )
        await session.commit()
        if available_at is not None and _now() < available_at:
            await query.answer()
            await bot.send_message(chat_id, cooldown_active(field, available_at))
            return
        prompt = f"{prompt}\n{cooldown_note(field, settings.profile_change_cooldown_days)}"

    rows: list[list[InlineKeyboardButton]] = []
    if field is ProfileField.GENDER:
        rows.append([_button(g.value.capitalize(), PickGender(value=g)) for g in Gender])
    if field in REMOVABLE_FIELDS and _current_value(ctx.profile, field) is not None:
        rows.append([_button(f"Remove {field.value}", RemoveField(field=field))])
    rows.append(_cancel_row(field))

    await state.set_state(EDIT_STATES[field])
    await query.answer()
    await bot.send_message(chat_id, prompt, reply_markup=_keyboard(*rows))


async def on_cancel(
    query: CallbackQuery, bot: Bot, callback_data: CancelEdit, state: FSMContext
) -> None:
    if not await _editing(state, callback_data.field):
        await query.answer(EXPIRED)
        return
    await state.clear()
    await query.answer()
    await bot.send_message(query.from_user.id, CANCELLED)


async def on_remove(
    query: CallbackQuery,
    bot: Bot,
    callback_data: RemoveField,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    chat_id = query.from_user.id
    ctx = await load_context(session, query.from_user)
    if not await _ready(ctx, bot, chat_id, session, state, settings):
        await query.answer()
        return
    field = callback_data.field
    if field not in REMOVABLE_FIELDS:
        await query.answer(EXPIRED)
        return
    was_editing = await _editing(state, field)
    if _current_value(ctx.profile, field) is None:
        # Already removed, e.g. a repeated press: nothing changes.
        if was_editing:
            await state.clear()
        await query.answer(NOTHING_TO_REMOVE)
        return
    if field is ProfileField.CITY:
        service.set_city(ctx.profile, None)
    elif field is ProfileField.BIO:
        service.set_bio(ctx.profile, None)
    else:
        service.set_photo(ctx.profile, None)
    await session.commit()
    if was_editing:
        await state.clear()
    await query.answer()
    await _send_card(bot, chat_id, ctx.profile, avatars, REMOVED)


# --- Answers -------------------------------------------------------------------------


async def on_text_answer(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    """A new nickname, city, or bio."""
    if message.from_user is None or message.text is None:
        return
    current = await state.get_state()
    ctx = await load_context(session, message.from_user)
    if not await _ready(ctx, bot, message.chat.id, session, state, settings):
        return
    try:
        if current == ProfileEdit.nickname.state:
            field = ProfileField.NICKNAME
            service.set_nickname(ctx.profile, message.text)
        elif current == ProfileEdit.city.state:
            field = ProfileField.CITY
            service.set_city(ctx.profile, message.text)
        else:
            field = ProfileField.BIO
            service.set_bio(ctx.profile, message.text)
    except ProfileValidationError as error:
        await session.rollback()
        await message.answer(
            f"{error.message}\n\n{EDIT_PROMPTS[field]}", reply_markup=_keyboard(_cancel_row(field))
        )
        return
    await session.commit()
    await state.clear()
    await _send_card(bot, message.chat.id, ctx.profile, avatars, SAVED)


async def on_age_answer(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    if message.from_user is None or message.text is None:
        return
    ctx = await load_context(session, message.from_user)
    if not await _ready(ctx, bot, message.chat.id, session, state, settings):
        return
    retry = _keyboard(_cancel_row(ProfileField.AGE))
    try:
        age = parse_age(message.text)
    except ProfileValidationError as error:
        await message.answer(
            f"{error.message}\n\n{EDIT_PROMPTS[ProfileField.AGE]}", reply_markup=retry
        )
        return
    if age < settings.min_age:
        # D-017 applies here too: refused, but a mistaken age can be sent again.
        await message.answer(
            f"You must be at least {settings.min_age} to use RealChat.\n\n{UNDER_AGE_RETRY}",
            reply_markup=retry,
        )
        return
    try:
        changed = await service.change_age(
            session,
            ctx.user.id,
            age,
            minimum_age=settings.min_age,
            now=_now(),
            cooldown=_cooldown(settings),
        )
    except ProfileValidationError as error:
        await session.rollback()
        await message.answer(
            f"{error.message}\n\n{EDIT_PROMPTS[ProfileField.AGE]}", reply_markup=retry
        )
        return
    except service.ChangeCooldownError as error:
        await session.rollback()
        await state.clear()
        await message.answer(cooldown_active(ProfileField.AGE, error.available_at))
        return
    await session.commit()
    await state.clear()
    header = SAVED if changed else unchanged(ProfileField.AGE)
    await _send_card(bot, message.chat.id, ctx.profile, avatars, header)


async def on_gender_pick(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PickGender,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    chat_id = query.from_user.id
    ctx = await load_context(session, query.from_user)
    if not await _ready(ctx, bot, chat_id, session, state, settings):
        await query.answer()
        return
    if not await _editing(state, ProfileField.GENDER):
        # A repeated press after saving, or a button from an older message.
        await query.answer(EXPIRED)
        return
    try:
        changed = await service.change_gender(
            session, ctx.user.id, callback_data.value, now=_now(), cooldown=_cooldown(settings)
        )
    except service.ChangeCooldownError as error:
        await session.rollback()
        await state.clear()
        await query.answer()
        await bot.send_message(chat_id, cooldown_active(ProfileField.GENDER, error.available_at))
        return
    await session.commit()
    await state.clear()
    await query.answer()
    header = SAVED if changed else unchanged(ProfileField.GENDER)
    await _send_card(bot, chat_id, ctx.profile, avatars, header)


async def on_country_text(message: Message) -> None:
    # Saves nothing: the choice is confirmed with a button, which is gated.
    if message.text is None:
        return
    matches = match_country(message.text)
    cancel = _cancel_row(ProfileField.COUNTRY)
    if not matches:
        await message.answer(COUNTRY_NOT_FOUND, reply_markup=_keyboard(cancel))
        return
    rows = [[_button(m.name, PickCountry(code=m.code))] for m in matches]
    rows.append([_button("None of these", PickCountry(code=""))])
    rows.append(cancel)
    await message.answer(COUNTRY_PICK, reply_markup=_keyboard(*rows))


async def on_country_pick(
    query: CallbackQuery,
    bot: Bot,
    callback_data: PickCountry,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    chat_id = query.from_user.id
    ctx = await load_context(session, query.from_user)
    if not await _ready(ctx, bot, chat_id, session, state, settings):
        await query.answer()
        return
    if not await _editing(state, ProfileField.COUNTRY):
        await query.answer(EXPIRED)
        return
    await query.answer()
    cancel = _keyboard(_cancel_row(ProfileField.COUNTRY))
    if not callback_data.code:
        await bot.send_message(chat_id, EDIT_PROMPTS[ProfileField.COUNTRY], reply_markup=cancel)
        return
    try:
        service.set_country(ctx.profile, callback_data.code)
    except ProfileValidationError:
        await session.rollback()
        await bot.send_message(chat_id, COUNTRY_NOT_FOUND, reply_markup=cancel)
        return
    await session.commit()
    await state.clear()
    await _send_card(bot, chat_id, ctx.profile, avatars, SAVED)


async def on_photo_answer(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
    avatars: Avatars,
) -> None:
    if message.from_user is None or not message.photo:
        return
    ctx = await load_context(session, message.from_user)
    if not await _ready(ctx, bot, message.chat.id, session, state, settings):
        return
    # Only Telegram's file identifier of the largest size is stored; never the image.
    service.set_photo(ctx.profile, message.photo[-1].file_id)
    await session.commit()
    await state.clear()
    await _send_card(bot, message.chat.id, ctx.profile, avatars, SAVED)


async def photo_expected(message: Message) -> None:
    await message.answer(PHOTO_EXPECTED, reply_markup=_keyboard(_cancel_row(ProfileField.PHOTO)))


async def text_expected(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    field = next(f for f, s in EDIT_STATES.items() if s.state == current)
    text = CHOICE_EXPECTED if field is ProfileField.GENDER else TEXT_EXPECTED
    await message.answer(text, reply_markup=_keyboard(_cancel_row(field)))


def create_router() -> Router:
    router = Router(name="profile")
    # Private chats only: profile details must never be posted in a group.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    router.message.register(show_profile, Command("profile"))

    router.callback_query.register(on_edit, EditField.filter())
    router.callback_query.register(on_cancel, CancelEdit.filter())
    router.callback_query.register(on_remove, RemoveField.filter())
    router.callback_query.register(on_gender_pick, PickGender.filter())
    router.callback_query.register(on_country_pick, PickCountry.filter())

    # Commands are never taken as answers; /start and others reach their own handlers.
    answer = F.text & ~F.text.startswith("/")
    router.message.register(
        on_text_answer, StateFilter(ProfileEdit.nickname, ProfileEdit.city, ProfileEdit.bio), answer
    )
    router.message.register(on_age_answer, ProfileEdit.age, answer)
    router.message.register(on_country_text, ProfileEdit.country, answer)
    router.message.register(on_photo_answer, ProfileEdit.photo, F.photo)
    not_a_command = ~(F.text.startswith("/"))
    router.message.register(photo_expected, ProfileEdit.photo, not_a_command)
    router.message.register(
        text_expected,
        StateFilter(
            ProfileEdit.nickname,
            ProfileEdit.age,
            ProfileEdit.gender,
            ProfileEdit.country,
            ProfileEdit.city,
            ProfileEdit.bio,
        ),
        not_a_command,
    )
    return router
