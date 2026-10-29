import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def _applied_revisions(database_url: str) -> set[str]:
    async def query() -> set[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                exists = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
                if exists is None:
                    return set()
                rows = await connection.execute(text("SELECT version_num FROM alembic_version"))
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(query())


def test_upgrade_and_downgrade_round_trip(database_url: str) -> None:
    config = _alembic_config(database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None

    command.downgrade(config, "base")
    assert _applied_revisions(database_url) == set()

    command.upgrade(config, "head")
    assert _applied_revisions(database_url) == {head}

    command.downgrade(config, "base")
    assert _applied_revisions(database_url) == set()

    # Leave the test database migrated for any later tests.
    command.upgrade(config, "head")
    assert _applied_revisions(database_url) == {head}


def test_single_migration_head(database_url: str) -> None:
    # Two heads means two migrations branched from the same parent and must be merged.
    heads = ScriptDirectory.from_config(_alembic_config(database_url)).get_heads()
    assert len(heads) == 1


def test_idle_tracking_counts_open_chats_from_their_last_message(database_url: str) -> None:
    config = _alembic_config(database_url)
    command.downgrade(config, "fc4bb28d2eba")  # before idle tracking

    async def seed() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                # As in the test fixtures: the audit log refuses TRUNCATE otherwise.
                await connection.execute(
                    text("ALTER TABLE admin_audit_log DISABLE TRIGGER admin_audit_log_no_truncate")
                )
                await connection.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
                await connection.execute(
                    text("ALTER TABLE admin_audit_log ENABLE TRIGGER admin_audit_log_no_truncate")
                )
                await connection.execute(
                    text("INSERT INTO users (telegram_user_id) VALUES (1), (2)")
                )
                await connection.execute(
                    text(
                        "INSERT INTO conversations (id, started_at) OVERRIDING SYSTEM VALUE "
                        "VALUES (1, '2026-01-01 10:00+00'), (2, '2026-01-01 11:00+00')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO chat_messages (conversation_id, sender_user_id, "
                        "sender_message_id, kind, created_at) "
                        "VALUES (1, 1, 1, 'text', '2026-01-01 10:30+00')"
                    )
                )
        finally:
            await engine.dispose()

    async def activity() -> dict[int, str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT id, last_activity_at::text FROM conversations ORDER BY id")
                )
                return {row[0]: row[1] for row in rows}
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed())
        command.upgrade(config, "head")
        assert asyncio.run(activity()) == {
            1: "2026-01-01 10:30:00+00",  # its last message
            2: "2026-01-01 11:00:00+00",  # no messages: its start
        }
    finally:
        command.upgrade(config, "head")
