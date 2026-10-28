"""/start, terms acceptance, and conversational onboarding.

Where a user is in onboarding is derived from the database whenever possible:
terms acceptance and the required fields (D-013) are durable, so a user who leaves
or hits a bot restart always resumes at the right required step. FSM state in Redis
records which answer is expected and the position within the optional steps (city,
photo); if it is lost, text is not taken as an answer and the step is asked again.

Rules every handler follows:
* Onboarding works only in private chats, so profile answers are never posted in a group.
* No profile data is saved unless the user has accepted the current terms version;
  a user who has not (e.g. after a terms update) is shown the current terms instead.
* The database transaction is ended before any Telegram API call, so no transaction
  or pooled connection is held across network latency.
"""

from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards import NEW_CHAT, current_keyboard, main_keyboard
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
    # The version shown with the buttons, so an old button cannot accept newer terms.
    version: str
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
TERMS_UPDATED = "The terms have been updated. Please review the current version."
UNDER_AGE_RETRY = "If you entered your age by mistake, send it again."
COUNTRY_NOT_FOUND = "I couldn't find that country. Please type its name in English, e.g. Germany."
COUNTRY_PICK = "Did you mean one of these?"
PHOTO_EXPECTED = "Please send a photo, or tap Skip."
ALREADY_DONE = "Already saved."
MENU_HINT = (
    f"Tap {NEW_CHAT} to meet someone. "
    "Open the menu any time: /profile to view or edit your profile, /help for help."
)
ONBOARDING_COMPLETE = f"You're all set! {MENU_HINT}"
WELCOME_BACK = f"Welcome back! {MENU_HINT}"

REQUIRED_STEPS = {
    "nickname": Onboarding.nickname,
    "age": Onboarding.age,
    "gender": Onboarding.gender,
    "country": Onboarding.country,
}


def _keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _terms_keyboard() -> InlineKeyboardMarkup:
    def button(text: str, accept: bool) -> InlineKeyboardButton:
        data = TermsChoice(version=TERMS_VERSION, accept=accept).pack()
        return InlineKeyboardButton(text=text, callback_data=data)

    return _keyboard([button("I agree", True), button("Decline", False)])


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


def _now() -> datetime:
    return datetime.now(UTC)


class Context:
    """The user's account and profile, loaded at the start of a handler."""

    def __init__(self, user: User, profile: Profile, terms_accepted: bool) -> None:
        self.user = user
        self.profile = profile
        self.terms_accepted = terms_accepted

    @property
    def in_optional_steps(self) -> bool:
        return (
            self.terms_accepted
            and not service.missing_required_fields(self.profile)
            and self.profile.onboarding_completed_at is None
        )


async def load_context(session: AsyncSession, telegram_user: TelegramUser) -> Context:
    """Load the user's state and end the transaction before any Telegram call."""
    user = await service.get_or_create_user(session, telegram_user.id)
    profile = await service.get_profile(session, user.id)
    accepted = await service.has_accepted_terms(session, user.id, TERMS_VERSION)
    await session.commit()
    return Context(user, profile, accepted)


async def prompt_next(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    ctx: Context,
    state: FSMContext,
    settings: BotSettings,
    after: str | None = None,
) -> None:
    """Send the next onboarding prompt. `after` names the optional step just finished.

    Callers must have committed their changes; this function writes only the
    completion timestamp, and commits it before replying.
    """
    if not ctx.terms_accepted:
        await state.set_state(Onboarding.terms)
        await bot.send_message(
            chat_id, terms_text(settings.min_age), reply_markup=_terms_keyboard()
        )
        return

    missing = service.missing_required_fields(ctx.profile)
    if missing:
        step = missing[0]
        await state.set_state(REQUIRED_STEPS[step])
        markup = _gender_keyboard() if step == "gender" else None
        await bot.send_message(chat_id, PROMPTS[step], reply_markup=markup)
        return

    if ctx.profile.onboarding_completed_at is not None:
        keyboard = await current_keyboard(session, ctx.user.id)
        await session.commit()
        await state.clear()
        await bot.send_message(chat_id, WELCOME_BACK, reply_markup=keyboard)
        return

    if after is None:
        await state.set_state(Onboarding.city)
        await bot.send_message(chat_id, PROMPTS["city"], reply_markup=_skip_keyboard("city"))
    elif after == "city":
        await state.set_state(Onboarding.photo)
        await bot.send_message(chat_id, PROMPTS["photo"], reply_markup=_skip_keyboard("photo"))
    else:
        ctx.profile.onboarding_completed_at = _now()
        await session.commit()
        await state.clear()
        await bot.send_message(chat_id, ONBOARDING_COMPLETE, reply_markup=main_keyboard())


async def _gate(
    ctx: Context, bot: Bot, chat_id: int, session: AsyncSession, state: FSMContext, s: BotSettings
) -> bool:
    """True if the user may give profile data; otherwise show the current terms."""
    if ctx.terms_accepted:
        return True
    await prompt_next(bot, chat_id, session, ctx, state, s)
    return False


async def _retry(message: Message, error: ProfileValidationError, step: str) -> None:
    await message.answer(f"{error.message}\n\n{PROMPTS[step]}")


# --- Entry points ------------------------------------------------------------------


