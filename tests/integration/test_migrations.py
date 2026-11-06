import asyncio

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.modules.users import public_ids


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
    command.upgrade(config, "head")  # a known starting point, even on a fresh database
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


def test_existing_profiles_get_distinct_realchat_ids(database_url: str) -> None:
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "2bd4ae63b8d8")  # before RealChat IDs (D-054)

    async def seed() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                # As in the test fixtures: the audit log and the ledger refuse TRUNCATE.
                for table in ("admin_audit_log", "coin_transactions"):
                    await connection.execute(
                        text(f"ALTER TABLE {table} DISABLE TRIGGER {table}_no_truncate")
                    )
                await connection.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
                for table in ("admin_audit_log", "coin_transactions"):
                    await connection.execute(
                        text(f"ALTER TABLE {table} ENABLE TRIGGER {table}_no_truncate")
                    )
                await connection.execute(
                    text(
                        "INSERT INTO users (telegram_user_id) "
                        "SELECT g FROM generate_series(1, 50) g"
                    )
                )
                await connection.execute(
                    text("INSERT INTO profiles (user_id) SELECT id FROM users")
                )
        finally:
            await engine.dispose()

    async def ids() -> list[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(text("SELECT public_id FROM profiles"))
                return [row[0] for row in rows]
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed())
        command.upgrade(config, "head")
        found = asyncio.run(ids())
        assert len(found) == 50 and len(set(found)) == 50
        assert all(public_ids.normalize(value) == value for value in found)
    finally:
        command.upgrade(config, "head")
