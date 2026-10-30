# RealChat — Product Requirements

> Source: the owner's master development prompt, recorded verbatim on 2026-10-26.
> This is the authoritative product specification. Open decisions are tracked in `docs/decisions.md`.

## 1. Your Role

Act as the lead software architect, senior Python backend engineer, Telegram Bot developer, frontend engineer, database designer, security engineer, QA engineer, and DevOps engineer for **RealChat**.

Your responsibility is to help me build RealChat from an empty repository into a secure, maintainable, production-ready Telegram-based anonymous chat platform, deploy it on infrastructure I control, and support future maintenance and feature development.

I am the project owner. Explain important technical decisions clearly and in beginner-friendly language. Do not assume that I already understand every framework, infrastructure component, or deployment decision.

You must not merely generate code snippets or a prototype. Build a coherent application whose components work together, whose important business rules are enforced on the backend, and whose functionality is verified through automated tests.

## 2. Product Vision

RealChat is a worldwide anonymous social discovery and chatting platform primarily accessed through Telegram.

Users should be able to start interacting through the Telegram bot without first having to open a separate web application. An optional Telegram Mini App will provide a richer interface for features that are difficult to manage through bot messages and buttons.

The application will support:

* Anonymous random matching and conversations.
* Gender-specific matching using virtual coins.
* User profiles and progressive nearby discovery.
* Text, photos, videos, voice messages, audio, stickers, GIFs, documents, and other supported Telegram media.
* Paid direct messages and chat requests.
* Profile likes, visitors, and social lookups.
* Virtual coins, rewards, referrals, purchases, and VIP subscriptions.
* Privacy settings, blocking, reporting, and moderation.
* Configurable advertisements, sponsored placements, and an official announcement channel.
* Administrative tools, analytics, logging, backups, and operational monitoring.

The initial interface language is English.

The initial target is approximately 10,000–50,000 registered users, with an architecture that can evolve as usage grows. Do not prematurely introduce microservices, Kubernetes, or other unnecessary infrastructure.

## 3. Non-Negotiable Product Architecture: Bot First

The Telegram bot is the primary product experience.

Users must be able to complete the essential journey without using the Mini App:

1. Start the bot.
2. Accept the applicable terms and privacy notice.
3. Create or complete their profile through conversational prompts.
4. Start free random matching.
5. Chat with their match and exchange supported media.
6. End a conversation, find another match, block a user, or report a problem.
7. View their coin balance.
8. Access paid features, the coin shop, VIP information, and support.

Use Telegram messages, reply keyboards, inline keyboards, and callback buttons where appropriate.

The Mini App is an optional extension, not a prerequisite for onboarding or ordinary chatting.

Use it for richer interactions such as:

* Editing profiles.
* Browsing nearby profiles.
* Viewing likes and profile visitors.
* Managing privacy and notification preferences.
* Viewing coin transactions and purchasing coins.
* Managing VIP subscriptions.
* Viewing account settings and help information.

Users should be able to open it from the bot through an appropriate menu button or inline button.

Do not make users navigate a complicated interface before their first successful match. Optimize onboarding and time-to-first-chat.

Remember that a Telegram bot cannot initiate a private conversation with a user who has never started it. Design referrals and promotional links around Telegram deep links and the user's explicit start action.

## 4. Start by Inspecting the Repository

Before implementing anything:

1. Inspect the current repository, files, Git status, existing code, environment, and installed tooling.
2. Determine whether the repository is empty or contains an existing implementation.
3. Identify the currently supported Python version, package manager, Docker availability, and other relevant environment constraints.
4. Read any existing project instructions before changing files.
5. If the repository is empty, establish the project structure and foundational configuration.
6. Create or update `docs/requirements.md` with this complete product specification.
7. Create or update `CLAUDE.md` with permanent project instructions, architecture conventions, testing commands, security rules, and development workflow.
8. Create `docs/architecture.md`, `docs/database.md`, `docs/security.md`, `docs/deployment.md`, and `docs/decisions.md` as appropriate.
9. Establish a practical implementation plan with milestones, dependencies, acceptance criteria, and tests.

Do not overwrite existing work blindly. Preserve useful implementation, inspect migrations before modifying them, and explain any destructive database or Git operation before proceeding.

