# Architecture

This document describes RealChat's architecture and technical boundaries. Sections marked **Planned** are not implemented yet. Product behavior is defined in [requirements.md](requirements.md), and decisions are recorded in [decisions.md](decisions.md).

## Current state

Implemented so far:

* **Configuration:** `backend/config/settings.py` loads and validates settings at startup. Errors name the problem setting but never its value.
* **Logging:** `backend/core/logging.py` writes JSON logs to stdout and redacts credentials, bot tokens, and sensitive fields.
* **Database:** `backend/database/engine.py` provides the async SQLAlchemy engine, the session factory, and a declarative `Base` with fixed constraint-naming rules. `migrations/` holds the Alembic migrations. The database URL comes from settings, never from `alembic.ini`. The schema is described in [database.md](database.md).
* **Users module:** `backend/modules/users/` holds the user, profile, and terms-acceptance models (`models.py`), pure validation rules (`validation.py`), and service functions (`service.py`) for the bot and API to call; no bot or API code uses them yet. Database operations take a session and leave committing to the caller.
* **Redis:** `backend/core/redis.py` provides the client.
* **Bot:** `apps/bot/main.py` runs the aiogram dispatcher with long polling. Two Redis-backed outer middlewares (`apps/bot/middlewares.py`) run before every handler. The first claims each `update_id` before dispatch, with a short lease tagged by the running process. A duplicate that arrives while this process holds the lease is dropped. After the handler succeeds, the update is marked done and later redeliveries are dropped. If the handler raises, the claim is released. Note that aiogram confirms each update to Telegram once it has been received, whether or not the handler succeeded, so Telegram does not redeliver an update whose handler failed. That update is not retried, and the user receives the generic error and can try again. Telegram does redeliver updates that were never confirmed, which happens when the process stops or crashes mid-update. A lease left by a different process is therefore taken over immediately: Telegram allows only one long-polling consumer, so that process must have stopped, and its in-flight update is processed rather than lost. This rule must be revisited if webhooks with several workers are adopted (O-006). The second middleware drops updates from a user over a per-window limit. Both middlewares let updates through if Redis is unavailable, so side-effecting operations must carry their own database-level idempotency. `apps/bot/errors.py` logs handler failures with only the update id and type, the exception type, and the code location. It never logs the exception message, which could quote user input. The user receives a generic reply. `/start` currently replies with a placeholder until onboarding exists. Messages are sent as plain text with no parse mode, so user content can never be interpreted as markup. The bot token is only required by the bot process (`BotSettings`).
* **API:** `apps/api/main.py` serves `/health` (the process is running) and `/ready` (PostgreSQL and Redis are reachable, each checked with a 2-second timeout). Failure details go to the server log and never into the HTTP response. Interactive docs and the OpenAPI schema are disabled.
* **Local services:** `docker-compose.yml` runs PostgreSQL 18 and Redis 8, both ARM64-compatible, bound to `127.0.0.1` only.

No user-facing bot flows or worker exist yet.

## Overview (planned)

RealChat is a **modular monolith**: one codebase with shared business modules, run as several processes. There are no microservices and no Kubernetes.

```
 Telegram users
      │
      ▼
 Telegram Bot API ◄────────────► Bot process (aiogram)
                                   │
 Mini App (React, in Telegram) ──► API process (FastAPI)
                                   │
                                   ▼
                   Shared backend modules (business rules)
                                   │
                ┌──────────────────┼──────────────────┐
                ▼                  ▼                  ▼
          PostgreSQL            Redis           Worker process
        (source of truth)  (queues, caches,    (expiry, refunds,
                            rate limits)        notifications,
                                                cleanup)
```

### Processes

| Process | Responsibility |
|---|---|
| Bot | Receives Telegram updates. Handles commands, onboarding conversations, callbacks, and message relay between matched users. |
| API | Authenticated endpoints for the Mini App and admin tools, health checks, and Telegram webhooks if those are chosen (decision O-006). |
| Worker | Scheduled and background jobs: chat-request expiry, refunds, notifications, retention cleanup, and payment reconciliation. |
| Mini App | Optional React and TypeScript interface opened inside Telegram. It is never required for onboarding, matching, or chatting. |

All processes call the same backend modules. Bot handlers, API routes, and frontend components contain no business rules of their own.

### Data stores

* **PostgreSQL** is the source of truth for users, profiles, conversations, the coin ledger, payments, subscriptions, reports, and audit records.
* **Redis** holds temporary state only: matching queues, rate-limit counters, caches, and short-lived locks. If Redis loses data, no payment, balance, or account record can be lost or duplicated.

### Planned repository layout

The layout follows requirements §24. Folders are added only when they are needed.

```
apps/        entry points: bot, api, worker, miniapp
backend/     shared code: config, database, integrations, and domain modules
migrations/  Alembic migrations
tests/       unit, integration, security, concurrency, end-to-end
infra/       Docker, reverse proxy, server scripts
docs/        requirements, architecture, decisions, and later security and deployment docs
```

## Key invariants (planned)

* Every coin balance change is an immutable ledger entry written inside a database transaction with a unique idempotency key.
* A match, charge, refund, or payment fulfilment happens at most once, even when a Telegram update is retried or delivered twice.
* No message is relayed unless the sender and recipient are both participants in the same active conversation.
* Exact coordinates are never shown to other users.

## Target infrastructure (planned)

* A single Oracle Cloud Always Free ARM64 (Ampere A1) server, if one is available to the account, running every process plus PostgreSQL and Redis under Docker Compose.
* Caddy or Nginx in front, providing HTTPS.
* PostgreSQL and Redis are never exposed to the internet.

A single server provides no high availability. See `docs/deployment.md`, once it is written.
