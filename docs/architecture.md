# Architecture

This document describes RealChat's architecture and technical boundaries. Sections marked **Planned** are not implemented yet. Product behavior is defined in [requirements.md](requirements.md), and decisions are recorded in [decisions.md](decisions.md).

## Current state

Implemented so far:

* **Configuration:** `backend/config/settings.py` loads and validates settings at startup. Errors name the problem setting but never its value.
* **Logging:** `backend/core/logging.py` writes JSON logs to stdout and redacts credentials, bot tokens, and sensitive fields.
* **Database:** `backend/database/engine.py` provides the async SQLAlchemy engine, the session factory, and a declarative `Base` with fixed constraint-naming rules. `migrations/` holds the Alembic setup and an empty baseline migration. The database URL comes from settings, never from `alembic.ini`.
* **Redis:** `backend/core/redis.py` provides the client.
* **API:** `apps/api/main.py` serves `/health` (the process is running) and `/ready` (PostgreSQL and Redis are reachable, each checked with a 2-second timeout). Failure details go to the server log and never into the HTTP response. Interactive docs and the OpenAPI schema are disabled.
* **Local services:** `docker-compose.yml` runs PostgreSQL 18 and Redis 8, both ARM64-compatible, bound to `127.0.0.1` only.

No domain tables, Telegram bot, or worker exist yet.

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
