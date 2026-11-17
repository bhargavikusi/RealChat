"""The test stand-in for Telegram refuses messages Telegram would refuse."""

import pytest
from aiogram.exceptions import TelegramBadRequest

from tests.support.telegram import make_bot


async def test_a_message_over_telegrams_limit_is_refused_and_not_counted_as_sent() -> None:
    bot, session = make_bot()
    await bot.send_message(1, "x" * 4096)
    with pytest.raises(TelegramBadRequest, match="too long"):
        await bot.send_message(1, "x" * 4097)
    assert session.sent_texts() == ["x" * 4096]
    [refused] = session.failed_sends
    assert refused.text == "x" * 4097


async def test_the_limit_counts_emoji_as_telegram_does() -> None:
    # Telegram counts UTF-16 code units: an emoji outside the basic plane is two.
    bot, session = make_bot()
    await bot.send_message(1, "😀" * 2048)
    with pytest.raises(TelegramBadRequest, match="too long"):
        await bot.send_message(1, "😀" * 2049)
    assert session.sent_texts() == ["😀" * 2048]
