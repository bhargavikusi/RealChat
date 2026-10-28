"""Which Telegram rejections the request middleware ignores (#29)."""

from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from pydantic import SecretStr

from apps.bot.main import create_bot
from apps.bot.telegram_session import IgnoreExpiredCallbackAnswers
from backend.config.settings import BotSettings
from tests.support.telegram import FAKE_TOKEN

ANSWER = AnswerCallbackQuery(callback_query_id="1")
SEND = SendMessage(chat_id=1, text="hi")


def _failing(message: str) -> Any:
    async def make_request(bot: Bot, method: TelegramMethod[Any]) -> Any:
        raise TelegramBadRequest(method=method, message=message)

    return make_request


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: query is too old and response timeout expired or query ID is invalid",
        "Bad Request: QUERY_ID_INVALID query ID is invalid",
    ],
)
async def test_expired_answers_are_ignored(message: str) -> None:
    # Typed as object: the middleware annotation says Response, but results pass through.
    result: object = await IgnoreExpiredCallbackAnswers()(
        _failing(message), Bot(FAKE_TOKEN), ANSWER
    )
    assert result is True


async def test_other_answer_errors_are_raised() -> None:
    with pytest.raises(TelegramBadRequest):
        await IgnoreExpiredCallbackAnswers()(
            _failing("Bad Request: message text is empty"), Bot(FAKE_TOKEN), ANSWER
        )


async def test_the_same_error_on_other_methods_is_raised() -> None:
    with pytest.raises(TelegramBadRequest):
        await IgnoreExpiredCallbackAnswers()(
            _failing("Bad Request: query is too old"), Bot(FAKE_TOKEN), SEND
        )


async def test_successful_requests_pass_through() -> None:
    async def make_request(bot: Bot, method: TelegramMethod[Any]) -> Any:
        return "result"

    result: object = await IgnoreExpiredCallbackAnswers()(make_request, Bot(FAKE_TOKEN), SEND)
    assert result == "result"


async def test_the_real_bot_installs_the_middleware() -> None:
    settings = BotSettings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://user:pass@localhost/db"),
        redis_url=SecretStr("redis://localhost:6379/0"),
        telegram_bot_token=SecretStr(FAKE_TOKEN),
    )
    bot = create_bot(settings)
    try:
        assert any(isinstance(m, IgnoreExpiredCallbackAnswers) for m in bot.session.middleware)
    finally:
        await bot.session.close()
