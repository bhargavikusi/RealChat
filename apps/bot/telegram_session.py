"""Behaviour shared by every Telegram API request the bot makes.

Button handlers acknowledge a tap (answerCallbackQuery) before replying. Telegram
rejects that acknowledgement when the tap is too old, for example after the bot was
offline or the update arrived late. The acknowledgement only stops the button's
loading spinner, so losing it must not stop the handler's real work: the request
middleware here turns that one rejection into a no-op, and every other error is
raised as before.
"""

import logging
from typing import cast

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, Response, TelegramMethod
from aiogram.methods.base import TelegramType

logger = logging.getLogger(__name__)

# Telegram's description for an acknowledgement that came too late or for an unknown
# query: "query is too old and response timeout expired or query ID is invalid".
_EXPIRED_MARKERS = ("query is too old", "query id is invalid")


def _is_expired_answer(method: TelegramMethod[TelegramType], error: TelegramBadRequest) -> bool:
    description = error.message.casefold()
    return isinstance(method, AnswerCallbackQuery) and any(
        marker in description for marker in _EXPIRED_MARKERS
    )


class IgnoreExpiredCallbackAnswers(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as error:
            if not _is_expired_answer(method, error):
                raise
            # No identifiers or text: the rejection says nothing about the user.
            logger.info("callback acknowledgement expired; continuing without it")
            # answerCallbackQuery returns True on success. The request chain passes
            # results through unchanged, despite the Response annotation.
            return cast(Response[TelegramType], True)


def install_request_middlewares(session: BaseSession) -> None:
    session.middleware(IgnoreExpiredCallbackAnswers())
