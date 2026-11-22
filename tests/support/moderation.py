"""The moderation Mini App's API with a recording bot, and people, chats, and reports
to review, for the moderation API tests (D-071)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api import moderation
from apps.api.main import create_app
from backend.config.settings import BotSettings
from backend.modules.chat.models import ChatMessage, Conversation, ConversationParticipant
from backend.modules.moderation.models import Report
from backend.modules.users import service as users
from backend.modules.users.models import Profile
from tests.support.telegram import RecordingSession, make_bot
from tests.support.webapp import authorization

Sessions = async_sessionmaker[AsyncSession]
ALICE, BOB, CAROL, MODERATOR, ADMIN = 6001, 6002, 6003, 7101, 7102
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class Panel:
    """The API with a recording bot, and helpers to call it as a signed-in person."""

    def __init__(self, client: TestClient, telegram: RecordingSession) -> None:
        self.client = client
        self.telegram = telegram

    def get(self, path: str, as_user: int = MODERATOR) -> Any:
        return self.client.get(path, headers=authorization(as_user, datetime.now(UTC)))

    def post(self, path: str, body: dict[str, Any], as_user: int = MODERATOR) -> Any:
        return self.client.post(path, json=body, headers=authorization(as_user, datetime.now(UTC)))


@pytest.fixture
def panel(
    bot_settings: BotSettings,
    session_factory: Sessions,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Panel]:
    monkeypatch.setattr(moderation, "DIST_DIR", tmp_path)
    with TestClient(create_app(bot_settings)) as client:
        bot, telegram = make_bot()
        client.app.state.bot = bot  # type: ignore[attr-defined]
        yield Panel(client, telegram)


async def make_person(
    sessions: Sessions,
    telegram_id: int,
    nickname: str,
    role: str = "user",
    photo: str | None = None,
) -> int:
    async with sessions() as session:
        user = await users.get_or_create_user(session, telegram_id)
        user.role = role
        await session.execute(
            update(Profile)
            .where(Profile.user_id == user.id)
            .values(
                nickname=nickname,
                age_at_entry=25,
                age_entered_on=date(2026, 1, 1),
                gender="female",
                country_code="IN",
                photo_file_id=photo,
            )
        )
        await session.commit()
        return user.id


@pytest.fixture
async def people(session_factory: Sessions) -> dict[str, int]:
    return {
        "alice": await make_person(session_factory, ALICE, "Night Owl"),
        "bob": await make_person(session_factory, BOB, "Moon Child", photo="AgAC-bob-photo"),
        "carol": await make_person(session_factory, CAROL, "Sun Ray"),
        "moderator": await make_person(session_factory, MODERATOR, "Mod Max", "moderator"),
        "admin": await make_person(session_factory, ADMIN, "Admin Ann", "admin"),
    }


async def make_conversation(sessions: Sessions, a: int, b: int) -> int:
    async with sessions() as session:
        conversation = Conversation(started_at=NOW, ended_at=NOW, end_reason="reported")
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                ConversationParticipant(conversation_id=conversation.id, user_id=a, left_at=NOW),
                ConversationParticipant(conversation_id=conversation.id, user_id=b, left_at=NOW),
            ]
        )
        await session.commit()
        return conversation.id


async def make_message(
    sessions: Sessions, conversation_id: int, sender: int, kind: str, **fields: Any
) -> int:
    async with sessions() as session:
        message = ChatMessage(
            conversation_id=conversation_id,
            sender_user_id=sender,
            sender_message_id=fields.pop("sender_message_id"),
            kind=kind,
            created_at=fields.pop("created_at", NOW),
            **fields,
        )
        session.add(message)
        await session.commit()
        return message.id


async def make_report(
    sessions: Sessions, reporter: int, reported: int, conversation_id: int | None, **fields: Any
) -> int:
    async with sessions() as session:
        report = Report(
            reporter_user_id=reporter,
            reported_user_id=reported,
            conversation_id=conversation_id,
            reason=fields.pop("reason", "harassment"),
            urgent=fields.pop("urgent", False),
            created_at=fields.pop("created_at", NOW),
            **fields,
        )
        session.add(report)
        await session.commit()
        return report.id