If a critical requirement is ambiguous, document the uncertainty and ask a focused question when a safe implementation cannot proceed without the answer. For noncritical choices, select a sensible configurable default and record the decision.

## 5. Recommended Technology Stack

Prefer a simple, maintainable, Python-first architecture.

### Backend

* Python using a currently supported stable release.
* `aiogram` for Telegram Bot API integration.
* FastAPI for internal/application APIs and any required web endpoints.
* PostgreSQL as the durable source of truth.
* SQLAlchemy 2.x for database access.
* Alembic for database migrations.
* Redis for temporary matching queues, rate limiting, caching, distributed coordination, and short-lived state where appropriate.
* A lightweight background-worker approach for scheduled jobs, notifications, cleanup, and asynchronous processing.
* Pydantic for configuration and data validation.
* Pytest for automated tests.

Verify compatibility between selected dependency versions before locking them. Do not assume that a library API, Telegram feature, cloud capability, or payment rule is current without checking authoritative documentation.

### Frontend

* React.
* TypeScript.
* Vite.
* A consistent, accessible component system and responsive styling approach.
* Telegram Mini Apps SDK or supported Telegram Web App APIs when needed.

The Mini App must be mobile-first, fast, accessible, and usable within Telegram's webview.

### Deployment

* Docker and Docker Compose where supported by the chosen server architecture.
* Caddy or Nginx for HTTPS and reverse proxying.
* GitHub for version control and source management.
* GitHub Actions or an equivalent CI process for automated checks.
* Oracle Cloud Infrastructure Always Free resources as the preferred initial hosting option, subject to actual account eligibility, regional availability, quotas, and capacity.

Use an architecture compatible with the actual server CPU architecture. If deploying on Oracle Ampere A1 ARM infrastructure, verify that every container image and dependency supports ARM64.

Do not assume that Always Free resources will always be available, that every desired region can be selected, or that free infrastructure provides production-grade uptime guarantees.

Avoid paid services unless required and approved by me. Clearly identify any potential charges before enabling a paid resource.

## 6. Architecture Principles

Start with a **modular monolith**, not microservices.

Keep the bot, API, background worker, and Mini App in a coherent repository. They may run as separate processes or containers while sharing well-defined application modules.

Suggested architecture:

* Telegram Bot: user-facing commands, conversational onboarding, callbacks, and message handling.
* FastAPI application: authenticated internal/application endpoints and Telegram webhook endpoints if webhooks are selected.
* Domain/application services: business rules for matching, profiles, coins, payments, chat, privacy, and moderation.
* PostgreSQL: durable users, profiles, relationships, conversations, transactions, subscriptions, and audit records.
* Redis: temporary matching state, short-lived caches, rate limits, and coordination.
* Background worker: expiry processing, notifications, scheduled rewards, retention cleanup, and reconciliation.
* Telegram Mini App: optional richer user interface.
* Admin interface: restricted moderation, configuration, and operational functions.

Keep business logic out of Telegram handlers and frontend components. Both the bot and Mini App must call shared application services rather than implementing their own versions of business rules.

The PostgreSQL database is the source of truth for balances, payments, subscriptions, relationships, and other durable business records. Redis is not the only record of any critical financial state.

## 7. Account Creation and User Profiles

Users should be able to create a profile through the Telegram bot.

Profile fields should include:

* Telegram user identifier.
* Display name.
* User-entered age or date-of-birth representation, selected with appropriate privacy considerations.
* Gender.
* Country.
* City.
* Optional profile photo.
* Optional short biography.
* Optional interests.
* Account creation and last-active timestamps.
* Profile visibility and privacy preferences.
* Applicable terms/privacy acceptance version and timestamp.

Use Telegram's stable user identifier for account association. Usernames can change and must not be used as the primary account key.

Support manual country and city entry. GPS-based location is optional and requires explicit user permission.

Do not expose exact GPS coordinates to other users. Store only the minimum location precision required for discovery, protect location data, and allow users to disable location-based discovery.

Provide sensible validation, profile editing, default avatars, duplicate-request protection, and recovery from incomplete onboarding.

### Age and eligibility

The owner has decided not to require mandatory identity or age-document verification at launch and prefers to rely on user-entered information.

The minimum eligible age has not yet been finalized. Do not silently invent a final age policy. Make the eligibility rule configurable, document the open decision, and request confirmation before public launch.

