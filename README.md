# RealChat

RealChat is a Telegram-first anonymous chat and social discovery platform. Users interact primarily through a Telegram bot, with an optional Telegram Mini App for richer features.

## Status

Early development. No runnable application yet.

## Prerequisites

* [Python 3.13](https://www.python.org/downloads/)
* [uv](https://docs.astral.sh/uv/), the Python package manager
* [Docker Desktop](https://www.docker.com/products/docker-desktop/), or another Docker engine with Compose

## Local setup

Setup steps will be added here as the application skeleton is built. For now, copy the environment template:

```bash
cp .env.example .env
```

Edit `.env` with your local values. It is ignored by Git and must never be committed.

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
