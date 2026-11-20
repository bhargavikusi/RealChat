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
**Resolved by D-070:** 18 is the launch value. `MIN_AGE` may be set higher but never lower.

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

### D-025 — Account deletion in the bot

**Date:** 2026-10-27
**Decision:**

* Deletion is reached from the **⚙️ Advanced settings** button on the profile card, which opens a **🗑 Delete account** button. A second, explicit confirmation is always required: since D-067, typing DELETE (the one-tap "Yes, delete my account" button was replaced). The Cancel button belongs to one request, so an old or repeated one does nothing.
* Deletion follows D-016 and also erases the age and gender change history (D-024) and the terms acceptances.
* A returning user starts onboarding from the terms. Accepting them makes a deleted account active again. `deleted_at` keeps the time of the last deletion.
* Accepting the current terms comes first. Deletion is available to users who have accepted the current terms, through the profile card. A profile card sent before a terms update shows the new terms instead. The one exception is suspended and banned users, who can delete from their notice without accepting the terms (D-026).
* The draft terms describe deletion (terms version `draft-3`).

**Why:** Owner preference for the entry point and for terms acceptance coming first. Erasing terms acceptances makes a return a genuinely fresh start.
**Consequence:** An unrestricted user who will not accept updated terms cannot delete their data in the bot and must ask support. Data-protection rules in some regions may require deletion without such a step, so this should be part of the legal review (O-007).

### D-026 — Suspended and banned users

**Date:** 2026-10-27
**Decision:**

* Suspended and banned users are stopped before every handler except help and account deletion. Anything else gets a notice that offers a **🗑 Delete account** button, and in group chats nothing is sent.
* They can still read `/help` and delete their account. Deleting erases their profile, but the account stays suspended or banned, so they cannot sign up again.
* Suspensions have no end date yet: restrictions are set and lifted manually until moderation tools exist.

**Why:** Owner decision. It respects users' ability to erase their data without weakening a ban.

### D-027 — Matching queue in Redis, matches confirmed in PostgreSQL

**Date:** 2026-10-27
**Decision:** Users who are searching wait in a Redis queue with an expiry. A match is confirmed and written to PostgreSQL in one transaction, with constraints that stop anyone being in two active conversations, and it is announced only after that commit. Stale queue entries (expired, restricted, deleted, or already matched users) are skipped and removed.
**Why:** Owner decision. Redis keeps searches fast; PostgreSQL stays the source of truth for matches (requirements §8, §23).
**Consequence:** Queue and database can disagree after a Redis restart or a crash, so the matching code must re-check every candidate in PostgreSQL, and tests must cover lost and stale queue entries.

### D-028 — Chat messages are stored for a limited time

**Date:** 2026-10-27
**Decision:**

* Relayed message text, captions, message types, Telegram file identifiers, and timestamps are stored for moderation and report verification.
* They are deleted automatically after the retention period, an admin runtime setting with a default of 7 days (D-035). Messages tied to an open report are kept until the report is closed. How they are deleted: D-046.
* Access is restricted to authorised code paths; ordinary logs never contain message content. The terms describe this before storage starts.

**Why:** Owner decision. Reports can be checked against what was actually said, while keeping private content only briefly (requirements §20).
**Still open:** The launch retention value (O-002).

### D-029 — Matched users see each other's full profile card

**Date:** 2026-10-27
**Decision:** When a match is made, each user receives the other's profile card, the same card `/profile` shows: photo or default avatar, nickname, age, gender, country, city, and bio. The terms say so.
**Why:** Owner decision.

### D-030 — Chat keyboard during a conversation (amends D-022)

**Date:** 2026-10-27
**Decision:**

* While a user is in a conversation, the keyboard under the message box is replaced by chat controls in at most two rows: 👤 Partner profile and 🔒 Private mode (🔓 Private mode off while it is on, D-047), then ⏹ End chat. The main keyboard (D-022) returns when the conversation ends.
* The fourth slot is unassigned (O-010).
* Until O-010 is decided, 🚫 Block and ⚠️ Report are inline buttons on the partner's profile card and on the "chat ended" message.

**Why:** Owner preference: one-tap controls in the same place as the main menu.

### D-031 — Private mode

**Date:** 2026-10-27
**Decision:**

* Either user in a conversation can turn on 🔒 Private mode. Messages sent while it is on are deleted from both users' Telegram chats when it is turned off or the conversation ends.
* Telegram lets bots delete messages only within 48 hours, so private mode turns itself off before that limit.
* Private-mode messages are still stored for moderation with the normal retention (D-028), and the in-bot note says so.
* It is called "Private mode". It is not described as a Telegram Secret Chat, as end-to-end encrypted, or as screenshot-proof.

**Why:** Owner request. The naming and storage rules keep the description honest (requirements §10) and stop the mode from becoming a way to avoid moderation.
**Details:** D-047.

### D-032 — Search time limit

**Date:** 2026-10-27
**Decision:** A search that finds no partner stops after 2 minutes and the user is told, with an option to try again. The limit is an admin runtime setting (D-035).
**Why:** Owner decision. Users who have walked away are not matched later with someone who then talks to nobody.

### D-033 — Avoiding immediate repeat matches

**Date:** 2026-10-27
**Decision:** Two users who chatted within the last 5 minutes are not matched again while another suitable user is waiting. If nobody else is available, they can be matched. The window is an admin runtime setting, and 0 turns the rule off (D-035).
**Why:** Owner decision, following requirements §8 ("when suitable alternatives exist").

### D-034 — Roles stored in the database

**Date:** 2026-10-27
**Decision:**

* Each account has a role: `user` (default), `moderator`, or `admin`.
* Roles are changed only by a server-side command (`apps/admin_cli.py`), never from inside the bot. Each change is written to the admin audit log.
* Deleting an account resets its role to `user`, recorded in the audit log with the account itself as the actor. This adds to what D-016 and D-025 erase: a returning account has no special access until a role is granted again. Deletion is the only change outside the CLI, and it can only lower a role.
* Moderators have no tools yet; their screens come with moderation work.

**Why:** Owner decision, after comparing this with a list of admin Telegram IDs in configuration. Roles allow least-privilege access for moderators who review chats (requirements §19), take effect without a restart, and are reused by the planned web admin panel.

### D-035 — Runtime settings changed by admins

**Date:** 2026-10-27
**Decision:**

* Values that operators need to tune are runtime settings stored in PostgreSQL, changed without a restart. Each has a default and an allowed range, and every change is audit-logged with the old and new value.
* Admins change them from 🛠 Admin settings, a button inside their own ⚙️ Advanced settings. The bot re-checks the admin role for every action.
* Initial settings: repeat-match avoidance 300 seconds (0–86,400, 0 is off), search time limit 120 seconds (30–1,800), idle notice after 300 seconds (60–86,400), idle chat ends 60 seconds after the notice (10–3,600), chat message retention 7 days (1–365). Added later: reports per user per 24 hours, 10 (1–100) (D-045).
* A web admin panel with these settings and moderation tools (such as reviewing reported chats) is planned for the moderation work.

**Why:** Owner decision: these values should be adjustable by whoever runs the bot, from the start.

### D-036 — Relayed messages are protected content

**Date:** 2026-10-27
**Decision:** Every relayed message is sent with Telegram's `protect_content`, which stops forwarding and saving in Telegram apps that honour it. User-facing text does not promise screenshot protection; what each Telegram app blocks is to be checked against current Telegram documentation before that text is written.
**Why:** Owner request for the strictest protection available. A bot cannot control screenshots or on-screen tools beyond what Telegram apps do for protected content.

### D-037 — Report reasons

