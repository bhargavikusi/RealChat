# Architecture

This document describes RealChat's architecture and technical boundaries. Sections marked **Planned** are not implemented yet. Product behavior is defined in [requirements.md](requirements.md), and decisions are recorded in [decisions.md](decisions.md).

## Current state

Implemented so far:

* **Configuration:** `backend/config/settings.py` loads and validates settings at startup. Errors name the problem setting but never its value.
* **Logging:** `backend/core/logging.py` writes JSON logs to stdout and redacts credentials, bot tokens, and sensitive fields.
* **Database:** `backend/database/engine.py` provides the async SQLAlchemy engine, the session factory, and a declarative `Base` with fixed constraint-naming rules. `migrations/` holds the Alembic migrations. The database URL comes from settings, never from `alembic.ini`. The schema is described in [database.md](database.md).
* **Users module:** `backend/modules/users/` holds the user, profile, terms-acceptance, and profile-change models (`models.py`), pure validation rules (`validation.py`), service functions (`service.py`) used by the bot's onboarding and profile management, terms text (`terms.py`), and country matching (`countries.py`). Database operations take a session and leave committing to the caller. Age and gender changes after onboarding go through `change_age` and `change_gender`, which lock the profile row, enforce the cooldown (D-019), and append to the change history (D-024).
* **Redis:** `backend/core/redis.py` provides the client.
* **Bot:** `apps/bot/main.py` runs the aiogram dispatcher with long polling. Two Redis-backed outer middlewares (`apps/bot/middlewares.py`) run before every handler. The first claims each `update_id` before dispatch, with a short lease tagged by the running process. A duplicate that arrives while this process holds the lease is dropped. After the handler succeeds, the update is marked done and later redeliveries are dropped. If the handler raises, the claim is released. Note that aiogram confirms each update to Telegram once it has been received, whether or not the handler succeeded, so Telegram does not redeliver an update whose handler failed. That update is not retried, and the user receives the generic error and can try again. Telegram does redeliver updates that were never confirmed, which happens when the process stops or crashes mid-update. A lease left by a different process is therefore taken over immediately: Telegram allows only one long-polling consumer, so that process must have stopped, and its in-flight update is processed rather than lost. This rule must be revisited if webhooks with several workers are adopted (O-006). The second middleware drops updates from a user over a per-window limit. Both middlewares let updates through if Redis is unavailable, so side-effecting operations must carry their own database-level idempotency. `apps/bot/errors.py` logs handler failures with only the update id and type, the exception type, and the code location. It never logs the exception message, which could quote user input. The user receives a generic reply. `apps/bot/onboarding.py` handles `/start`, terms acceptance, and onboarding. The user's position is derived from the database where possible: terms acceptance (versioned, D-015) and the required fields (D-013). A user who leaves, or whose bot process restarts, therefore resumes at the right required step. Only the position within the optional steps (city, photo) and which text answer is expected are kept in aiogram FSM state in Redis. If that state is lost, a text message is not taken as an answer and the current step is asked again; a photo sent during the optional steps is still accepted. Because onboarding depends on that FSM state, it cannot proceed while Redis is unavailable, and the user receives the generic error. Each update gets its own database session (`apps/bot/db.py`). Handlers commit before sending replies, so no transaction stays open during a Telegram API call. Commands are never accepted as answers, and repeated or stale button presses are acknowledged without changing data. Onboarding runs only in private chats, so profile prompts and answers never appear in a group. Every handler that saves profile data first checks that the current terms version is accepted, and terms buttons carry the version they were shown with, so a button from older terms cannot accept newer ones. The dispatcher uses per-user event isolation, so a quick second message is not routed by state the first one is still changing. The locks are kept in Redis and expire after 60 seconds (D-018), so the process keeps no per-user lock memory, and isolation still holds if webhooks with several workers are adopted (O-006). `apps/bot/profile.py` handles `/profile`: it shows the user's profile card as others see it and lets each field be edited with the same validation as onboarding. It opens only after onboarding is complete and the current terms are accepted. Otherwise the user is taken to the terms or their onboarding step. Each edit gets a random id, kept in FSM state with the field being edited and carried by that prompt's buttons. A button from a cancelled, finished, or older edit therefore no longer matches and is answered as expired without changing anything. A profile without a photo is shown with a default avatar for its gender (`apps/bot/avatars.py`, D-020). Each image is uploaded once and then reused by its Telegram file identifier, cached in Redis per bot. The bot only ever stores Telegram file identifiers for user photos and never downloads image files. `apps/bot/account.py` handles Advanced settings and account deletion (D-025), and provides the access gate: an inner update middleware, registered after the database session, that stops suspended and banned users before any handler runs, except help and deletion (D-026). `apps/bot/menu.py` handles `/help` (D-023) and defines the command menu, which the bot registers with Telegram for private chats at startup (D-022). Routers are tried in order (errors, menu, account, profile, onboarding), so onboarding's final handler only sees messages no other handler took. Messages are sent as plain text with no parse mode, so user content can never be interpreted as markup. The bot token is only required by the bot process (`BotSettings`).
* **API:** `apps/api/main.py` serves `/health` (the process is running) and `/ready` (PostgreSQL and Redis are reachable, each checked with a 2-second timeout). Failure details go to the server log and never into the HTTP response. Interactive docs and the OpenAPI schema are disabled.
* **Local services:** `docker-compose.yml` runs PostgreSQL 18 and Redis 8, both ARM64-compatible, bound to `127.0.0.1` only.

No keyboard menu, matching, chat, or worker exist yet.

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
