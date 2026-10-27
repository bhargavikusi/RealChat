"""/start, terms acceptance, and conversational onboarding.

Where a user is in onboarding is derived from the database whenever possible:
terms acceptance and the required fields (D-013) are durable, so a user who leaves
or hits a bot restart always resumes at the right required step. Only the position
within the optional steps (city, photo) lives in FSM state in Redis; if that is lost
the optional steps are simply offered again.

Handlers commit before replying, so no transaction stays open across Telegram calls.
"""

from datetime import UTC, date, datetime

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.types import (
    User as TelegramUser,
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import BotSettings
from backend.modules.users import service
from backend.modules.users.countries import match_country
from backend.modules.users.models import Profile, User
from backend.modules.users.terms import TERMS_VERSION, terms_text
from backend.modules.users.validation import Gender, ProfileValidationError, parse_age


class Onboarding(StatesGroup):
    terms = State()
    nickname = State()
    age = State()
    gender = State()
    country = State()
    city = State()
    photo = State()


class TermsChoice(CallbackData, prefix="terms"):
    accept: bool


class GenderChoice(CallbackData, prefix="gender"):
    value: Gender


class CountryChoice(CallbackData, prefix="country"):
    code: str  # empty string means "none of these"


class SkipStep(CallbackData, prefix="skip"):
    step: str


PROMPTS = {
    "nickname": (
        "Choose a nickname that others will see (2-20 characters).\n"
        "Your Telegram name is never shown to other users."
    ),
    "age": "How old are you? Send your age as a number.",
    "gender": "What is your gender?",
    "country": "Which country are you in? Type its name, e.g. India.",
    "city": "Which city? This is optional and you can add it later.",
    "photo": "Send a profile photo if you'd like one. This is optional.",
}
TERMS_DECLINED = (
    "You need to accept the terms to use RealChat. Send /start whenever you change your mind."
)
TERMS_REQUIRED = "Please accept the terms first."
UNDER_AGE_RETRY = "If you entered your age by mistake, send it again."
COUNTRY_NOT_FOUND = "I couldn't find that country. Please type its name in English, e.g. Germany."
COUNTRY_PICK = "Did you mean one of these?"
PHOTO_EXPECTED = "Please send a photo, or tap Skip."
ALREADY_DONE = "Already saved."
ONBOARDING_COMPLETE = "You're all set! The main menu is coming soon."
WELCOME_BACK = "Welcome back! The main menu is coming soon."

REQUIRED_STEPS = {
    "nickname": Onboarding.nickname,
    "age": Onboarding.age,
    "gender": Onboarding.gender,
    "country": Onboarding.country,
}


def _keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _terms_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            InlineKeyboardButton(text="I agree", callback_data=TermsChoice(accept=True).pack()),
            InlineKeyboardButton(text="Decline", callback_data=TermsChoice(accept=False).pack()),
        ]
    )


def _gender_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            InlineKeyboardButton(
                text=g.value.capitalize(), callback_data=GenderChoice(value=g).pack()
            )
            for g in Gender
        ]
    )


def _skip_keyboard(step: str) -> InlineKeyboardMarkup:
    return _keyboard([InlineKeyboardButton(text="Skip", callback_data=SkipStep(step=step).pack())])


async def _load(session: AsyncSession, telegram_user: TelegramUser) -> tuple[User, Profile]:
    user = await service.get_or_create_user(session, telegram_user.id)
    return user, await service.get_profile(session, user.id)