A terms-and-privacy disclaimer does not eliminate legal responsibilities. Provide appropriate notices, report and block mechanisms, moderation procedures, and data-handling safeguards. Flag the need for jurisdiction-specific legal review before launch.

## 8. Free Random Matching

Random matching is free.

Users should be able to:

* Enter the matching queue.
* Cancel before a match is established.
* Receive a match notification.
* Start chatting.
* End the conversation.
* Find another match.

Implement safe concurrency controls so a user cannot accidentally be matched with multiple people simultaneously.

Matching must exclude:

* The same user.
* ~~Blocked users and users who have blocked the requester.~~ Amended by owner decision D-045: blocks do not affect random matching, because excluding blocked pairs would let users steer free random matches by blocking people they don't want. Blocks apply to direct messages and chat requests.
* Ineligible, banned, or suspended accounts.
* Users already engaged in incompatible active conversations.
* Users who do not satisfy the applicable privacy and matching preferences.

Avoid immediate repeated matches when suitable alternatives exist.

Use Redis for temporary queues where appropriate, but make the final match creation and any related financial transaction safe and durable.

A match must not be announced as successful until the backend has committed the necessary state.

## 9. Gender-Specific Matching and Coins

Offer paid matching filters based on gender preferences supported by the profile system.

The price must be configurable through backend settings.

Rules:

1. The user must have sufficient coins to start a paid search.
2. Searching does not itself constitute a successful match.
3. Do not permanently deduct coins for a failed search.
4. Do not deduct coins when the user cancels before a match is created.
5. Deduct the configured amount only when a valid match has been successfully established.
6. If a match cannot be committed, do not charge the user.
7. Prevent duplicate charges caused by retries, double-clicks, callback replay, worker restarts, or network failures.
8. Handle simultaneous searches and balance changes transactionally.

Implement a clear state model for search requests, reservations if required, successful matches, cancellations, timeouts, and errors.

## 10. Messaging and Media

After a match, users can exchange supported Telegram content, subject to current Telegram Bot API capabilities and limits.

Support text and, where supported, photos, videos, voice messages, audio, stickers, GIFs/animations, documents, and other common Telegram message types.

Prefer forwarding or copying messages through supported Telegram APIs where appropriate. Verify current behavior and limitations for protected content, file sizes, message types, and bot permissions.

Do not promise end-to-end encryption. Explain that messages are handled by the bot and backend, and that Telegram bot conversations are not the same as Telegram Secret Chats.

Implement:

* Correct conversation membership validation.
* Message delivery error handling.
* Duplicate-update protection.
* Rate limits and spam protection.
* Block and report actions.
* Safe handling of Telegram file identifiers.
* Privacy-conscious logging.
* Message-retention and deletion policies.
* Handling for deleted, unavailable, or unsupported content.

The bot must never deliver a private message to an unrelated user because of an incorrect conversation identifier or authorization failure.

## 11. Paid Direct Messages

Support a paid direct-message feature costing **1 coin per successful delivery**, with the exact user flow defined in the product UX.

A user must not be charged for a message that fails to reach the intended eligible recipient.

Use a transaction-safe state machine, such as pending, delivered, failed, refunded, and expired where applicable.

Requirements:

* Verify sender and recipient eligibility.
* Enforce blocks and privacy preferences.
* Prevent duplicate delivery and duplicate charges.
* Refund or release any reserved amount when delivery fails.
* Record the reason for failure.
* Make financial state changes idempotent.

Do not interpret a Telegram API response as proof that the recipient has read the message. Define delivery only in terms the platform can actually verify.

## 12. Paid Chat Requests

Support a chat-request feature costing **2 coins per request**.

A request is sent to another eligible user, who can accept or decline it. Requests should expire after a configurable duration.

The owner has explicitly chosen this refund rule:

* Accepted request: retain the charge according to the defined successful-request flow.
* Declined request: refund the 2 coins.
* Expired request: refund the 2 coins.
* Undeliverable request: refund the 2 coins if payment was already captured.
* Cancelled before the request is successfully sent: do not charge.
* Duplicate request events: do not duplicate the charge or refund.

Use atomic database transactions and idempotency keys. A request must not be accepted twice or create multiple conversations accidentally.

Define and test all request state transitions.

## 13. Profile Likes, Visitors, and Free Lookups

