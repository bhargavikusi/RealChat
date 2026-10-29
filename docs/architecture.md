# Architecture

This document describes RealChat's architecture and technical boundaries. Sections marked **Planned** are not implemented yet. Product behavior is defined in [requirements.md](requirements.md), and decisions are recorded in [decisions.md](decisions.md).

## Current state

Implemented so far:

* **Configuration:** `backend/config/settings.py` loads and validates settings at startup. Errors name the problem setting but never its value.
* **Logging:** `backend/core/logging.py` writes JSON logs to stdout and redacts credentials, bot tokens, and sensitive fields.
* **Database:** `backend/database/engine.py` provides the async SQLAlchemy engine, the session factory, and a declarative `Base` with fixed constraint-naming rules. `migrations/` holds the Alembic migrations. The database URL comes from settings, never from `alembic.ini`. The schema is described in [database.md](database.md).
* **Users module:** `backend/modules/users/` holds the user, profile, terms-acceptance, and profile-change models (`models.py`), pure validation rules (`validation.py`), service functions (`service.py`) used by the bot's onboarding and profile management, terms text (`terms.py`), and country matching (`countries.py`). Database operations take a session and leave committing to the caller. Age and gender changes after onboarding go through `change_age` and `change_gender`, which lock the profile row, enforce the cooldown (D-019), and append to the change history (D-024).
* **Admin module:** `backend/modules/admin/` holds runtime settings and the admin audit log (`models.py`), the list of settings with their defaults and allowed ranges plus a reader with a 10-second in-process cache (`settings.py`, D-035), and role and setting changes (`service.py`). Every change is audit-logged. Roles (D-034) are changed only by the server-side CLI, `apps/admin_cli.py`, except that account deletion resets a role to `user`; a test checks that no bot module changes roles. The audit log is append-only in the database itself: triggers reject any update, deletion, or truncation. Because the application currently connects as the table owner, which could disable them, full protection needs a restricted application database role (O-011).
* **Redis:** `backend/core/redis.py` provides the client.
* **Bot:** `apps/bot/main.py` runs the aiogram dispatcher with long polling. Two Redis-backed outer middlewares (`apps/bot/middlewares.py`) run before every handler. The first claims each `update_id` before dispatch, with a short lease tagged by the running process. A duplicate that arrives while this process holds the lease is dropped. After the handler succeeds, the update is marked done and later redeliveries are dropped. If the handler raises, the claim is released. Note that aiogram confirms each update to Telegram once it has been received, whether or not the handler succeeded, so Telegram does not redeliver an update whose handler failed. That update is not retried, and the user receives the generic error and can try again. Telegram does redeliver updates that were never confirmed, which happens when the process stops or crashes mid-update. A lease left by a different process is therefore taken over immediately: Telegram allows only one long-polling consumer, so that process must have stopped, and its in-flight update is processed rather than lost. This rule must be revisited if webhooks with several workers are adopted (O-006). The second middleware drops updates from a user over a per-window limit. Both middlewares let updates through if Redis is unavailable, so side-effecting operations must carry their own database-level idempotency. `apps/bot/errors.py` logs handler failures with only the update id and type, the exception type, and the code location. It never logs the exception message, which could quote user input. The user receives a generic reply. `apps/bot/onboarding.py` handles `/start`, terms acceptance, and onboarding. The user's position is derived from the database where possible: terms acceptance (versioned, D-015) and the required fields (D-013). A user who leaves, or whose bot process restarts, therefore resumes at the right required step. Only the position within the optional steps (city, photo) and which text answer is expected are kept in aiogram FSM state in Redis. If that state is lost, a text message is not taken as an answer and the current step is asked again; a photo sent during the optional steps is still accepted. Because onboarding depends on that FSM state, it cannot proceed while Redis is unavailable, and the user receives the generic error. Each update gets its own database session (`apps/bot/db.py`). Handlers commit before sending replies, so no transaction stays open during a Telegram API call. Commands are never accepted as answers, and repeated or stale button presses are acknowledged without changing data. Onboarding runs only in private chats, so profile prompts and answers never appear in a group. Every handler that saves profile data first checks that the current terms version is accepted, and terms buttons carry the version they were shown with, so a button from older terms cannot accept newer ones. The dispatcher uses per-user event isolation, so a quick second message is not routed by state the first one is still changing. The locks are kept in Redis and expire after 60 seconds (D-018), so the process keeps no per-user lock memory, and isolation still holds if webhooks with several workers are adopted (O-006). `apps/bot/profile.py` handles `/profile`: it shows the user's profile card as others see it and lets each field be edited with the same validation as onboarding. It opens only after onboarding is complete and the current terms are accepted. Otherwise the user is taken to the terms or their onboarding step. Each edit gets a random id, kept in FSM state with the field being edited and carried by that prompt's buttons. A button from a cancelled, finished, or older edit therefore no longer matches and is answered as expired without changing anything. A profile without a photo is shown with a default avatar for its gender (`apps/bot/avatars.py`, D-020). Each image is uploaded once and then reused by its Telegram file identifier, cached in Redis per bot. The bot only ever stores Telegram file identifiers for user photos and never downloads image files. `apps/bot/account.py` handles Advanced settings and account deletion (D-025), and provides the access gate: an inner update middleware, registered after the database session, that stops suspended and banned users before any handler runs, except help and deletion (D-026). `apps/bot/admin.py` provides 🛠 Admin settings, offered inside Advanced settings to admins only. Every handler that shows or opens a setting re-checks the admin role and the current terms on the server, so an old or hand-made admin button from anyone else is answered as expired and changes nothing. Saving re-checks the role again inside the same transaction as the write, with the account row locked, so a role revoked meanwhile cannot slip through. Cancelling an edit and the "please send a number" reply change no settings and only affect the user's own pending edit. A saved change clears the bot process's settings cache at once; other processes see it within the cache time. `apps/bot/menu.py` handles `/help` (D-023) and defines the command menu, which the bot registers with Telegram for private chats at startup (D-022). Routers are tried in order (errors, menu, account, chat, relay, admin, profile, onboarding), so onboarding's final handler only sees messages no other handler took. Messages are sent as plain text with no parse mode, so user content can never be interpreted as markup. Every request to Telegram passes through `apps/bot/telegram_session.py`: if Telegram rejects a button acknowledgement because the tap is too old or unknown, the rejection is ignored and the handler carries on with its action and reply. Any other Telegram error is raised as before. The test bot uses the same request middleware. The bot token is only required by the bot process (`BotSettings`).
* **API:** `apps/api/main.py` serves `/health` (the process is running) and `/ready` (PostgreSQL and Redis are reachable, each checked with a 2-second timeout). Failure details go to the server log and never into the HTTP response. Interactive docs and the OpenAPI schema are disabled.
* **Local services:** `docker-compose.yml` runs PostgreSQL 18 and Redis 8, both ARM64-compatible, bound to `127.0.0.1` only.

