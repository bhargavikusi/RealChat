# RealChat

RealChat is a Telegram-first anonymous chat and social discovery platform. Users interact primarily through a Telegram bot, with an optional Telegram Mini App for richer features.

## Status

Early development. No runnable application yet.

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

## Checks

Run all four before opening a pull request:

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # type checks
uv run pytest                # tests
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
