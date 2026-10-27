import io
import logging
from collections.abc import Iterator

import pytest
from aiogram import Dispatcher, F, Router
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Message

from apps.bot import errors
from backend.core.logging import JsonFormatter
from tests.support.telegram import callback_update, make_bot, message_update

SECRET_TEXT = "my private message 7q2w"


async def fail_on_message(message: Message) -> None:
    # Exception messages can quote user input; the log must not contain it.
    raise ValueError(f"invalid nickname: {message.text}")


async def fail_on_callback(query: CallbackQuery) -> None:
    raise RuntimeError("handler failed")


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(errors.__name__)
    logger.addHandler(handler)
    yield stream
    logger.removeHandler(handler)


def _dispatcher() -> Dispatcher:
    failing = Router(name="failing")
    failing.message.register(fail_on_message)
    failing.callback_query.register(fail_on_callback, F.data == "boom")
    dispatcher = Dispatcher()
    dispatcher.include_routers(errors.create_router(), failing)
    return dispatcher


async def test_message_error_is_reported_generically(captured_logs: io.StringIO) -> None:
    bot, session = make_bot()

    await _dispatcher().feed_update(bot, message_update(1, user_id=42, text=SECRET_TEXT))

    assert session.sent_texts() == [errors.GENERIC_ERROR_TEXT]
    logs = captured_logs.getvalue()
    assert "unhandled error while processing update" in logs
    assert SECRET_TEXT not in logs
    assert "invalid nickname" not in logs
    assert '"error_type": "builtins.ValueError"' in logs
    assert "in fail_on_message" in logs


async def test_callback_error_is_reported_as_alert() -> None:
    bot, session = make_bot()

    await _dispatcher().feed_update(bot, callback_update(2, user_id=42, data="boom"))

    answers = [r for r in session.requests if isinstance(r, AnswerCallbackQuery)]
    assert len(answers) == 1
    assert answers[0].text == errors.GENERIC_ERROR_TEXT
    assert answers[0].show_alert is True
