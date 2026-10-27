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

### D-008 — Branch protection ruleset

**Date:** 2026-10-27
**Decision:** The repository ruleset "Protect long-lived branches" covers `develop`, `staging`, and `main`:

* Changes go through pull requests.
* Both CI jobs ("Lint, types, tests, migrations" and "Dependency vulnerability scan") must pass.
* Force pushes and branch deletion are blocked.
* No reviewer approval is required, because GitHub does not let the sole maintainer approve their own pull requests.
* Repository admins may bypass the rules only when merging a pull request, never by pushing directly.

**Why:** It enforces in GitHub the workflow `CLAUDE.md` requires. A direct push to `develop` was tested and rejected.

### D-009 — Minimum age for development: 18

**Date:** 2026-10-27
**Decision:** Onboarding rejects users below a configurable minimum age, set to **18** for development.
**Why:** Anonymous chat with strangers carries adult-content and grooming risks.
**Still open:** The final launch value, ideally after jurisdiction-specific legal review (see O-001).

### D-010 — Age entered as a number

**Date:** 2026-10-27
**Decision:**

* Users enter their age as a whole number. No birth date or birth year is collected.
* The entered age is stored with the date it was entered. The current age is that number plus the number of full years since.
* Other users only ever see the current age.

**Why:** It collects the least personal data while keeping ages current without users re-entering them.
**Consequence:** Eligibility depends on the user telling the truth. Identity and age verification are not required at launch (requirements §7).

### D-011 — Gender options: Male, Female, Other

**Date:** 2026-10-27
**Decision:** Profiles offer exactly Male, Female, and Other. Paid gender-specific matching can target any of the three.
**Why:** The options are simple to match on, and they give users who are neither male nor female an accurate choice.

### D-012 — Display name is a user-chosen nickname

**Date:** 2026-10-27
**Decision:**

* Onboarding asks for a nickname.
* The user's Telegram name is never shown to other users and is not used as a default.

**Why:** It protects anonymity. Many users would otherwise expose their real first name without realizing it.

### D-013 — Required onboarding fields

**Date:** 2026-10-27
**Decision:**

* **Required** before reaching the main menu: nickname, age, gender, and country. Country is typed and matched against the ISO 3166 country list.
* **Optional** at any time: city, photo, and bio.

**Why:** It keeps the time to a first chat short, as requirements §3 asks, while capturing the country that discovery depends on.

### D-014 — Interests deferred to discovery work

**Date:** 2026-10-27
**Decision:** Profile interests are not built with onboarding. They will be designed together with nearby discovery, which is what uses them.
**Why:** It keeps onboarding small and avoids locking in a data model before its main consumer exists.

### D-015 — Draft terms and privacy text during development

**Date:** 2026-10-27
**Decision:**

* Development uses a short, plain-English terms and privacy draft, clearly labelled "DRAFT – not for launch".
* The text is versioned, and users must re-accept it whenever the version changes.

**Still open:** Legally reviewed text must replace the draft before launch.

### D-016 — Account deletion

**Date:** 2026-10-27
**Decision:**

* Deleting an account requires confirmation.
* All profile data is erased immediately: nickname, age, gender, country, city, bio, and photo reference.
* A minimal account record is kept: the Telegram user ID, the account status, and timestamps. This prevents moderation restrictions from being bypassed by deleting the account and re-registering.
* A deleted user who returns starts onboarding again, unless they are restricted.
* Financial records will be kept as required by law, even after deletion.

**Why:** It erases what other users could see or identify while preserving the integrity of bans.

---

## Open

Each of these needs the owner's decision. Until then, the behavior stays configurable and is not presented to users as final.

| ID | Question | Needed before | Notes |
|---|---|---|---|
| O-001 | Final minimum eligible age | Public launch | Development uses 18 (D-009). Age is user-entered, with no document verification (requirements §7). Jurisdiction-specific legal review is recommended. |
| O-007 | Final terms of service and privacy policy text | Public launch | Development uses a versioned draft (D-015). It needs legal review. |
| O-002 | Retention period for conversation content | Public launch | Kept separate from the retention of payment, moderation, security, and audit records (requirements §20). |
| O-003 | Terms of the 1-coin activity notification | Before that feature is built | What event triggers it, how long it lasts, and whether it is one alert or several (requirements §15). |
| O-004 | Price of gender-specific matching | Before that feature is built | Must be configurable (requirements §9). |
| O-005 | Coin package and VIP pricing in Telegram Stars | Before payments are built | Also covers what VIP includes and how many coins it grants (requirements §17). |
| O-006 | Webhook or long polling for receiving Telegram updates in production | Before deployment | Development will use polling. Webhooks require a public HTTPS endpoint. |
