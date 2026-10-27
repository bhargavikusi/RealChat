"""Temporary /start reply until conversational onboarding exists."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

PLACEHOLDER_TEXT = "RealChat is being built. Onboarding is coming soon."


async def start(message: Message) -> None:
    await message.answer(PLACEHOLDER_TEXT)


def create_router() -> Router:
    router = Router(name="start")
    router.message.register(start, CommandStart())
    return router
