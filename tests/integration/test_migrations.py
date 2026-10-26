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
