# Decisions

This file records decisions about RealChat's product, architecture, and operations. Each entry states what was decided, why, and when.

* **Approved** entries were explicitly confirmed by the project owner.
* **Open** entries are unresolved. Until one is decided, the related behavior must stay configurable and must not be presented as final.

To change an approved decision, add a new entry that supersedes it rather than editing the old one.

---

## Approved

### D-001 — Python 3.13

**Date:** 2026-10-26
**Decision:** The backend targets Python 3.13.
**Why:** 3.13 is a current stable release with established library support. 3.14 is newer, and some dependencies may not fully support it yet. Each dependency's compatibility is checked against its official documentation before versions are locked.

### D-002 — `uv` for Python package management

**Date:** 2026-10-26
**Decision:** `uv` manages the Python version, virtual environment, dependencies, and lockfile.
**Why:** It is fast, produces a reproducible lockfile, and handles both the interpreter and the packages.

### D-003 — Docker for local services and deployment

**Date:** 2026-10-26
**Decision:** PostgreSQL and Redis run in Docker containers locally, through Docker Compose, and on the server. Docker Desktop is used for development on macOS.
**Why:** The development and production environments stay alike, and nothing needs to be installed directly on the machine. Images must support ARM64, because both the development Mac and the Oracle Ampere A1 target use ARM64.

### D-004 — Branch strategy

**Date:** 2026-10-26
**Decision:** Long-lived branches:

* `main` — production-ready
* `staging` — staging validation
* `develop` — integration

Work happens on short-lived issue branches, such as `feature/12-onboarding`, created from `develop`. A branch is merged only through an approved pull request.
**Why:** The owner specified this strategy in `CLAUDE.md`. All three branches were created from the initial commit.

### D-005 — Single decision log

**Date:** 2026-10-26
**Decision:** Decisions are recorded in this one file, `docs/decisions.md`, rather than in a folder of separate records.
**Why:** Owner preference: one place to read every decision.

### D-006 — Public repository

**Date:** 2026-10-26
**Decision:** `bhargavikusi/RealChat` stays public.
**Why:** Owner preference.
**Consequences:**

* Anyone can read the code, the documentation, and the commit history, so secrets must never be committed (see `.gitignore` and `.env.example`).
* Documentation must not describe moderation or anti-abuse thresholds in a way that helps someone evade them. Such values belong in configuration.

### D-007 — Commit identity

**Date:** 2026-10-26
**Decision:** Commits are authored as `bhargavikusi <bhargavikusi@gmail.com>`, and project work carries no AI attribution.
**Why:** The project belongs solely to the `bhargavikusi` account.

---

## Open

Each of these needs the owner's decision. Until then, the behavior stays configurable and is not presented to users as final.

| ID | Question | Needed before | Notes |
|---|---|---|---|
| O-001 | Minimum eligible age | Public launch | Age is user-entered, with no document verification (approved in requirements §7). Jurisdiction-specific legal review is recommended. |
| O-002 | Retention period for conversation content | Public launch | Kept separate from the retention of payment, moderation, security, and audit records (requirements §20). |
| O-003 | Terms of the 1-coin activity notification | Before that feature is built | What event triggers it, how long it lasts, and whether it is one alert or several (requirements §15). |
| O-004 | Price of gender-specific matching | Before that feature is built | Must be configurable (requirements §9). |
| O-005 | Coin package and VIP pricing in Telegram Stars | Before payments are built | Also covers what VIP includes and how many coins it grants (requirements §17). |
| O-006 | Webhook or long polling for receiving Telegram updates in production | Before deployment | Development will use polling. Webhooks require a public HTTPS endpoint. |