**Date:** 2026-10-27
**Decision:** Reports use six reasons: Spam or scam, Harassment or hate, Sexual content without consent, Appears under 18, Threats or violence, and Other. Optional context text of up to 500 characters may be added. "Appears under 18" is marked urgent.
**Why:** Owner decision.

### D-038 — Replies and edits are mirrored

**Date:** 2026-10-27
**Decision:** A reply is shown as a reply to the matching message on the partner's side, and editing a text or caption updates the partner's copy. Deletions cannot be mirrored, because Telegram does not tell bots when a user deletes a message.
**Why:** Owner decision.

### D-039 — Idle conversations end automatically

**Date:** 2026-10-27
**Decision:** After 5 minutes with no messages, both users are told that the chat will end in 60 seconds unless someone writes. If nobody does, the chat ends as if ended by a user. Both times are admin runtime settings (D-035).
**Why:** Owner decision. Users are not left in a conversation with someone who has gone.

### D-040 — The chat keyboard grows with its features

**Date:** 2026-10-28
**Decision:** Following D-022's rule that a button appears only when it works, the chat keyboard (D-030) starts with 👤 Partner profile and a basic ⏹ End chat, which ends the chat for both users, tells both, and brings back the main keyboard. The full "chat ended" message (Find new, Block, Report) and the idle timeout come with the chat-ending work; 🔒 Private mode joins the first row when it is built. Deleting an account also ends any chat it is in, and the partner is told.
**Why:** Owner decision. Every button shown works, and nobody is left stuck in a chat.

### D-041 — A single matcher in the worker pairs searchers

**Date:** 2026-10-28
**Decision:** The bot starts and cancels searches in the Redis queue (D-027); it never pairs users. A matcher in the worker process runs about once a second: it ends searches past the time limit, re-checks the waiting users in PostgreSQL, pairs them oldest first while avoiding recent partners (D-033), commits each match, and only then tells both users. A Redis lock normally lets only one matcher run at a time.
**Why:** Owner decision. With one matcher there is no race between simultaneous taps; the cost is up to about a second before a match is announced.
**Consequence:** Matching needs the worker running alongside the bot. If Redis loses its data, searches in progress are lost without a "no one found" notice; users can simply search again.

### D-042 — What can't be sent in a chat

**Date:** 2026-10-28
**Decision:**

* **Refused:** contact cards and locations (including venues), and text or captions containing a link, a phone number, or an @username. That includes links and user mentions hidden in formatting, and messages carrying link buttons.
* **When refused:** the sender is told why, and nothing is relayed or stored. An edit that adds one of these is not mirrored.
* **How it is checked:** the profile's link and @username rules plus a phone-number heuristic (seven or more digits in a number-like run). They run on a normalized copy of the text: full-width forms folded, combining and invisible marks removed, and common Cyrillic and Greek look-alikes folded to Latin.
* **Scope:** text inside images and files is not checked. The draft terms describe exactly this scope.
**Why:** Owner decision: chats stay anonymous, so neither partner reveals how to reach them, by mistake or under pressure. Profile location for discovery is separate and will show only country and city.
**Consequence:** The phone heuristic can also refuse long runs of numbers that are not phone numbers.

### D-043 — Edited messages keep their original text

**Date:** 2026-10-28
**Decision:** When a relayed message is edited, the original text is kept and the latest edit is stored beside it with the time of the edit. Both are deleted with the rest of the message content after the retention period (D-028).
**Why:** Owner decision. Someone cannot edit away an abusive message before a report about it is reviewed.

### D-044 — The new-chat menu, and what happens when a chat ends

**Date:** 2026-10-29
**Decision:**