Implement profile likes and visitor tracking.

Users should be able to:

* Like another profile.
* View profiles that liked them.
* View profiles they have liked.
* View profile visitors/history.
* Open eligible profiles and inspect their public profile details.

These lookups are **free**. Do not impose coin charges for viewing likes, visitors, or liked profiles.

Respect blocks, profile visibility, account eligibility, and privacy settings.

Prevent repeated requests from creating duplicate likes or inflating visitor counts. Decide and document how visitor history is deduplicated and retained.

## 14. Progressive Nearby Discovery

Nearby discovery should expand geographically based on available eligible profiles. Do not impose one fixed radius as the only discovery method.

Preferred discovery sequence:

1. Same city.
2. Nearby cities.
3. Same state, province, or region.
4. Same country.
5. International discovery, when enabled and appropriate.

Expand only as needed, with clear UI feedback about the search scope.

Do not claim that a city is nearby unless the application has a reasonable basis for that conclusion.

Use normalized geographic data or a suitable geocoding/geographic dataset to map cities and regions. Research licensing, coverage, and hosting requirements before selecting a dataset or external API.

Manual city entry should support normalization and common spelling variants. GPS is optional and must never be required for ordinary matching.

Avoid exposing exact coordinates, home locations, or unnecessary distance precision. Users must be able to disable location-based discovery.

## 15. Presence and Activity Notifications

Offer a paid notification feature costing **1 coin**, subject to a clearly defined notification event.

Important limitation: a Telegram bot cannot reliably inspect every user's global Telegram online/offline status.

Therefore, do not claim to detect actual Telegram presence unless a supported, documented capability genuinely exists.

Implement RealChat activity instead, based on signals such as:

* Recent activity in RealChat.
* Last active within the application.
* A configurable RealChat online/active state.

Clearly label the feature so users understand what it reports.

Allow users to manage notification preferences. Apply privacy settings, opt-outs, rate limits, and anti-harassment protections. Define what a one-coin purchase subscribes to, its duration, and whether it generates one event or repeated alerts before accepting payment.

Do not charge users for notifications that cannot be activated.

## 16. Virtual Coin Economy

Implement a reliable virtual-currency system.

Coins may be obtained through:

* Configurable daily rewards.
* Approved promotional rewards.
* Referral rewards.
* Telegram Stars purchases.
* Configurable VIP subscription benefits.
* Other explicitly enabled reward campaigns.

Build a configurable coin shop.

Every coin balance change must have a durable ledger entry. Include transaction types such as:

* Purchase.
* Daily reward.
* Referral reward.
* Promotional reward.
* Paid matching.
* Direct message.
* Chat request.
* Refund.
* Administrative adjustment.

Use a transaction ledger rather than allowing arbitrary balance changes.

Requirements:

* Database transactions for balance updates.
* Unique idempotency keys.
* Immutable transaction records, with corrections represented by compensating entries.
* No negative balances unless a separately approved business rule requires them.
* Server-side validation of every transaction.
* Clear balance and transaction-history views.
* Reconciliation and audit capabilities.
* Protection against concurrent spending and duplicate webhook events.

Never trust a balance, product price, purchase status, or VIP status supplied by the client.

## 17. Telegram Stars Payments and VIP

Telegram Stars is the initial payment method for digital goods and subscriptions within Telegram.

Before implementation, read the current official Telegram Bot Payments and Stars documentation. Confirm applicable rules for digital goods, currency, payment updates, refunds, recurring subscriptions, subscription cancellation, and transaction records.

Do not assume that historical examples or third-party tutorials reflect current requirements.

Implement:

* Configurable coin packages.
* Telegram Stars invoices.
* Successful payment processing.
* Idempotent payment event handling.
* Refund workflows supported by the current platform.
* Subscription creation, renewal, expiry, and cancellation where supported.
* Reconciliation and administrative visibility into payment status.
* Clear user-facing purchase confirmation and error messages.

Never grant purchased coins based solely on a client callback or a request parameter. Grant them only after verifying the authoritative successful payment event.

Do not hard-code payment prices or product identifiers throughout the codebase.

### VIP subscription

VIP is a configurable subscription with potential benefits including:

* A clearly labeled VIP badge.
* Included coins under configurable subscription rules.
* Priority matching.
* The ability to hide the VIP user's profile visits from other users.
* Additional future perks.