async def prompt_next(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    profile: Profile,
    state: FSMContext,
    settings: BotSettings,
    after: str | None = None,
) -> None:
    """Send the next onboarding prompt. `after` names the optional step just finished."""
    if not await service.has_accepted_terms(session, user.id, TERMS_VERSION):
        await state.set_state(Onboarding.terms)
        await bot.send_message(
            chat_id, terms_text(settings.min_age), reply_markup=_terms_keyboard()
        )
        return

    missing = service.missing_required_fields(profile)
    if missing:
        step = missing[0]
        await state.set_state(REQUIRED_STEPS[step])
        markup = _gender_keyboard() if step == "gender" else None
        await bot.send_message(chat_id, PROMPTS[step], reply_markup=markup)
        return

    if profile.onboarding_completed_at is not None:
        await state.clear()
        await bot.send_message(chat_id, WELCOME_BACK)
        return

    if after is None:
        await state.set_state(Onboarding.city)
        await bot.send_message(chat_id, PROMPTS["city"], reply_markup=_skip_keyboard("city"))
    elif after == "city":
        await state.set_state(Onboarding.photo)
        await bot.send_message(chat_id, PROMPTS["photo"], reply_markup=_skip_keyboard("photo"))
    else:
        profile.onboarding_completed_at = datetime.now(UTC)
        await session.commit()
        await state.clear()
        await bot.send_message(chat_id, ONBOARDING_COMPLETE)


async def _retry(message: Message, error: ProfileValidationError, step: str) -> None:
    await message.answer(f"{error.message}\n\n{PROMPTS[step]}")


# --- Entry points ------------------------------------------------------------------