* 🎭 New Anonymous Chat opens a "Who do you want to meet?" message with a button for each kind of chat. For now there is one, 🎲 Random chat · Free. The paid choices (chat with men, women, or other genders, with their prices) join it with the coin work.
* However a chat ends (a user ends it, the partner leaves RealChat, the partner can't be reached, or it is idle), each user is told how it ended, the main keyboard comes back, and the same menu is shown again. The next chat is one tap away, and the bot never starts a search on its own.
* A search that finds no one also shows the menu again.
* This supersedes the "Find new" button planned in D-040. Block and Report are offered after a chat once blocking and reporting exist.
* Wording and layout are provisional until the design pass.

**Why:** Owner decision. One entry point for every kind of chat, including the paid ones to come.

### D-045 — Blocking and reporting chat partners

**Date:** 2026-10-29
**Decision:**

* 🚫 Block and ⚠️ Report are inline buttons on the partner's profile card and on an "Anything wrong with this chat?" message sent after every chat ending, between the "how it ended" message and the new-chat menu. The layout is provisional until the design pass.
* **Block** is one button that toggles with ✅ Unblock. Blocking someone you are still chatting with asks for confirmation ("Blocking will end this chat"), then ends the chat. Outside that chat, blocking and unblocking each take one tap.
* **Blocks do not affect random matching.** Blocked pairs can be matched again by search. A blocked user cannot send direct messages or chat requests to the person who blocked them, once those features exist. This amends requirements §8.
* **Report** asks for a reason (D-037) and files the report at once. Filing also blocks the reported user and ends the chat if it is still open. Optional details (up to 500 characters) can then be added, or skipped. Details are not asked for if the reporter is already in a chat with someone else or searching for one, so messages meant for that chat are never captured as report details.
* Each user can report a conversation once. Reports are limited per user per 24 hours by an admin runtime setting (default 10, range 1–100). Blocking is never limited.
* The other user is only ever told that the chat ended, never that they were blocked or reported.
* Only the other participant of a conversation the user was in can be blocked or reported. *Amended by D-054:* someone the user met through a profile visit, either way round, can be too.
* Blocks (with their history of blocking and unblocking) and reports are kept when either account is deleted.
* The draft terms describe this (terms version `draft-7`).

**Why:** Owner decisions. Excluding blocked pairs from matching would let a user rig free matches, for example by blocking everyone of one gender. Not revealing blocks and reports avoids retaliation. Keeping them after deletion stops deletion being used to undo a block or erase a report.
**Consequence:** Until direct messages and chat requests exist, a block made outside a chat is stored and shown (as ✅ Unblock) but has no other effect. Someone who blocked a partner can be matched with them again, and then leaves with ⏹ End chat.

---

### D-046 — Expired chat messages are deleted entirely, once a day

**Date:** 2026-10-30
**Decision:**

* A worker job runs once a day and deletes every stored chat message older than the retention period (D-028, D-035). The whole message record goes: text, edits, file identifier, sender, and time. Messages in a conversation with an open report are kept until the report is closed.
* Conversations, their participants, blocks, reports, and audit records are kept; they have their own retention (requirements §20).
* In a chat still open after the retention period, replies to the deleted messages arrive as plain messages and edits to them are not passed on.
* The draft terms say messages are deleted "after a set period" (terms version `draft-8`). Legal and policy texts do not give numbers for periods admins can change, so the text stays true when a setting changes.
* Deleting stored messages does not remove them from users' Telegram chats.

**Why:** Owner decisions. Deleting the whole record keeps the least data: no per-message record of sender, time, or content remains. Conversations and their participants are still kept, as the record of which chats happened. Leaving numbers out of the terms keeps them accurate whatever the setting.
**Consequence:** Because the job normally runs daily, a message can stay up to a day past the retention period. It runs in its own loop in the worker, so matching and idle chats never wait for a cleanup to finish; they still share the database, so a very large cleanup can slow them somewhat. It deletes in batches of 1,000 per transaction, skips messages being written at that moment (they go on the next run), and logs only counts. A failed or interrupted run is retried within the hour, and a long run keeps its claim renewed so a second run does not start alongside it.

### D-047 — How Private mode works

**Date:** 2026-10-30
**Decision:** (details of D-031)

* 🔒 Private mode is on the chat keyboard's first row, next to 👤 Partner profile; while it is on, the button reads 🔓 Private mode off. Either user turns it on or off, and both are told, with the note required by D-031.
* Every message sent while it is on is relayed normally, including dice and games, except photos, videos, voice messages, and video messages, which are sent as view-once media (D-048). When it is turned off, when the chat ends in any way, or when it reaches its time limit, both copies of each of those messages (the sender's original and the partner's copy) are deleted from Telegram, as far as Telegram allows. For view-once media this means unopened placeholders and their originals; media already opened is deleted at the end of its viewing time instead (D-048).
* The time limit is an admin runtime setting: 24 hours by default, 1–46 hours. When it is reached, both users are told and can turn Private mode on again.
* Telegram lets bots delete messages for 48 hours, and does not let them delete a dice message in a private chat until it is more than 24 hours old (Bot API documentation, confirmed by a test on the development bot on 2026-10-30). Dice and games are therefore deleted automatically once they are past 24 hours, and users are told this when Private mode ends.
* Deletions are queued in the database and sent by the worker, up to 100 per chat per call. An entry is removed when the message is deleted, is already gone, can't be deleted, or passes the 48-hour limit. Only counts are logged.
* Messages refused under the chat rules (D-042), and the bot's own notices, are not deleted.
* The note says screenshots, screen recording, saving, and forwarding are blocked in Telegram's own apps (all relayed messages are protected content, D-036), and that nothing can stop someone photographing the screen with another device. This wording is provisional: the owner will provide reference wording during the design pass, to be checked against Telegram's documentation as D-036 requires.
* View-once media in Private mode: D-048.
* Wording and layout are provisional until the design pass.

**Why:** Owner decisions. Dice stay relayed normally rather than refused or changed, and are removed as soon as Telegram allows.
**Consequence:** Dice sent in Private mode stay visible for about a day after it ends. Deletion is not instant: it usually takes a few seconds after Private mode ends.

### D-048 — View-once media in Private mode

**Date:** 2026-10-31
**Decision:**

* While Private mode is on, photos, videos, voice messages, and video messages are view-once. Other kinds (text, stickers, GIFs, files) are relayed as before. Normal chats are unchanged for now.
* The partner receives a placeholder ("📷 Photo · tap to view. It disappears 20 seconds after opening.") with a 👁 View button. On tap, the bot sends the media as protected content, with its caption, and the placeholder says it was opened.
* The opened copy, the placeholder, and the sender's original are deleted after the viewing time: the view timer (an admin runtime setting, default 20 seconds, 10–300) plus the length of a video, voice, or video message. It is fixed when the media is sent, so the placeholder's promise holds even if the setting changes.
* Each item opens once, only for the partner. A tap after Private mode or the chat ended is refused, and unopened placeholders and originals are deleted with the rest of the session (D-047). Media opened before Private mode ends keeps its full viewing time.
* Opening queues the placeholder and the original at once; the opened copy is queued once it is sent. If Telegram asks the bot to wait at that moment, the opening is undone so it can be tapped again.
* Replies to the opened copy point at the sender's original, and a caption edit after opening is shown on the opened copy.
* A caption edit before opening is stored (D-043) but not sent to the placeholder; opening shows the edited caption.
* Media is still stored for moderation with normal retention. The Private mode note says media can be opened once.

**Why:** Owner decisions, including that opened media keeps its viewing time when Private mode ends. The Bot API has no view-once or self-destruct option and does not tell bots when a message is opened (Bot API documentation, checked 2026-10-30). Telegram does not offer its own view-once option in chats with bots, and a photo sent to a bot arrives as an ordinary photo (checked on the development bot). A button tap is the one signal a bot receives, so tap-to-view starts the timer at the moment of opening.
**Consequence:** The partner sees a placeholder rather than a preview. If the sender deletes their original before it is opened, it cannot be opened. If the bot stops between sending the opened copy and recording it, that copy is not deleted automatically. Wording and layout are provisional until the design pass.

### D-049 — Asking before sending media right after Private mode ends

**Date:** 2026-10-31
**Decision:**

* A photo, video, voice message, or video message sent within a short window after Private mode ended is held, not relayed, when the partner turned it off or it reached its time limit. The sender is asked whether to send it as a normal message, with [Send] and [Don't send]. A user who turned Private mode off themselves is never asked.
* The window is an admin runtime setting, default 30 seconds (0–300, 0 turns it off).
* Send relays it as a normal message; Don't send relays nothing, and the sender's own copy stays in their chat. Each question is answered once. It lapses after 10 minutes, when the chat ends, or when Private mode has been turned on again since (even if it was then turned off). Other messages sent meanwhile do not affect it.
* Send needs the current terms, as any message does. An edit to the held message's caption is checked against the chat rules (D-042): one that breaks them cancels the held message, since Send copies the message as it now is.
* The decision to hold, and the check that an answer still applies, are made under the conversation lock that turning Private mode on or off takes, so they cannot be overtaken.
* Only the held message's details (ids, kind, caption) are kept, in a Redis entry that expires after 10 minutes; nothing is recorded in the database until it is sent.
* Text and other kinds are relayed as before.

**Why:** Owner decision. A user who turned Private mode on may send a photo without noticing that their partner just turned it off, and would otherwise have it delivered as a normal photo.

### D-050 — Coins and paid matching

**Date:** 2026-11-04
**Decision:**

* **Ledger:** every change to a user's coins is an append-only ledger entry (database triggers reject updates, deletes, and truncation; corrections are compensating entries) with a unique idempotency key. A balance is the sum of the user's entries. No negative balances.
* **Holds:** coins for a paid search are set aside when it starts, taken only when the match is committed, and released at once on cancel, time limit, or failure.
* **Paid matching:** "Who do you want to meet?" offers men, women, or other genders for one price (admin runtime setting, default 2 coins), shown on each button. Without enough available coins, the user is told and pointed to /coins. The payer's match message says how many coins were used; cancelling or a search that finds no one says the coins were returned. If the held coins cannot be taken when a match is made, the match is not made. A paid searcher can be matched with anyone whose gender fits, including free random searchers, provided each fits the other's own choice. Only those who chose a gender pay; if both chose, both pay. The partner is never told who paid. Settles O-004.
* **Earning, before purchases exist:**
  * Welcome bonus: 10 coins on completing onboarding, once per Telegram account ever.
  * Invite links: the inviter gets 10 coins when an invited person who has never used the bot completes onboarding. The new user gets 10 once, as their welcome bonus, never both. An inviter is rewarded for at most 10 invites per day. "Never used the bot" means never completed a profile and never deleted an account; someone who only opened the bot before still counts. The first inviter stays. The inviter is told, without names, when an eligible new user starts the bot through their link (invalid links, self-invites, and existing or deleted accounts are ignored without a notice), and again if the reward is earned. The reward is withheld when the inviter's account is not active, the daily cap is reached, or the invited user has ever deleted an account.
  * Daily reward: for promotional periods, off by default; admins switch it on or off and set the amount (default 1 coin), as runtime settings. While it is on, /coins offers a 🎁 Claim button to users who have completed signing up and accepted the current terms, once per calendar day (UTC); the amount is the one set at claim time, and an old button does nothing once it is off or already used.
  * Admin grants and corrections through the server-side admin CLI, with a reason, audit-logged.
  * All amounts and caps are admin runtime settings.
* **💰 /coins** in the command menu shows the balance, coins held for a search, and recent activity; the daily reward and invite link join it.
* **Account deletion** keeps coins and the ledger; a returning user has the same balance, and bonuses are never given twice. *Superseded by D-055:* deletion forfeits the coins.
* The draft terms say coins are virtual, with no cash value, and cannot be exchanged or transferred (terms version `draft-9`).

**Why:** Owner decisions (requirements §9, §16). Holds keep coins from being spent twice once more paid features exist; keeping coins on deletion means nothing is lost by mistake and nothing is gained by rejoining.

### D-051 — People: past chat partners, and RealChat activity

**Date:** 2026-11-05
**Decision:**

* Until location-based discovery exists, the people a user can reach again are their past chat partners: anyone they shared a conversation with that ended within a window. The window is an admin runtime setting, default 10 days (1–30). Later features (visits, likes, direct messages, chat requests) build on this list.
* 👥 People is a button on the user's own profile card. It opens 💬 Past chats: each partner once, by their latest chat, latest first, in pages of 10. Tapping one shows their profile card with Block (or Unblock) and Report for that chat (D-045). The layout is provisional until the design pass.
* Only partners who can still use RealChat are listed: an active account with a finished profile. Chats from before either user deleted their account are never counted, so a returning account starts afresh. Someone the user is chatting with again is left out until that chat ends.
* Blocks hide nobody: both users still see each other, and the blocker sees 🚫 beside the name. What a blocked user cannot do (direct messages, chat requests) is enforced by those features.
* RealChat activity: each active user's last use of the bot in a private chat is recorded (at most once a minute). Suspended and banned users, who are stopped by the access gate, and group-chat updates are not counted. It is never presented as Telegram online status, which bots cannot see (requirements §15).
* The draft terms say past chat partners can see the profile card for a while after the chat (terms version `draft-10`).

**Why:** Owner decisions. Browsing strangers waits for discovery; a short window limits unwanted contact after an anonymous chat, while keeping blocked people visible lets the blocker unblock and keeps the block from being obvious.

### D-052 — Notification settings

**Date:** 2026-11-06
**Decision:**

* Advanced settings (on the profile card) holds the notification settings, each a one-tap button that shows its current value:
  * 🤫 **Silent mode**, off by default: nobody can send the user chat requests. Direct messages still arrive.
  * 👀 **Visit alerts** and ❤️ **Like alerts**, on by default: notices when someone visits or likes the profile.
  * ⏰ **Reminders**, on by default: general reminders, such as the daily reward while it is offered. Daily reward reminders are sent from D-060.
  * 🔔 **Activity alerts about me**, on by default: whether others can buy activity alerts about the user (added by D-059).
* Advanced settings also keeps Delete account (and Admin settings for admins), and gains ◀️ Back to the profile card. VIP options join it with the VIP work. More categories may be split out before launch.
* Each setting is enforced by the feature it controls, as those features ship.
* Account deletion resets the settings to their defaults. Suspended and banned users cannot change them.

**Why:** Owner decisions (requirements §15). Reminders are on by default so that they reach users.

### D-053 — Profile visits

**Date:** 2026-11-06
**Decision:**

* Opening someone's profile card from People (Past chats, Visited you, or a visit notice) records a visit. Opening 👤 Partner profile during a chat records one too.
* 👀 **Visited you** in People lists each visitor once, ordered by their latest visit, with no visit counts. Tapping one opens their card with Block (or Unblock) and Report; that is a visit to them in turn.
* Every visit from People sends the visited user a notice ("‹nickname› viewed your profile", with a button to the visitor's card). No notice is sent while the two are chatting together, or when the visited user turned visit alerts off (D-052). *Amended by D-056:* a visit from someone the visited user blocked is announced too; it was previously silent.
* A visit is kept for 30 days after the latest one (admin runtime setting, 1–365), then the worker deletes it. Visitors who can no longer use RealChat are not listed. A blocked visitor stays listed and is marked for the blocker. Account deletion erases the account's visits, made and received.
* Visits are free.
* The draft terms say that visiting a profile card can be seen and notified (terms version `draft-11`).

**Why:** Owner decisions (requirements §13). One entry per visitor keeps the list readable and visit counts from being inflated. A notice on every visit can be repeated by reopening a card; the per-user flood limit caps the rate, and turning visit alerts off stops it. (Blocking the visitor also stopped it until D-056, which announces visits from blocked users too.)

### D-054 — RealChat IDs and /userid

**Date:** 2026-11-06
**Decision:**

* Every account has a **RealChat ID**: 7 random characters from lowercase letters and digits, without look-alikes (`abcdefghjkmnpqrstuvwxyz23456789`). It is not the Telegram ID or the internal account number, follows no pattern, and is unique. Existing accounts were given one when the feature shipped.
* The ID never changes while the account exists. Deleting the account retires it with the rest of the profile: the old ID finds nobody, and a returning user gets a new one. A retired ID is not tracked, so a later random draw could reissue it; with about 27 billion possible IDs, this is negligible.
* Every profile card, including the user's own, shows the ID as a tappable `/userid_<id>`. `/userid <id>` does the same, ignoring letter case, and `/userid` alone shows the user's own ID. `/userid` is in the command menu.
* Anyone who has finished onboarding can open a card by ID. It is a visit with the usual notice rules (D-053). The user's own ID opens their own card, without a visit.
* Unknown IDs and accounts that can't be opened (deleted, suspended, banned, or unfinished) all get "No profile with that ID", so a lookup reveals nothing about them.
* Wrong lookups are limited per user: after 10 in an hour (admin runtime setting, 1–100), counted from the first, every lookup is refused until the hour ends, even for a correct ID. The user's own ID is not limited. If the limit can't be checked (Redis unavailable), lookups are refused.
* A person met through a visit, either way round, within the visits window, can be blocked and reported from their card without a shared chat (amends D-045). If the two have chatted since the card was sent, its buttons act on that chat, so an open chat is ended as usual. When the two did chat (after both accounts' latest deletion), Block and Report act on the latest chat, as before. Otherwise a report is about the profile: it has no chat messages, can be filed once per reporter per person, counts toward the daily report limit, and also blocks. A profile block or report ends no chat.
* Visits are the record of having met by ID, so later features that reach visitors (direct messages, chat requests) also reach people met this way.
* The draft terms say the ID is visible, lets anyone open the card, and is replaced after deletion (terms version `draft-12`).

**Why:** Owner decisions. A visible ID lets users find and share profiles and, in the design pass, can replace some buttons. Making it random and retiring it on deletion keeps it from revealing the user count or following a user into a fresh account.

### D-055 — Account deletion starts completely fresh

**Date:** 2026-11-07
**Decision:**

* Deleting an account forfeits its coins. Coins on hold are released first, then one ledger entry (`account_deleted`) removes the whole balance. Nothing is recorded for a zero balance. The ledger itself is never changed, so the history stays complete (D-016). This supersedes the coin rule in D-050.
* The invite code is retired: old invite links credit nobody, and a returning user gets a new code. Invites still pending (people who joined through the link but haven't finished their profile) are settled as not rewarded.
* One-time rewards already given, such as the welcome bonus, are not given again, and a returning user can never be rewarded as someone's invitee (unchanged from D-050).
* A returning user's `/coins` shows only activity after their latest deletion, cut at the account's last ledger entry at deletion rather than by time. Older entries stay in the ledger for records and admins.
* The confirmation lists what will be lost, including the current balance. Suspended and banned users who delete from their notice lose their coins the same way.
* Every coin ledger write (spending, holds, and credits, including admin grants), following an invite, and completing a profile wait for a deletion in progress. A write either lands before it, and is forfeited, or after it, and is listed for the returning account. The daily reward is only granted to an active, onboarded account.
* The draft terms say that deletion removes coins and the invite link (terms version `draft-13`).

**Why:** Owner decision: a returning user should start completely fresh. Purchased coins don't exist yet; how forfeiting them on deletion is presented must be part of the legal review before payments ship (O-007).

### D-056 — Profile likes

**Date:** 2026-11-07
**Decision:**

* Every other user's card has ❤️ **Like** above Block and Report: in People (Past chats, Visited you, and the like lists), from /userid, and 👤 Partner profile during a chat. Once liked, the button becomes 💔 **Unlike**. Unliking is silent. The user's own card has none.
* A like needs both accounts active with a finished profile. A user can't like someone they blocked; the button stays on the card and the tap is refused with a hint to unblock. A like given before blocking stays and can still be removed.
* Each pair has at most one like. Repeated or concurrent taps never add a second like or a second notice.
* A new like tells the liked user ("‹nickname› liked your profile", with a button to the liker's card), unless they turned like alerts off (D-052). A like from the current chat partner says "Your chat partner liked your profile" instead, with no button, since the partner's card is on the chat keyboard.
* At most one like notice per pair within a gap: an admin runtime setting, default 1 hour (1–720 hours). Unliking and liking again within the gap works, but sends no new notice. A like made while like alerts were off doesn't count toward the gap.
* Blocks hide nobody, as for visits: blocked people stay in both lists, marked 🚫 for the blocker. A like from someone the liked user blocked still sends its notice, and so does a visit (amends D-053).
* People gains ❤️ **Liked you** and 💘 **You liked**: current likes, latest first, in pages of 10, without counts. Only accounts that can still use RealChat are listed. Opening a card from them is a visit, with the usual notice (D-053), while the like stands.
* A like lasts until it is removed or either account is deleted; it does not expire. Account deletion erases the likes the account made and received.
* Likes and the lists are free.
* The draft terms say that likes can be seen and notified, and that blocking doesn't stop visit and like notices (terms version `draft-14`).

**Why:** Owner decisions (requirements §13). A like is a deliberate choice, so it lasts until removed. Unliking and liking again is still possible at any time; the gap only keeps it from repeating the notice. Notices from blocked people were an explicit owner choice; turning the alerts off still stops them.

### D-057 — Direct messages

**Date:** 2026-11-07
**Decision:**

* Every other user's card has ✉️ **Message · ‹price›** under Like, for anyone whose card the user can open (Past chats, Visited you, the like lists, /userid). It is not offered to the current chat partner, and is hidden from someone the card's owner blocked.
* Tapping it asks for the message, showing the price. The message is checked against the chat rules (D-042) and shown back as a preview, exactly as the recipient will see it, with ✅ Send and Cancel. Sending another message replaces the preview. Text, photos, videos, animations, audio, voice messages, video messages, stickers, and files can be sent. (Only text and photos have been checked end to end so far; the other kinds share the same sending code and need a check against Telegram before launch.) Polls and dice are refused, because their content can't be kept and shown again unchanged.
* The price is an admin runtime setting, default 1 coin (1–100). On Send it is held. The recipient gets "✉️ Message from ‹nickname› · /userid_‹id›" with 📩 Open. The coin is taken once Telegram accepts that notice; if Telegram refuses it, the hold is released, the reason is recorded, and the sender is told no coin was taken. A repeated Send sends and charges nothing more; to try again after a failure, the sender writes the message again. If sending is interrupted (the bot stops before settling), the worker fails the message after a few minutes and releases the hold, without charging, even if the notice did arrive.
* The content is stored, so Open shows it whatever the sender does with their copy. It is sent protected, so Telegram apps that honour that setting block forwarding and saving it. The first Open tells the sender "‹nickname› opened your message", once. Telegram doesn't report reading; opening the message is the only signal used.
* A paid message offers the recipient ↩️ **Reply (free)** once. The reply goes through the same preview, notice, and Open, at no cost, and has no free reply of its own.
* Nobody can message, or freely reply to, someone who blocked them or whom they blocked; the blocker is asked to unblock first. Refused attempts take no coin. Messages already delivered still open. Silent mode does not stop direct messages (D-052). Block and Report stay on the profile card, which the notice's /userid link opens.
* Direct messages are kept for the chat message retention period (D-046), counted from sending, then deleted by the daily cleanup, even while a report about the sender is open: reports are about the profile and never attach a direct message, so moderators don't see its content. After that they can't be opened or replied to; the charge stays in the ledger. Account deletion erases the messages an account sent and received.
* The draft terms describe direct messages: the nickname and ID shown, the opened notice, storage, and blocks (terms version `draft-15`).

**Why:** Owner decisions (requirements §11). Charging on the notice Telegram confirms is the only delivery the platform can verify. A preview lets the sender check the message before paying; keeping the content makes Open reliable; one free reply lets the recipient answer without making the exchange free.

### D-058 — Chat requests

**Date:** 2026-11-08
**Decision:**

* Every other user's card has 💬 **Chat request · ‹price›** under Message, on the same cards and with the same exceptions (not the current chat partner; hidden from someone the card's owner blocked). It is shown whatever the person is doing; whether they can take a request is only checked on the tap, so a card never reveals activity, chats, or Silent mode.
* A request can be sent when the recipient used RealChat within the last 10 minutes (admin runtime setting, 1–60), is not in a chat, and is not in Silent mode (D-052); the sender is not in a chat or searching; and neither has blocked the other. A refusal says only that the person can't take a request right now, and charges nothing. A sender may have several requests waiting, but only one per recipient.
* The tap shows the price (2 coins, admin runtime setting, 1–100) and that it is taken once the request is delivered and never returned, with Send and Cancel. On Send the price is held and the recipient gets "‹nickname› · /userid_‹id› wants to chat with you" with ✅ Accept and Decline. The price is taken once Telegram accepts that notice; if Telegram refuses it, the hold is released and the sender is told. A repeated Send sends and charges nothing more; Send is refused if the price changed since it was shown. An interrupted send is failed by the worker after a few minutes, uncharged, and the sender is told. An Accept or Decline tapped while the delivery is still being recorded asks to try again.
* **No refund once delivered** (amends requirements §12): not for a decline, an expiry, or a sender who doesn't switch.
* The recipient has 60 minutes (admin runtime setting, 5–1440) to answer. Decline tells the sender; an unanswered request expires and the sender is told.
* Accept by a recipient who is chatting or searching asks them to confirm that it ends their chat or search. A free sender is connected at once. A sender who is chatting or searching is asked to 🔀 Switch or Stay, and the recipient sees that they're waiting; Stay, or no answer within 90 seconds (admin runtime setting, 30–600), lapses the request and tells the recipient.
* Connecting ends any chat either user is in (the partner left behind is told, as for any ending) and their searches (a paid search's held coins are returned), creates the conversation, and announces it to both with the chat keyboard. Both must still be able to chat (active, onboarded, current terms, no block either way); otherwise the request closes and its sender is told. When one of a sender's requests connects, their other waiting requests close: one still being sent is never charged, and a recipient who was waiting for the sender to switch is told.
* Each request is accepted at most once and creates at most one conversation; every step is a conditional update, and the conversation is created under the same unique index on open chats as matching. If one of them was matched meanwhile, nothing changes and they're told.
* Account deletion erases the requests an account sent and received, releasing the holds of those still pending.
* The draft terms describe chat requests and their no-refund rule (terms version `draft-16`).

**Why:** Owner decisions (requirements §12, amended). Checking availability only on the tap keeps RealChat activity private. Charging on delivery matches what the platform can verify; the no-refund rule for delivered requests is the owner's. Several waiting requests let a sender reach more than one person, with the first acceptance winning.

### D-059 — Activity alerts

**Date:** 2026-11-09
**Decision:**

* Every other user's card has 🔔 **Alert when active · ‹price›** under Chat request, on the same cards and with the same exceptions. It buys one alert the next time the person uses RealChat (any use of the bot in a private chat, recorded at most once a minute as their RealChat activity, D-051; group updates don't count), within 7 days (admin runtime setting, 1–30), for 1 coin (admin runtime setting, 1–100). The confirmation says exactly that, says the coin is returned if they don't, and that this is RealChat activity, not Telegram online status. Settles O-003.
* The coin is taken on Buy; a repeated Buy charges nothing more, and Buy is refused if the price changed since it was shown. It can be bought at any time, including while the person is using RealChat, when the person is active and onboarded, neither has blocked the other, and the person allows activity alerts; one active alert per buyer per person. A refusal takes nothing.
* The alert fires once, the first time the person uses RealChat after the purchase and within the period (even if the worker only sees it after the period ended): "‹nickname› is using RealChat now · /userid_‹id›", labelled as RealChat activity. The worker checks every few seconds, so an alert can come up to about a minute after the activity.
* The coin is returned, and the buyer told, if the alert can't fire: no activity within the period, a block either way, the person turning alerts off, or their account being suspended, banned, deleted, or deleted and started again (a returning account is someone new). The buyer is told only an expiry's reason; a block or an opt-out isn't revealed. An alert is never both fired and refunded.
* While an alert is set, the card shows 🔔 **Alert set · Cancel**; cancelling ends it without a refund.
* Advanced settings gains 🔔 **Activity alerts about me**, on by default (amends D-052). Turned off, nobody can buy an alert about the user, and pending ones are refunded. The person is never told who set an alert.
* Account deletion erases the alerts the account bought (its coins are forfeited, D-055); alerts about the account are refunded to their buyers.
* The draft terms describe activity alerts and the opt-out (terms version `draft-17`).

**Why:** Owner decisions (requirements §15). Telegram doesn't let bots see online status, so the alert is defined by RealChat activity and labelled so. One alert per coin with a refund keeps the purchase clear; the opt-out and blocks are the privacy and anti-harassment controls §15 asks for.

### D-060 — Daily reward reminders

**Date:** 2026-11-09
**Decision:**

* While the daily reward is on (D-050), the worker reminds users who have ⏰ Reminders on (D-052) and could claim it (an active account, onboarding finished, the current terms accepted) but haven't claimed it this UTC day: "🎁 Your daily reward is waiting: ‹amount›." with 🎁 Claim (the same claim as in /coins) and 🔕 Turn off reminders. Everyone with Reminders on is reminded, however long since they last used RealChat.
* **Timing follows the user's rhythm first:** the time of day of their latest daily reward claim since their latest account deletion. Without one, a local hour (admin runtime setting, default 18:00) in their country's main time zone, from the `tzdata` country table (a short list names the main zone of large multi-zone countries; otherwise the first listed). This is an approximation for multi-zone countries.
* At most one reminder per user per UTC day, and at least 20 hours between two, so a local evening that spans midnight UTC never gets two. The reminder is recorded before it is sent, after the user's account is locked and every condition checked again (so a claim or an account deletion made meanwhile wins), and a restart or a second worker never sends it twice. Candidates are read in pages.
* A user whose last reminder couldn't reach them (they blocked the bot) is skipped until they use RealChat again. Reminders go out in batches of up to 100 a minute, paced well under Telegram's limit.
* Reminder records are kept for 30 days. Account deletion erases them.
* The draft terms mention reminders and how to turn them off (terms version `draft-18`).

**Why:** Owner decisions. The user's own claim time is the best signal of when they come by; their country's evening is the fallback. Activity-based and promotional reminders are out of scope (planned with discovery and announcements).

### D-061 — Buying coins with Telegram Stars

**Date:** 2026-11-16
**Decision:**

* Coins and VIP are digital goods, so they are sold only in Telegram Stars (currency `XTR`), as Telegram requires. Prices are shown in Stars only: what a Star costs varies by platform, country, and tax, and bots get no exchange rate.
* Four coin packs, each with its coins and Stars price as admin runtime settings (D-035): 150 coins for 80 Stars, 350 for 150, 800 for 290, and 1,900 for 580 by default. A pack with 0 coins is hidden. /coins lists them under "🛒 Buy coins with Telegram Stars".
* A pack button stores an invoice row (who, what it buys, its price) and sends a Stars invoice whose payload is only that row's reference (`inv:<id>`). The invoice is honoured at what it showed for 1 hour, even if admins change the pack meanwhile. Forwarded invoices open the bot instead of offering someone else a Pay button.
* Telegram asks before taking the Stars (pre-checkout, answered within its 10-second limit). It is approved only for an invoice of this user's, made after their latest account deletion, under 1 hour old, with the stored amount in Stars, while the account is active with the current terms accepted. Otherwise it is declined with a reason Telegram shows the user.
* Coins are credited only on Telegram's payment confirmation. The payment is recorded once per Telegram charge id (unique) and the coins are a `coin_purchase` ledger entry keyed by the payment, in the same transaction, after the account row is locked. A repeated confirmation changes nothing.
* A payment confirmed after the account was suspended or banned is still credited; the coins stay with the account like any other balance. One confirmed after the account was deleted is refunded (`refundStarPayment`): the bot tries at once, and if Telegram refuses or can't be reached, the payment stays owed and the worker tries again every few minutes until Telegram accepts the refund (Telegram doesn't send a confirmation again once the bot has received it). Telegram's answer that the charge was already refunded is recorded as refunded.
* Payment rows are guarded in the database like the coin ledger: they can't be deleted or truncated, the charge, buyer, invoice, amount, and time never change, and the status only moves forward: paid to refund owed to refunded, or paid straight to refunded (for refunds made by admins later).
* Known gap: if recording a confirmation fails (for example, the database is unavailable), Telegram doesn't send it again, so the payment isn't recorded or credited until it is reconciled against Telegram's transaction list. Such a case shows in the error log and is found by the admin `reconcile` command (D-066). An admin records it with `record-payment` (D-068); one that matches none of our invoices needs manual handling.
* Payment updates are never dropped by flood control or the restricted-account gate: an unanswered pre-checkout cancels the user's payment, and a confirmation means the Stars were taken.
* For testing, `TELEGRAM_TEST_ENVIRONMENT` connects the bot to Telegram's test environment, where Stars payments cost nothing; it is refused in production.
* /terms shows the terms, and /paysupport points to RealChat support and says Telegram's support can't help with purchases; both are in the command menu and available to restricted users. `SUPPORT_CONTACT` is required in production. The draft terms cover buying coins (terms version `draft-19`).

**Why:** Owner decisions on pricing (packs give more coins for the same Stars than comparable bots), invoice validity, and payments that race a restriction or deletion. Telegram's rules for digital goods (Bot API payments documentation, checked 2026-11-16). Settles O-005 for coin packs; D-062 settles its VIP part.

### D-062 — The VIP pass

**Date:** 2026-11-17
**Decision:**

* VIP is sold as a one-time pass in Telegram Stars, bought from /vip with the same stored invoice, checkout, and exactly-once fulfilment as coin packs (D-061). It never renews; Telegram's Stars subscriptions are not used. The price, length, and included coins are admin settings: 780 Stars, 180 days, and 3,750 coins by default.
* VIP is stored as `users.vip_until`, and the user is VIP while it is in the future, so it ends at that moment with no job. A pass sets it to the later of now and its current value, plus the invoice's days; the included coins are a `vip_coins` ledger entry. Each payment records the end date it produced.
* Stacking limit: a pass can be bought only while at most one pass's length of VIP is left, so a pass bought then ends at most two passes ahead. The button and checkout both check it, and /vip says from when another pass can be bought. The limit is not checked again when the payment arrives: invoices that all passed checkout before any was paid are all granted, since the Stars were taken, even if that puts VIP more than two passes ahead. That needs several invoices approved within seconds, and each was paid for.
* When VIP ends the user is a normal user again and keeps all their coins, including those that came with the pass.
* A payment that arrives after the account was suspended or banned still grants VIP, and its days keep running during the restriction. A payment for a deleted account is refunded like a coin pack. Deleting an account ends VIP, and the deletion warning says so.
* /vip shows the status and end date, only the perks that work today, the pass, and a Buy (or Extend) button while a pass can be bought, otherwise the date from which one can; /coins shows the status line. Approved perks, each added to /vip when built: a ✅ 👑 VIP badge (requirements §17 amended; the terms will say it isn't identity verification), priority in every search, and Ghost mode for profile visits. Not part of VIP: hiding the profile (O-008), extra view-once views (O-012), and bulk messages.
* The draft terms describe the pass (terms version `draft-20`).

**Why:** Owner decisions on the pass, its price, the stacking limit, how restrictions and deletion apply, and listing only working perks. Settles the VIP part of O-005.

### D-063 — The VIP badge and matching priority

**Date:** 2026-11-18
**Decision:**

* While a user is VIP (D-062), `✅ 👑 VIP` follows their nickname on every profile card (their own, the chat partner's, People, /userid) and in the People lists (past chats, visitors, likes), and `✅` follows it in every notice that names them (direct messages and replies, likes, profile visits, chat requests, activity alerts). VIP is read when the card, list, or notice is made, so anything shown after VIP ends has no mark; messages already sent in Telegram are not edited and keep it.
* The marks show that VIP was bought in RealChat, never a verified identity; /vip and the terms say so (terms version `draft-21`).
* VIP searchers are matched first, in free and gender searches: each tick orders VIPs ahead of everyone else, each group oldest first, and pairs as before. A VIP's partner can be anyone who fits, VIP or not, and a gender search still matches only that gender; blocks, repeat avoidance, and paid holds are unchanged.
* Every waiting VIP is in every tick, also beyond the 200 longest-waiting searches: a VIP's new search is recorded in a second Redis sorted set (`match:vip`), in the same script that starts it, and the matcher reads it each tick. It is only a hint: entries no longer queued are skipped, entries past the search time limit are trimmed, and VIP is re-checked in PostgreSQL before priority is given.
* Someone who becomes VIP during a search gets priority in that search only while it is among the 200 longest-waiting; from their next search, always.

**Why:** Owner decisions on where the marks appear (the full badge on cards and lists, the tick everywhere else), on VIP-first ordering with any fitting partner, and on reaching every waiting VIP each tick.

### D-064 — Ghost mode for profile visits

**Date:** 2026-11-18
**Decision:**

* A VIP user (D-062) has 👻 Ghost mode in Advanced settings; nobody else sees the switch, and the server refuses it unless the user is VIP at that moment. It is stored as `users.ghost_mode`.
* While the user is VIP and Ghost mode is on, opening any profile card records no visit and sends no visit alert: from Past chats, Visited you, Liked you, You liked, `/userid`, and 👤 Partner profile during a chat. A ghost visit is never recorded, so it doesn't appear later either, including after VIP ends. Visits recorded earlier stay as they were.
* Turning Ghost mode off, or VIP ending, makes visits record and notify normally again from then on.
* Each new VIP period starts with Ghost mode on: a VIP payment that starts VIP again turns it on in the same update that sets the end date. A payment that extends running VIP keeps the user's choice. Account deletion resets it.
* The check is in `visits.record_visit`, after both account rows are locked, so every way a card is opened follows it. /vip lists the perk, and the terms say visits are not shown while Ghost mode is on (terms version `draft-22`).

**Why:** Owner decisions to cover the chat partner's card too, and to turn Ghost mode on with every new VIP period.

### D-065 — Refunding Stars purchases

**Date:** 2026-11-19
**Decision:**

* A refund takes back what the purchase gave: its coins, by one `purchase_refund` ledger entry keyed to the payment when there are coins to take, and for a VIP pass its days. VIP is worked out again as if the pass had never been bought: from the user's other passes that stand (since their account was last deleted), each adding its days from the later of its purchase and the end so far. Time already used counts against those passes, and VIP never gets longer. Balances never go below 0, and held coins are never taken.
* Admins refund with the server CLI (`purchases <telegram_user_id>`, `refund <purchase_id> --reason`). A refund is allowed only for a paid purchase while all of its coins are available, so nothing is taken back only in part. In one transaction it takes the coins and days back, marks the Stars owed (reason `admin`, the note kept on the payment), and writes an audit entry. The worker's existing refund job returns the Stars within about a minute and keeps retrying until Telegram accepts. Running it again changes nothing.
* When Telegram reports a payment refunded (`refunded_payment`, for example after a dispute), a paid purchase is taken back as far as its coins are available; the rest is recorded on the payment as a shortfall for admins, and a warning is logged without user data. A refund already owed is recorded as made; one already recorded changes nothing. The payment row is locked, so each charge is applied once.
* The user is told what was removed, at most once, when the refund is recorded: after Telegram accepts an admin refund, or on Telegram's report, whichever records it first, including when Telegram answers that an earlier call already refunded it. A refund for a deleted account keeps its own notice. Like the bot's other notices, it is sent after the commit and not retried, so a crash in between loses it. Refund reports are payment updates: never dropped by flood control or the restricted-account gate, and not counted as RealChat activity.
* The terms say what a refund removes (terms version `draft-23`). Partial and self-service refunds are not offered.

**Why:** Owner decisions to remove the refunded pass's days as if it had never been bought, to send admin refunds through the worker, and to tell users what a refund removed.

### D-066 — Reconciling purchases with Telegram

**Date:** 2026-11-19
**Decision:**

* The admin CLI's `reconcile` command reads Telegram's whole list of the bot's Stars transactions (`getStarTransactions`, 100 per page) and compares it with every stored payment. It needs the bot token, read from the same configuration as the bot.
* Only users' invoice payments and refunds sent to users are compared: Telegram lists a refund under its payment's charge id, so outgoing transactions that match no payment (gifts, for example) and everything else (withdrawals, paid media) are skipped.
* It reports, each with the charge id and Telegram user id: paid at Telegram but not recorded here; recorded here but not at Telegram; refunded at Telegram but still paid here; refunded here with no refund at Telegram; a refund owed here that Telegram already made (the worker records it on its next pass); and a different Stars amount or user for the same charge. Refunds still owed with nothing at Telegram yet are only counted, as the worker's normal work.
* It is read-only: it never changes payments or balances. Existing commands fix only some differences: `refund` refunds a recorded purchase, and `coins` can deliver the coins of a payment that was never recorded or correct a balance. Since D-068, `record-payment` records a payment that was never recorded; one that matches none of our invoices needs manual handling. It isn't scheduled; a payment confirmed in the last few minutes can show as not recorded until the bot records it.

**Why:** Approved scope of the reconciliation issue: a read-only report run by an admin, with fixes kept manual and audited.

### D-067 — Typed confirmation for account deletion

**Date:** 2026-11-19
**Decision:**

* After the deletion warning, the user must reply with exactly `DELETE`, in capitals; any other reply (other text, other case or spacing, media) ends the request and keeps the account, with a message saying how to start again. Cancel still ends it. The one-tap confirm button is gone; an old one answers that it has expired.
* The warning lists the current coins, saying they include any bought and aren't refunded, and while VIP runs, the days left (a part day counts as one) and the end date, saying VIP isn't refunded.
* While the warning waits, any reply goes to the deletion check before the chat, so it is never relayed to a chat partner; commands are still handled as usual.
* Suspended and banned users still delete from their notice: the access gate lets their reply, text or media, through only while their warning waits, and never a command.
* The terms say deletion needs replying DELETE and that coins, including bought ones, and VIP time aren't refunded (terms version `draft-24`).

**Why:** The approved issue scope, and the owner's choice of an exact `DELETE`.

### D-068 — Recording a payment that was never recorded

**Date:** 2026-11-20
**Decision:**

* The admin CLI's `record-payment <charge_id>` records a payment Telegram has that was never recorded. It finds the charge in Telegram's Stars transactions and processes it with the same code as a payment confirmation (`fulfil`), using Telegram's own user, amount, and invoice reference, so nothing the admin types decides what is granted: a coin pack's coins or a VIP pass, once; for an account deleted meanwhile, a refund owed, which is sent at once (and retried by the worker if Telegram refuses).
* It is refused, writing nothing, if Telegram doesn't list the charge as a user's payment, has refunded it, it is already recorded, or it matches none of our invoices; those last are handled manually, outside the bot.
* The payment is recorded with the time it is recorded, as `fulfil` does: a VIP pass adds its days to the later of that time and the end of VIP already running, so a pass recorded late loses the days between the payment and the recording unless VIP was running. Each recording is audited (`payment_recorded`, `new_value` the `stars_payments` id).
* The user gets the receipt they would have got at the time, or the deleted-account refund notice, best effort. `reconcile` points to the command for payments that weren't recorded.

**Why:** Owner decisions to replay the payment from Telegram's own record, to send the usual receipt, and to leave payments matching none of our invoices to manual handling.

### D-069 — Terms and privacy notice as web pages

**Date:** 2026-11-20
**Decision:**

* The draft's bullets are regrouped, without changing their wording, into a terms of use part (age, chat requests, blocked content, conduct, blocking and reporting, coins, purchases and refunds, VIP, restrictions) and a privacy notice part (what others see, message and direct message storage, activity alerts about you, reminders, forwarding protection, account deletion and the record kept). Both stay in `backend/modules/users/terms.py`.
* The bot shows both everywhere it shows the terms, under "📄 Terms of use" and "🔒 Privacy notice", below one draft label, so users accept the same text as before (terms version `draft-25`).
* The API serves `/terms` and `/privacy` as self-contained HTML pages, with no scripts, fonts, images, or tracking, a content security policy that allows nothing else, the draft label, the terms version, and a link to the other page. The minimum age moves to the shared settings, so the page and the bot show the same one, and production requires it to be set for the API too.
* An optional `PUBLIC_BASE_URL` (https) makes the bot's /terms add link buttons to both pages; while it is empty, nothing changes. At deployment, the privacy policy link in BotFather is set to the `/privacy` page.
* The text is still the draft; final wording waits for legal review (O-007). Superseded by D-070.

**Why:** Owner decisions to serve the pages from RealChat's own server, as separate terms and privacy pages, and to link them from the bot once an address is set.

### D-070 — The User Agreement, the Privacy Policy, and the welcome screen

**Date:** 2026-11-20
**Decision:**

* RealChat's terms are a professional **User Agreement** (21 sections) and **Privacy Policy** (14 sections), with numbered sections and subsections, a version (`1.0`), and an effective date. They are written as plain text with headings in `backend/modules/users/legal/`, the single source for the bot and the web pages, and describe only what the product does; periods admins can change are not given as numbers.
* The operator is named only as "RealChat" ("we", "us"). Indian law governs, with the courts of India (no city). Users must be 18 or older. The existing support contact (in /help and /paysupport) is the contact for help, privacy requests, and grievances, with the acknowledgement and resolution times applicable Indian law sets. Data is stored on Oracle Cloud Infrastructure in India. The Privacy Policy follows India's Digital Personal Data Protection Act, 2023.
* Owner-requested protections: the Privacy Policy opens with its commitments (data never sold, handled by Telegram and the host and disclosed to others only as the policy's sharing section allows, that is where the law requires or to prevent harm, investigate abuse, or protect rights and safety, no ads, analytics, cookies, or AI training, and not kept forever, with the records kept after deletion named). Both documents state that personal information users share with each other is shared by their own choice, consent, and risk, and that RealChat is not responsible for it. The User Agreement adds that users are not screened, contact and meetings outside the Service are at the user's own risk, a release from disputes between users, RealChat's role as an intermediary under the IT Act, 2000, the electronic-record clause, and the usual general terms. The Service may go down, move servers, change, or end at any time without notice and without compensation, and data may not be kept. RealChat accepts no liability; only where a court finds liability that cannot be excluded is it capped at the Stars the user paid in the previous three months. Users must contact support and allow 30 days before any legal proceedings. No document can remove rights that Indian law does not allow to be waived, such as complaints to the Data Protection Board or a consumer commission, or the duty to report data breaches and handle grievances; the documents keep those and apply every exclusion to the greatest extent the law allows.
* The documents are official once the owner approves the text; there is no lawyer review. This resolves O-001 (18+; `MIN_AGE` can no longer be set below 18, and production no longer has to set it explicitly) and O-007 (owner-approved, no legal review).
* At first start, and when the version changes ("We've updated…"), the bot sends a short welcome: a bold title, an italic tagline, one line about RealChat, and "By tapping ✅ Continue you confirm your agreement with our Privacy Policy and User Agreement", with a button for each document, then a message with a blue (`primary` style) ✅ Continue keyboard button. Tapping it accepts the version the welcome showed; a Continue after the version changed, or without the welcome's state, shows the welcome again. Old "I agree" and "Decline" buttons accept nothing. The next text step takes the keyboard away.
* Once `PUBLIC_BASE_URL` is set, the document names are links and the buttons open the web pages; until then, the buttons send the document in the bot, as several messages with bold headings. /terms and Help's button send a short summary with the same buttons. Restricted users can read both.
* Formatting uses Telegram message entities, never a parse mode, so user content still can't be read as markup. Continue is handled ahead of every other message handler, so it is never relayed to a chat partner.

**Why:** Owner decisions on the operator's name, Indian law and courts, no legal review, the support contact as the only contact, 18+, and the welcome screen modelled on the owner's example.

## Open

Each of these needs the owner's decision. Until then, the behavior stays configurable and is not presented to users as final.

| ID | Question | Needed before | Notes |
|---|---|---|---|
| O-002 | Retention period for conversation content | Public launch | Kept separate from the retention of payment, moderation, security, and audit records (requirements §20). Development default: 7 days, changeable by admins (D-028, D-035). |
| O-006 | Webhook or long polling for receiving Telegram updates in production | Before deployment | Development will use polling. Webhooks require a public HTTPS endpoint. |
| O-008 | Which premium offer includes hiding the profile, and what happens when it ends | Before hidden profiles are built | D-021. VIP does not include it (owner, 2026-11-16). For example, whether the profile becomes visible again automatically. Also whether a hidden profile can still be opened by its RealChat ID (D-054); until decided, nothing hides a profile, so every finished profile can be. |
| O-009 | Retention period for the age and gender change history | Public launch | D-024. Separate from conversation content (O-002), because moderators use the history to spot repeated switching. Until decided, rows are kept until account deletion. |
| O-010 | The fourth button on the chat keyboard | Before the chat keyboard is final | D-030. Until then Block and Report are inline buttons on the partner's card and after each chat (D-045). |
| O-011 | Separate database roles for migrations and the running application | Before deployment | The application currently connects as the table owner, which can disable the audit log's append-only triggers. A restricted application role without UPDATE, DELETE, or TRUNCATE on `admin_audit_log` (and without DDL rights) would make the log tamper-resistant. Part of deployment hardening. |
| O-012 | Whether VIP users can open view-once media again | Before the feature is built | Private mode tells senders media can be opened once (D-048), so an extra view must not contradict what the sender is shown. Options offered: no extra view; an extra view with the in-chat note no longer promising a single view; or the note saying "once (twice with VIP)". Not part of the first VIP release. |
