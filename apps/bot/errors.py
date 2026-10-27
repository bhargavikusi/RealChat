"""Last-resort handling for exceptions raised while processing an update."""

import logging

from aiogram import Router
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)

GENERIC_ERROR_TEXT = "Something went wrong on our side. Please try again in a moment."


async def handle_error(event: ErrorEvent) -> bool:
    update = event.update
    # Only the update id and type are logged, never message text or user details.
    logger.error(
        "unhandled error while processing update",
        extra={"update_id": update.update_id, "update_type": update.event_type},
        exc_info=event.exception,
    )

    try:
        if update.message is not None:
            await update.message.answer(GENERIC_ERROR_TEXT)
        elif update.callback_query is not None:
            await update.callback_query.answer(GENERIC_ERROR_TEXT, show_alert=True)
    except Exception:
        logger.warning("could not deliver error notice", exc_info=True)
    # Returning True marks the error as handled so it is not re-raised to polling.
    return True


def create_router() -> Router:
    router = Router(name="errors")
    router.errors.register(handle_error)
    return router