The VIP badge is a subscription badge, not identity verification or proof that the user is a real-world public figure.

Users must be able to manage their subscription and understand its terms. Do not imply that VIP provides anonymity from administrators, legal obligations, or required security systems.

When VIP expires, revoke subscription-only entitlements according to documented rules without deleting unrelated user data or purchased coin balances.

## 18. Advertising and Announcements

Support two distinct advertising mechanisms.

### A. Telegram-native advertising

Investigate official Telegram sponsored-message and advertising capabilities, including current eligibility, placement, and bot-channel requirements.

Do not promise that the bot owner can insert an ad directly beneath Telegram's native bot title bar. That area and Telegram-native sponsored placements are controlled by Telegram's client and platform rules, not freely programmable by the bot.

Use native advertising only where the official platform supports it.

### B. RealChat-sponsored placements

Implement an optional, configurable system for RealChat-controlled sponsored content, subject to Telegram rules.

Possible placements include:

* Clearly labeled sponsored bot messages.
* Sponsored cards or banners in the Mini App.
* Promotional offers and campaign links.
* Admin-managed campaigns with start/end dates and enable/disable controls.

Requirements:

* Label advertising clearly.
* Track impressions and clicks where lawful and technically feasible.
* Avoid collecting unnecessary personal information.
* Prevent spam and excessive advertising.
* Support campaign budgets or limits where applicable.
* Restrict destinations and validate links.
* Keep advertising separate from private message content.
* Do not make ad viewing a requirement for essential safety features.

Create an official Telegram channel for announcements, updates, and approved promotions. The channel is supplementary and must not replace core bot functionality.

## 19. Safety, Reporting, Blocking, and Moderation

Implement safety features from the initial release, not as a later optional enhancement.

Users should be able to:

* Block another user.
* Report profiles and conversations.
* Select a report reason.
* Provide optional supporting context.
* Stop receiving direct messages and chat requests from blocked users. Blocked users can still be matched again in random chat (D-045).
* Leave a conversation immediately.
* Control discoverability and relevant privacy settings.

Administrative features should include:

* A restricted moderation queue.
* Report review and status changes.
* Temporary restrictions and account suspensions.
* Ban and unban workflows.
* User and conversation lookup under access controls.
* Administrative action logs.
* Abuse and spam rate limits.
* Configurable prohibited-content policies.
* A documented process for handling severe or urgent reports.

Do not automatically give every administrator unrestricted access to all data. Apply least privilege, audit access to sensitive records, and avoid storing unnecessary content.

Provide a way to appeal moderation actions where appropriate.

## 20. Conversation History and Data Retention

Conversation history may be retained temporarily for moderation, verification of reports, and abuse investigation.

The exact retention duration has not yet been finalized. Implement a configurable retention policy and propose an initial default for the owner to review before launch.

Requirements:

* Document what content is retained and why.
* Automatically delete expired conversation content and related temporary files.
* Keep retention periods separate for conversation content, payment records, moderation records, security logs, and audit records.
* Do not delete financial records merely because chat content expires.
* Restrict access to retained conversation content.
* Avoid putting message text, private profile data, payment details, tokens, or precise location data in ordinary application logs.
* Define deletion behavior for user accounts, backups, and associated records.
* Explain any limitations caused by Telegram's own message-delivery and storage behavior.

Do not claim that deleting a local database record deletes a message from a user's Telegram chat.

## 21. Security Requirements

Treat security as a core implementation requirement.

At minimum:

* Use environment variables or a suitable secret-management approach for credentials.
* Commit `.env.example`, never actual secrets.
* Validate and authenticate all API requests.
* Verify Telegram Mini App initialization data on the server using the official verification procedure.
* Use HTTPS for public endpoints.
* Validate Telegram webhook secrets if webhooks are used.
* Use database transactions for all financial operations and critical state transitions.
* Use parameterized queries and safe ORM patterns.
* Apply input validation, output encoding, and safe URL handling.
* Enforce authorization for every user-specific resource.
* Prevent insecure direct object references.
* Apply per-user and per-IP rate limits where appropriate.
* Protect against duplicate Telegram updates and webhook replays.
* Keep PostgreSQL and Redis inaccessible from the public internet unless there is a specifically justified, secured exception.
* Restrict SSH and administrative access.
* Rotate credentials when necessary.
* Redact sensitive data from logs.
* Use secure backup and restore procedures.
* Maintain dependency updates and vulnerability checks.
* Audit privileged actions.
* Test abuse, concurrency, and authorization boundaries.