async def start(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    await prompt_next(bot, message.chat.id, session, ctx, state, settings)


async def resume(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    """A message no step is waiting for: show the current step again.

    One exception: a photo sent during the optional steps after FSM state was lost is
    still accepted as the profile photo rather than dropped.
    """
    if message.from_user is None:
        return
    ctx = await load_context(session, message.from_user)
    if message.photo and ctx.in_optional_steps and await state.get_state() is None:
        await _save_photo(message, bot, session, ctx, state, settings)
        return
    await prompt_next(bot, message.chat.id, session, ctx, state, settings)


# --- Terms ---------------------------------------------------------------------------


async def on_terms(
    query: CallbackQuery,
    bot: Bot,
    callback_data: TermsChoice,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    ctx = await load_context(session, query.from_user)
    if callback_data.version != TERMS_VERSION:
        # A button from an older version: the user has not seen the current terms.
        await query.answer(TERMS_UPDATED, show_alert=True)
        await prompt_next(bot, query.from_user.id, session, ctx, state, settings)
        return
    if ctx.terms_accepted:
        # A repeated press: nothing to record and no prompt to repeat.
        await query.answer(ALREADY_DONE)
        return
    if not callback_data.accept:
        await state.clear()
        await query.answer()
        await bot.send_message(query.from_user.id, TERMS_DECLINED)
        return
    await service.accept_terms(session, ctx.user.id, TERMS_VERSION)
    await session.commit()
    ctx.terms_accepted = True
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings)


async def terms_pending(message: Message) -> None:
    await message.answer(TERMS_REQUIRED)


# --- Required steps ------------------------------------------------------------------


async def on_nickname(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or message.text is None:
        return
    ctx = await load_context(session, message.from_user)
    if not await _gate(ctx, bot, message.chat.id, session, state, settings):
        return
    try:
        service.set_nickname(ctx.profile, message.text)
    except ProfileValidationError as error:
        await _retry(message, error, "nickname")
        return
    await session.commit()
    await prompt_next(bot, message.chat.id, session, ctx, state, settings)


async def on_age(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or message.text is None:
        return
    ctx = await load_context(session, message.from_user)
    if not await _gate(ctx, bot, message.chat.id, session, state, settings):
        return
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
        service.set_age(ctx.profile, age, minimum_age=settings.min_age, today=_now().date())
    except ProfileValidationError as error:
        await _retry(message, error, "age")
        return
    await session.commit()
    await prompt_next(bot, message.chat.id, session, ctx, state, settings)


async def on_gender(
    query: CallbackQuery,
    bot: Bot,
    callback_data: GenderChoice,
    session: AsyncSession,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    ctx = await load_context(session, query.from_user)
    if not await _gate(ctx, bot, query.from_user.id, session, state, settings):
        await query.answer()
        return
    if service.missing_required_fields(ctx.profile)[:1] != ["gender"]:
        await query.answer(ALREADY_DONE)
        return
    service.set_gender(ctx.profile, callback_data.value)
    await session.commit()
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings)


async def on_country_text(message: Message) -> None:
    # Saves nothing: the choice is confirmed with a button, which is gated.
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
    ctx = await load_context(session, query.from_user)
    if not await _gate(ctx, bot, query.from_user.id, session, state, settings):
        await query.answer()
        return
    if service.missing_required_fields(ctx.profile)[:1] != ["country"]:
        await query.answer(ALREADY_DONE)
        return
    await query.answer()
    if not callback_data.code:
        await bot.send_message(query.from_user.id, PROMPTS["country"])
        return
    try:
        service.set_country(ctx.profile, callback_data.code)
    except ProfileValidationError:
        await bot.send_message(query.from_user.id, COUNTRY_NOT_FOUND)
        return
    await session.commit()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings)


# --- Optional steps ------------------------------------------------------------------


async def on_city(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or message.text is None:
        return
    ctx = await load_context(session, message.from_user)
    if not await _gate(ctx, bot, message.chat.id, session, state, settings):
        return
    try:
        service.set_city(ctx.profile, message.text)
    except ProfileValidationError as error:
        await message.answer(
            f"{error.message}\n\n{PROMPTS['city']}", reply_markup=_skip_keyboard("city")
        )
        return
    await session.commit()
    await prompt_next(bot, message.chat.id, session, ctx, state, settings, "city")


async def _save_photo(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    ctx: Context,
    state: FSMContext,
    settings: BotSettings,
) -> None:
    if not message.photo:
        return
    # Only Telegram's file identifier of the largest size is stored; never the image.
    service.set_photo(ctx.profile, message.photo[-1].file_id)
    await session.commit()
    await prompt_next(bot, message.chat.id, session, ctx, state, settings, "photo")


async def on_photo(
    message: Message, bot: Bot, session: AsyncSession, state: FSMContext, settings: BotSettings
) -> None:
    if message.from_user is None or not message.photo:
        return
    ctx = await load_context(session, message.from_user)
    if not await _gate(ctx, bot, message.chat.id, session, state, settings):
        return
    await _save_photo(message, bot, session, ctx, state, settings)


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
    ctx = await load_context(session, query.from_user)
    # New terms are shown before anything else, even for a stale Skip button.
    if not await _gate(ctx, bot, query.from_user.id, session, state, settings):
        await query.answer()
        return
    current = await state.get_state()
    expected = {"city": Onboarding.city.state, "photo": Onboarding.photo.state}.get(
        callback_data.step
    )
    # Stale Skip buttons are ignored. If FSM state was lost (current is None) the
    # database still shows whether the user is in the optional steps.
    if expected is None or not ctx.in_optional_steps or current not in (None, expected):
        await query.answer(ALREADY_DONE)
        return
    await query.answer()
    await prompt_next(bot, query.from_user.id, session, ctx, state, settings, callback_data.step)


def create_router() -> Router:
    router = Router(name="onboarding")
    # Private chats only: in a group, prompts and answers would be visible to everyone.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

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