* **Matching and chat:** `backend/modules/matching/queue.py` keeps searches in Redis: a sorted set of searching users plus one key per user holding the state of their current search (searching, held by the matcher, or cancelled while held) (D-027). Every change that touches both is atomic, run as a single Lua script. A search cancelled while the matcher holds it is never put back, and a queue entry whose search key has expired is discarded rather than matched. A claim marker expires after 60 seconds, so one left behind by a failed release cannot block new searches for long; a failed release after a committed match is logged and the match is still announced. `backend/modules/matching/matcher.py` pairs searchers (D-041); `backend/modules/chat/` holds the conversation tables and the service that starts, finds, and ends conversations, checks eligibility, and finds recent partners. `apps/bot/chat.py` handles 🎭 New Anonymous Chat (start a search, with a Cancel button bound to that search), 👤 Partner profile, and ⏹ End chat, and provides `ChatNotifier`, which the worker uses to announce matches and ended searches. `apps/bot/keyboards.py` defines the main and chat keyboards; the bot shows the chat keyboard while the user is in a chat. `apps/bot/relay.py` relays everything else a user in a chat sends (D-028, D-036, D-038, D-042, D-043): the message is checked against the sender's open conversation in PostgreSQL and against `backend/modules/chat/content.py` (no contact cards, locations, links, phone numbers, or @usernames), the transaction is committed, the message is copied to the partner with `copyMessage` and `protect_content` (no sender shown; no forwarding or saving), and only then stored in `chat_messages`. Storing share-locks the conversation; if the chat ended while the copy was on its way, the copy is deleted again. Replies are pointed at the matching message on the partner's side, and edits to text and captions are mirrored. A redelivered message is relayed once. If the partner blocked the bot or is gone, the chat ends as `unreachable` and the sender is told. The relay passes over users who are not in a chat, commands, and messages while another step (such as a profile edit) is waiting for an answer, so those reach their usual handlers. Message content never appears in ordinary logs. An album is relayed as separate messages, because each item reaches the bot as its own message.
* **Worker:** `apps/worker/main.py` runs the matcher about once a second. Each tick takes a Redis lock (30 seconds, not renewed), so normally only one matcher runs even with several workers. Correctness does not depend on the lock: a pair is claimed from Redis atomically, and the database refuses a second open conversation, so an overlapping tick cannot create a double match. It ends searches past the time limit (an admin setting) and tells those users; re-checks waiting users in PostgreSQL (active account, onboarding complete, current terms, no open conversation) and drops anyone ineligible; pairs the rest oldest first, avoiding partners from the last few minutes while anyone else is waiting (D-033), and swapping partners between pairs where that avoids a repeat; each tick considers at most the 200 longest-waiting searches still within the time limit, so an alternative further back in a longer queue is not seen in that tick; then, for each pair, takes both out of the queue atomically, re-checks them with their user rows share-locked and creates the conversation in one transaction, and only after that commit tells both users. Account deletion locks the same row first, so a deletion and a match never interleave. If the database refuses the match, whoever is still eligible goes back to their old place in the queue; on any database error both go back before the error is raised. A failed tick is logged without exception messages and the next one runs as usual.

No blocking, reporting, chat-content cleanup, or Mini App exist yet.

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
