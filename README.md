# RealChat

RealChat is a Telegram-first anonymous chat and social discovery platform. Users interact primarily through a Telegram bot, with an optional Telegram Mini App for richer features.

## Status

Early development. The infrastructure runs locally: an API with health checks, PostgreSQL, Redis, database migrations, and a Telegram bot runtime that answers `/start` with a placeholder. No product features exist yet: there's no onboarding, profiles, matching, or chat.

## Prerequisites

* [Python 3.13](https://www.python.org/downloads/)
* [uv](https://docs.astral.sh/uv/), the Python package manager
* [Docker Desktop](https://www.docker.com/products/docker-desktop/), or another Docker engine with Compose

## Local setup

Install the Python dependencies. `uv` creates `.venv/` using Python 3.13:

```bash
uv sync
```

Copy the environment template, then edit `.env` with your local values. `.env` is ignored by Git and must never be committed.

```bash
cp .env.example .env
```

If a required setting is missing or invalid, the application stops at startup with an error. The error names the setting but never prints its value.

Start PostgreSQL and Redis, then apply the database migrations:

```bash
docker compose up -d --wait
uv run alembic upgrade head
```

PostgreSQL listens on `localhost:5433`, not the default 5432, so it doesn't clash with a PostgreSQL installed directly on the machine. Redis listens on `localhost:6379`. Both ports accept connections from this machine only.

Start the API:

```bash
uv run uvicorn --factory apps.api.main:create_app --reload
```

To check that it's running:

* `GET /health` returns 200 while the process is running.
* `GET /ready` returns 200 when PostgreSQL and Redis are both reachable. Otherwise it returns 503 and names the failing service.

To stop the services, run `docker compose down`. Data is kept in Docker volumes. Add `-v` to delete it as well.

### Telegram bot

Create a development bot with [@BotFather](https://t.me/BotFather), keeping it separate from any production bot. Put its token in `.env` as `TELEGRAM_BOT_TOKEN`. Then, with PostgreSQL and Redis running, start the bot:

```bash
uv run python -m apps.bot.main
```

During development the bot uses long polling, so it needs no public URL. It stops on Ctrl+C.

While Redis is available, two protections apply:

* An update that Telegram delivers again after being handled successfully is dropped for `BOT_UPDATE_DEDUP_TTL_SECONDS`. An update whose handling failed or was interrupted is processed again.
* Each user is subject to a flood limit of `BOT_FLOOD_LIMIT` updates per `BOT_FLOOD_WINDOW_SECONDS`.

If Redis is unavailable, both protections let updates through so the bot stays online. Handlers with side effects, such as charges or matches, must therefore enforce their own idempotency in the database.

## Checks

GitHub Actions runs these checks on every pull request to `develop`, `staging`, or `main`, and again after each merge (`.github/workflows/ci.yml`). The workflow also cycles the migrations on a clean database, runs `alembic check` to catch model changes that lack a migration, and scans dependencies for known vulnerabilities. Its PostgreSQL and Redis are throwaway containers with CI-only credentials; no repository secrets are used.

Run all four locally before opening a pull request:

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # type checks
uv run pytest                # tests
```

Integration tests use the `realchat_test` database and Redis database 15, configured by `TEST_DATABASE_URL` and `TEST_REDIS_URL` in `.env`. They need `docker compose up` to be running. Without those settings the tests are skipped. Set `REQUIRE_INTEGRATION=1` to make missing settings a failure instead, as CI does.

The test database is created only when the PostgreSQL volume is first created. If your volume is older than the init script, create the database once with:

```bash
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" realchat_test'
```

## Documentation

* [Product requirements](docs/requirements.md)
* [Architecture](docs/architecture.md)
* [Decisions and open questions](docs/decisions.md)
* [Contributor instructions](CLAUDE.md)

## Branches

* `main` — production-ready code
* `staging` — staging validation
* `develop` — integrated development work

Work happens on short-lived issue branches created from `develop`.