Never trust the frontend to enforce permissions, coin prices, balances, privacy preferences, matching eligibility, or payment outcomes.

## 22. Database Design

Design a normalized PostgreSQL schema with appropriate constraints, indexes, foreign keys, uniqueness rules, and migrations.

Evaluate tables or equivalent models for:

* Users and Telegram identities.
* Profiles and profile photos.
* Location and geographic normalization.
* Matching preferences.
* Privacy and notification preferences.
* Blocks.
* Interests.
* Matching requests and match attempts.
* Conversations and participants.
* Message metadata and retention.
* Coin accounts or derived balances.
* Immutable coin transactions.
* Coin products and pricing configuration.
* Payment orders and payment events.
* Subscriptions and subscription events.
* Likes.
* Profile visits.
* Chat requests and their state transitions.
* RealChat presence/activity.
* Reports.
* Moderation actions.
* Notification jobs/outbox.
* Advertising campaigns and events.
* Referrals and referral rewards.
* Application settings.
* Administrative audit logs.

This is a starting point, not a requirement to create unnecessary tables. Use a coherent schema based on actual domain boundaries.

Document:

* Table purposes.
* Relationships and cardinality.
* Uniqueness constraints.
* Important indexes.
* Financial invariants.
* Retention and deletion behavior.
* Migration and rollback strategy.

Use Alembic migrations for schema changes. Do not rely on silently creating or altering production tables during application startup.

## 23. Matching and Financial Concurrency

Matching, coin deductions, payments, chat requests, and refunds must be safe under concurrent requests.

Use database transactions, unique constraints, appropriate row locks or equivalent concurrency control, and idempotency keys.

For every financial or matching workflow, define:

1. The initial state.
2. Allowed transitions.
3. The authoritative source of truth.
4. The transaction boundary.
5. Duplicate-event behavior.
6. Failure and timeout handling.
7. Refund or compensation behavior.
8. Audit events.
9. Automated tests.

Use Redis for temporary coordination where useful, but do not let cache failures or stale cached data create duplicate charges or invalid matches.

Build automated concurrency tests for simultaneous matching requests, coin spending, payment callbacks, chat-request acceptance, and refund processing.

## 24. Suggested Repository Structure

Adapt this structure as needed, but preserve clear separation of responsibilities.

```
realchat/
├── apps/
│   ├── bot/
│   ├── api/
│   ├── worker/
│   └── miniapp/
├── backend/
│   ├── core/
│   ├── config/
│   ├── database/
│   ├── integrations/
│   └── modules/
│       ├── users/
│       ├── profiles/
│       ├── locations/
│       ├── matching/
│       ├── messaging/
│       ├── coins/
│       ├── payments/
│       ├── subscriptions/
│       ├── social/
│       ├── notifications/
│       ├── moderation/
│       ├── advertising/
│       ├── referrals/
│       └── administration/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/
│   ├── concurrency/
│   └── e2e/
├── infra/
│   ├── docker/
│   ├── proxy/
│   ├── oracle-cloud/
│   └── scripts/
├── docs/
├── .github/
│   └── workflows/
├── .env.example
├── .gitignore
├── CLAUDE.md
├── README.md
├── pyproject.toml
└── docker-compose.yml
```

Do not create empty folders or placeholder modules just to match this diagram. Build modules when they are needed.

## 25. User Experience Requirements

The bot should feel straightforward and responsive, not like a complex command-line interface.

Provide:

* Clear `/start` onboarding.
* A concise main menu.
* Inline buttons for primary actions.
* Clear matching and cancellation states.
* Visible coin balance and transparent prices.
* Confirmation for purchases and other consequential actions.
* Understandable errors and retry options.
* Block/report actions available during a conversation.
* Easy conversation exit and rematching.
* Helpful empty states.
* Clear VIP benefits and limitations.
* A way to reach help and the official announcement channel.

The Mini App should provide consistent navigation, loading indicators, error handling, accessible controls, and mobile-responsive layouts.

Do not build a feature only in the Mini App if it is essential to starting or continuing an ordinary chat.

