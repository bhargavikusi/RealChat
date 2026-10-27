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
* The entered age is stored with the date it was entered. The displayed age is an **estimate**: that number plus the number of full years since. It advances on the anniversary of entry, not the user's birthday, so it can be up to a year below the real age.
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

### D-017 — Under-age entries can be corrected

**Date:** 2026-10-27
**Decision:**

* A user who enters an age below the configured minimum is refused politely and cannot continue.
* The user may immediately send a different age. No block or cooldown is applied.

**Why:** Owner preference. Age is self-declared (D-010), so the check stops honest under-age users and genuine typos. It cannot stop a determined false entry.

### D-018 — Per-user event isolation uses Redis locks

**Date:** 2026-10-27
**Decision:**

* The bot handles each user's updates one at a time using aiogram's `RedisEventIsolation`.
* Each lock expires after 60 seconds, so a lock left by a crashed process cannot block a user for longer than that.

**Why:** The in-process alternative (`SimpleEventIsolation`) keeps one lock per user forever, so memory grows until restart. It also only works within a single process. Redis locks keep no per-user memory in the process, and they stay correct if webhooks with several workers are adopted (O-006). The cost is about two Redis calls per update. Onboarding already depends on Redis for FSM state, so this adds no new point of failure.

### D-019 — Age and gender changes are limited by a cooldown

**Date:** 2026-10-27
**Decision:**

* After onboarding, age and gender can each be changed at most once per cooldown period. The period is the setting `PROFILE_CHANGE_COOLDOWN_DAYS`, 30 days by default.
* The first change after onboarding is always allowed, so a typo can be fixed straight away.
* A new age must still meet the minimum age (D-009). An under-age entry is refused and can be corrected (D-017).
* Choosing the current value is not a change and does not start the cooldown.
* Nickname, country, city, bio, and photo can be changed at any time.

**Why:** Age and gender drive eligibility and paid gender matching. Free switching would let users game paid matching, while a full lock would stop honest users fixing mistakes.

### D-020 — Default avatar per gender

**Date:** 2026-10-27
**Decision:**

* A profile without a photo is shown with a neutral default avatar. There is one image each for Male, Female, and Other (`apps/bot/assets/avatars/`).
* Each image is uploaded to Telegram once and reused by its file identifier, cached in Redis per bot. If the cache is lost, the image is uploaded again.
* The current images are simple placeholders. Final artwork belongs to the pre-launch polish pass.

**Why:** Owner preference. Every profile card then has the same shape, a photo with the profile as its caption.

### D-021 — Hidden profiles are a premium feature

**Date:** 2026-10-27
**Decision:**

* A hidden profile does not appear in nearby discovery or other users' browse lists, and cannot be visited.
* A hidden user can still use random matching and chat, and chat partners still see their profile.
* Hiding the profile is a premium feature. The setting is built with the VIP work, not with profile management. Until then every profile stays visible (the stored default).

**Why:** It is a privacy control that does not stop the user using the core product. Owner decision to make it premium.
**Still open:** Which premium offer includes it, and what happens to a hidden profile when that offer ends (O-008).

### D-022 — Main menu layout

**Date:** 2026-10-27
**Decision:**

* The keyboard under the message box holds only the primary actions, in at most two rows: 🎭 New Anonymous Chat on the first row, 📍 Nearby Peoples and 🔎 Browse People on the second. A Mini App button may join them if it fits within the two rows.
* Each keyboard button is added when its feature ships. Until matching exists, no keyboard is shown.
* Everything else, such as the profile, help, and later the coin balance, is reached from Telegram's command menu (`/start`, `/profile`, `/help`) or, if built, the Mini App.
* Section entries, such as the keyboard buttons, `/profile`, and `/help`, reply with a message carrying inline buttons for that section. `/start` replies with plain text.

**Why:** Owner preference. The most frequent actions stay one tap away, and the keyboard never shows a button that does not work.

### D-023 — Help contents

**Date:** 2026-10-27
**Decision:** `/help` shows a short guide to RealChat, the available commands, and a button to read the current terms again. Links to the announcement channel (`ANNOUNCEMENT_CHANNEL_URL`) and support (`SUPPORT_CONTACT`) are shown only when they are configured, and must be `https://` links.
**Why:** Requirements §25 asks for a way to reach help and the announcement channel. Neither exists yet, so nothing links to a missing page.

### D-024 — Change history for age and gender

**Date:** 2026-10-27
**Decision:**

* Every age or gender change after onboarding adds a row to the append-only `profile_changes` table: the field, the old value, the new value, and the time.
* The cooldown (D-019) is read from this history. The profile row is locked while a change is checked and written, so two simultaneous changes cannot both pass the cooldown.
* Values set during onboarding are not recorded.

**Why:** Moderators can see a pattern of changes, such as a user who keeps switching gender, which last-changed timestamps alone would not show.
**Consequence:** The history holds age and gender values, so account deletion (D-016) must erase it with the rest of the profile data. How long it is kept otherwise is open (O-009).

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
| O-008 | Which premium offer includes hiding the profile, and what happens when it ends | Before the VIP work | D-021. For example, whether the profile becomes visible again automatically. |
| O-009 | Retention period for the age and gender change history | Public launch | D-024. Separate from conversation content (O-002), because moderators use the history to spot repeated switching. Until decided, rows are kept until account deletion. |