async def start(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None:
        return
    user, profile = await _load(session, message.from_user)
    await session.commit()
    await prompt_next(bot, message.chat.id, session, user, profile, state, settings)


async def resume(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    """Any message that no step is waiting for: show the current step again."""
    await start(message, bot, session, state, settings)


# --- Terms ---------------------------------------------------------------------------


async def on_terms(
    query: CallbackQuery,
    bot: Bot,
    callback_data: TermsChoice,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user, profile = await _load(session, query.from_user)
    if await service.has_accepted_terms(session, user.id, TERMS_VERSION):
        # A repeated or stale press: nothing to record and no prompt to repeat.
        await query.answer(ALREADY_DONE)
        return
    if not callback_data.accept:
        await state.clear()
        await query.answer()
        await bot.send_message(query.from_user.id, TERMS_DECLINED)
        return
    await service.accept_terms(session, user.id, TERMS_VERSION)
    await session.commit()
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, user, profile, state, settings)


async def terms_pending(message: Message) -> None:
    await message.answer(TERMS_REQUIRED)


# --- Required steps ------------------------------------------------------------------


async def on_nickname(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or message.text is None:
        return
    user, profile = await _load(session, message.from_user)
    try:
        service.set_nickname(profile, message.text)
    except ProfileValidationError as error:
        await _retry(message, error, "nickname")
        return
    await session.commit()
    await prompt_next(bot, message.chat.id, session, user, profile, state, settings)


async def on_age(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or message.text is None:
        return
    user, profile = await _load(session, message.from_user)
    try:
        age = parse_age(message.text)
    except ProfileValidationError as error:
        await _retry(message, error, "age")
        return
    if age < settings.min_age:
        # D-017: under-age users are refused but may correct a mistaken age.
        await message.answer(
            f"You must be at least {settings.min_age} to use RealChat.\n\n{UNDER_AGE_RETRY}"
        )
        return
    try:
        service.set_age(profile, age, minimum_age=settings.min_age, today=date.today())
    except ProfileValidationError as error:
        await _retry(message, error, "age")
        return
    await session.commit()
    await prompt_next(bot, message.chat.id, session, user, profile, state, settings)


async def on_gender(
    query: CallbackQuery,
    bot: Bot,
    callback_data: GenderChoice,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user, profile = await _load(session, query.from_user)
    if profile.gender is not None or service.missing_required_fields(profile)[:1] != ["gender"]:
        await query.answer(ALREADY_DONE)
        return
    service.set_gender(profile, callback_data.value)
    await session.commit()
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, user, profile, state, settings)


async def on_country_text(message: Message) -> None:
    if message.text is None:
        return
    matches = match_country(message.text)
    if not matches:
        await message.answer(COUNTRY_NOT_FOUND)
        return
    rows = [
        [InlineKeyboardButton(text=m.name, callback_data=CountryChoice(code=m.code).pack())]
        for m in matches
    ]
    rows.append(
        [InlineKeyboardButton(text="None of these", callback_data=CountryChoice(code="").pack())]
    )
    await message.answer(COUNTRY_PICK, reply_markup=_keyboard(*rows))


async def on_country_choice(
    query: CallbackQuery,
    bot: Bot,
    callback_data: CountryChoice,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user, profile = await _load(session, query.from_user)
    if profile.country_code is not None or service.missing_required_fields(profile)[:1] != [
        "country"
    ]:
        await query.answer(ALREADY_DONE)
        return
    await query.answer()
    if not callback_data.code:
        await bot.send_message(query.from_user.id, PROMPTS["country"])
        return
    try:
        service.set_country(profile, callback_data.code)
    except ProfileValidationError:
        await bot.send_message(query.from_user.id, COUNTRY_NOT_FOUND)
        return
    await session.commit()
    await prompt_next(bot, query.from_user.id, session, user, profile, state, settings)


# --- Optional steps ------------------------------------------------------------------


async def on_city(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or message.text is None:
        return
    user, profile = await _load(session, message.from_user)
    try:
        service.set_city(profile, message.text)
    except ProfileValidationError as error:
        await message.answer(
            f"{error.message}\n\n{PROMPTS['city']}", reply_markup=_skip_keyboard("city")
        )
        return
    await session.commit()
    await prompt_next(bot, message.chat.id, session, user, profile, state, settings, "city")


async def on_photo(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or not message.photo:
        return
    user, profile = await _load(session, message.from_user)
    # Only Telegram's file identifier is stored; the image itself is never downloaded.
    service.set_photo(profile, message.photo[-1].file_id)
    await session.commit()
    await prompt_next(bot, message.chat.id, session, user, profile, state, settings, "photo")


async def photo_expected(message: Message) -> None:
    await message.answer(PHOTO_EXPECTED, reply_markup=_skip_keyboard("photo"))


async def on_skip(
    query: CallbackQuery,
    bot: Bot,
    callback_data: SkipStep,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    user, profile = await _load(session, query.from_user)
    current = await state.get_state()
    expected = {"city": Onboarding.city.state, "photo": Onboarding.photo.state}.get(
        callback_data.step
    )
    in_optional_steps = (
        profile.onboarding_completed_at is None and not service.missing_required_fields(profile)
    )
    # Stale Skip buttons are ignored. If FSM state was lost (current is None) the
    # database still shows whether the user is in the optional steps.
    if expected is None or not in_optional_steps or current not in (None, expected):
        await query.answer(ALREADY_DONE)
        return
    await query.answer()
    await prompt_next(
        bot, query.from_user.id, session, user, profile, state, settings, callback_data.step
    )


def create_router() -> Router:
    router = Router(name="onboarding")
    router.message.register(start, CommandStart())

    router.callback_query.register(on_terms, TermsChoice.filter())
    router.callback_query.register(on_gender, GenderChoice.filter())
    router.callback_query.register(on_country_choice, CountryChoice.filter())
    router.callback_query.register(on_skip, SkipStep.filter())

    router.message.register(terms_pending, Onboarding.terms)
    # Commands (e.g. /help) are never taken as answers to a text step.
    answer = F.text & ~F.text.startswith("/")
    router.message.register(on_nickname, Onboarding.nickname, answer)
    router.message.register(on_age, Onboarding.age, answer)
    router.message.register(on_country_text, Onboarding.country, answer)
    router.message.register(on_city, Onboarding.city, answer)
    router.message.register(on_photo, Onboarding.photo, F.photo)
    router.message.register(photo_expected, Onboarding.photo)
    # Anything else (stickers during a text step, messages with no state, etc.).
    router.message.register(resume)
    return router