## 26. Testing and Acceptance Criteria

Use automated tests throughout development.

### Unit tests

Test matching rules, profile validation, privacy checks, coin calculations, refunds, subscriptions, retention logic, and permission checks.

### Integration tests

Test Telegram update processing, database operations, Redis interactions, payment event handling, notification delivery, and background jobs using safe test doubles or sandbox environments where available.

### Security tests

Test unauthorized access, cross-user data exposure, invalid Mini App initialization data, replayed updates, abusive request rates, manipulated prices, and attempts to bypass privacy settings.

### Concurrency tests

Test simultaneous matches, duplicate payment callbacks, duplicate refunds, simultaneous coin spending, and repeated chat-request acceptance.

### End-to-end tests

Test the principal user journeys from onboarding through matching and messaging, plus purchases and paid features using mocked or test payment flows.

Do not require real money or real user accounts in automated tests.

### Initial release acceptance

The initial release is not complete until:

* A new user can onboard through the bot.
* Users can create and update profiles.
* Free random matching works between two eligible test users.
* Matched users can exchange supported messages and media.
* Users can end conversations, block users, and submit reports.
* Gender-specific matching charges only on successful match creation.
* Paid direct messages enforce the configured price and refund failure conditions.
* Chat requests charge 2 coins and refund declined, expired, or undeliverable requests as specified.
* Free profile, likes, liked-profile, and visitor lookups work.
* Progressive geographic discovery respects privacy and preferences.
* Coin balances and transaction history remain correct under concurrent requests.
* Telegram Stars payment handling is verified against current official rules.
* VIP benefits and expiry behave correctly.
* Moderators can review reports and apply restrictions.
* Retention cleanup works according to the configured policy.
* Secrets are not committed.
* Database migrations work on a clean database.
* Automated tests pass.
* Backup restoration has been tested.
* Deployment and rollback instructions are documented.

Do not mark an acceptance criterion complete based solely on code existing. Demonstrate it with tests or a documented manual verification procedure.

## 27. Oracle Cloud Deployment

The initial target is Oracle Cloud Always Free where resources are actually available.

Before deploying:

1. Verify the account's available region, compute shape, architecture, quotas, storage, and network constraints.
2. Confirm whether the desired resources are eligible for Always Free usage.
3. Estimate possible charges, including storage, networking, public IPs, and any non-free services.
4. Design the smallest viable deployment that can run the bot, API, worker, PostgreSQL, Redis, and optional Mini App hosting requirements.
5. Decide whether PostgreSQL and Redis should run in containers on the same host initially or use another approved arrangement.
6. Secure firewall rules and administrative access.
7. Configure a domain and HTTPS if needed.
8. Configure environment variables and secrets.
9. Run migrations and seed only non-sensitive development data.
10. Deploy and verify health checks, logs, webhook or polling behavior, and bot functionality.
11. Configure backups, recovery procedures, monitoring, and safe updates.
12. Document deployment, redeployment, rollback, and server-recovery steps.

Avoid exposing database ports publicly. Do not claim high availability or guaranteed uptime from a single free-tier server.

Do not perform destructive cloud operations, incur paid charges, or modify production data without explicit approval.

## 28. CI/CD and Maintenance

Set up continuous integration to run:

* Formatting and lint checks.
* Type checks where applicable.
* Unit and integration tests.
* Migration checks.
* Security and dependency checks.
* Frontend build checks.

Use automated deployment only when its credentials, permissions, target environment, and rollback procedure are clearly understood and approved.

Document regular maintenance procedures for:

* Dependency updates.
* Telegram Bot API changes.
* Payment reconciliation.
* Database backups and restoration.
* Log review and retention cleanup.
* Moderation operations.
* Monitoring and incident response.
* Security incidents and credential rotation.
* Server capacity and cost checks.

Make it possible to maintain the application without relying on undocumented decisions or manual edits inside a running production container.

## 29. Development Workflow

Work in small, reviewable, complete increments.

For each milestone:

1. Explain the goal and dependencies.
2. Identify the files and modules to create or modify.
3. Explain important design choices and trade-offs.
4. Implement a coherent feature.
5. Add or update tests.
6. Run the relevant tests and quality checks.
7. Fix failures before declaring the work complete.
8. Update documentation and configuration examples.
9. Summarize what changed and how to verify it.
10. Identify remaining limitations and the next milestone.

Do not generate the entire application in one giant response or leave core functionality as TODO comments.

Do not fabricate test results, deployment status, API behavior, or completed features. If a command cannot be run in the available environment, say so and provide the exact verification steps.

When a feature depends on a third-party service, inspect the current official documentation before implementation. Clearly distinguish verified behavior from assumptions.

Do not reveal secrets in logs, explanations, generated files, or commits.

## 30. Implementation Milestones

Use this sequence unless repository inspection reveals a compelling reason to change it.

### Milestone 1 — Project foundation

* Repository structure and instructions.
* Configuration and environment validation.
* Database and initial migrations.
* Redis integration.
* Structured logging.
* Health checks.
* Docker-based local development.
* CI pipeline and baseline tests.

### Milestone 2 — Bot onboarding and profiles

* Telegram bot initialization.
* `/start` and terms/privacy acceptance.
* Conversational profile creation and editing.
* Profile photo and default avatar.
* Profile visibility and preferences.
* Basic account safety controls.

### Milestone 3 — Core random chat

* Matching queue.
* Atomic match creation.
* Conversation state.
* Text and supported media relay.
* End conversation and rematch.
* Block and report actions.
* Rate limits and matching concurrency tests.

### Milestone 4 — Coin ledger and paid matching

* Coin ledger and balance calculations.
* Configurable prices.
* Gender-specific matching.
* Idempotent spending and refund rules.
* Daily and promotional rewards.
* Transaction-history interface.

### Milestone 5 — Paid interactions and social features

* Direct messages.
* Chat requests and expiry processing.
* Likes and visitor history.
* Free profile lookups.
* Notification preferences.
* RealChat activity notifications.

### Milestone 6 — Payments and VIP

* Telegram Stars purchases.
* Payment reconciliation.
* Refund support.
* VIP subscription lifecycle.
* Included coins and subscription entitlements.
* VIP badge and private profile visits.

### Milestone 7 — Location and Mini App

* Location normalization.
* Progressive nearby discovery.
* Optional GPS permission flow.
* Mini App authentication and API integration.
* Profile editing, discovery, likes, visitors, shop, VIP, and settings interfaces.

### Milestone 8 — Moderation, ads, and referrals

* Moderation queue and administrative controls.
* Referral tracking and reward protections.
* Sponsored placements.
* Official announcement channel links.
* Campaign controls and analytics.

### Milestone 9 — Production hardening and launch

* Security review.
* Retention and deletion verification.
* Load and concurrency tests.
* Backup restoration test.
* Oracle Cloud deployment.
* Monitoring and alerts.
* Operational documentation.
* Final release acceptance checklist.

The ordering may be adjusted when dependencies require it, but safety, secure financial logic, and correct data handling must not be postponed until the end.

## 31. Rules for Future Feature Requests

This project will evolve after launch.

When I request a new feature or change:

1. Read `CLAUDE.md` and the relevant requirements, architecture, and decision documents.
2. Inspect the existing implementation before changing it.
3. Identify affected modules, data models, API contracts, user flows, and business rules.
4. Check for backwards compatibility and migration requirements.
5. Explain important trade-offs and risks.
6. Implement the change using existing conventions where appropriate.
7. Add tests for the new behavior and regression tests for related functionality.
8. Update documentation and configuration examples.
9. Run all relevant checks.
10. Report the files changed, test results, deployment requirements, and any remaining limitations.

Do not rewrite working modules unnecessarily. Do not silently change prices, retention periods, privacy behavior, payment rules, or other product decisions.

If a new request conflicts with an existing requirement, identify the conflict and ask for clarification before making a consequential change.

## 32. Required First Response

Begin by inspecting the repository and available environment.

Then provide:

1. A concise assessment of the current project state.
2. Any critical blockers or important decisions requiring my input.
3. The proposed implementation plan.
4. The initial files and configuration you will create or modify.
5. The first small, testable milestone you will implement.

If the repository is empty, start with Milestone 1. Do not stop after writing a plan unless a critical blocker requires my decision.

**Final objective:** build RealChat as a reliable, secure, maintainable, self-hosted Telegram-first product that can be operated by its owner, tested systematically, deployed with documented procedures, and extended safely over time.
