# Database

PostgreSQL is the source of truth for durable state. The schema changes only through Alembic migrations in `migrations/versions/`. The application never creates or alters tables at startup. Models are in `backend/modules/*/models.py`, and each model module must be imported in `backend/database/models.py` so that migrations can see it.

All timestamps are `timestamptz` and stored in UTC. Constraint names follow the convention in `backend/database/engine.py`, for example `ck_profiles_gender` and `uq_users_telegram_user_id`.

## Tables

### `users`

There is one row per Telegram account.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | Internal primary key. Other tables reference this, never the Telegram ID. |
| `telegram_user_id` | bigint, unique | Telegram's stable numeric user ID. Usernames change and are never used as keys. |
| `status` | varchar(16) | One of `active`, `suspended`, `banned`, `deleted`. Defaults to `active`. A constraint requires `deleted_at` whenever the status is `deleted`. `suspended` and `banned` are set manually until moderation tools exist (D-026). |
| `created_at` | timestamptz | |
| `last_active_at` | timestamptz | |
| `deleted_at` | timestamptz, nullable | When the user last deleted their account (D-016). Kept if they return. |
| `role` | varchar(16) | One of `user` (default), `moderator`, `admin` (D-034). Changed only by the admin CLI, except that account deletion resets it to `user`. |

### `profiles`

There is one row per user. The primary key is also a foreign key to `users.id`, with `ON DELETE CASCADE`. Fields stay NULL until the user provides them.

| Column | Type | Rule |
|---|---|---|
| `nickname` | varchar(20) | 2–20 characters (D-012), with no leading or trailing spaces, so a blank nickname cannot be stored. |
| `age_at_entry` | smallint | 13–99 in the database. The eligibility minimum (D-009, currently 18) is a setting applied by the service. |
| `age_entered_on` | date | Set together with `age_at_entry`, enforced by a constraint. The current age is derived from both (D-010). |
| `gender` | varchar(8) | `male`, `female`, or `other` (D-011). |
| `country_code` | char(2) | Two uppercase letters. The service also checks it against ISO 3166-1. |
| `city` | varchar(60) | Optional. No leading or trailing spaces. Stored as entered otherwise; normalization comes with discovery work. |
| `bio` | varchar(300) | Optional. No leading or trailing spaces. |
| `photo_file_id` | text | Optional. A Telegram file identifier only; the application never downloads or stores image files. |
| `visibility` | varchar(8) | `visible` (default) or `hidden`. Nothing sets `hidden` yet: hiding the profile is a premium feature built with the VIP work (D-021). |
| `onboarding_completed_at` | timestamptz | A constraint allows this to be set only once nickname, age, gender, and country are present (D-013). |
| `created_at`, `updated_at` | timestamptz | |

### `terms_acceptances`

Records which terms version each user accepted, and when (D-015).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `user_id` | bigint | References `users.id`, with `ON DELETE CASCADE`. |
| `terms_version` | varchar(32) | |
| `accepted_at` | timestamptz | |

`(user_id, terms_version)` is unique, so repeated acceptance of the same version keeps the first timestamp. That unique index also serves lookups by user.

### `profile_changes`

An append-only history of age and gender changes made after onboarding (D-024). The latest row for a field drives the change cooldown (D-019). Rows are never updated.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `user_id` | bigint | References `users.id`, with `ON DELETE CASCADE`. |
| `field` | varchar(16) | `age` or `gender`. |
| `old_value`, `new_value` | varchar(16) | For age, the displayed (current) age at the time of the change. For gender, the stored value. |
| `changed_at` | timestamptz | |

The index on `(user_id, field, changed_at)` serves the "latest change of this field" lookup. While a change is checked and written, the user's `profiles` row is locked (`SELECT ... FOR UPDATE`), so two simultaneous changes cannot both pass the cooldown. Account deletion (D-016) must erase these rows along with the rest of the profile data. Their retention period is otherwise still open (O-009).

### `app_settings`

Runtime settings changed by admins (D-035). A row exists only for a setting an admin has changed; any other setting uses its default. The known keys, defaults, and allowed ranges are defined in code (`backend/modules/admin/settings.py`), so adding a setting needs no migration. A stored value outside the allowed range is ignored in favour of the default.

| Column | Type | Notes |
|---|---|---|
| `key` | varchar(64), primary key | |
| `value` | bigint | All current settings are whole numbers. |
| `updated_at` | timestamptz | |

### `admin_audit_log`

An append-only record of administrative changes. Rows are never updated or deleted, including on account deletion. Triggers enforce this in the database: any `UPDATE`, `DELETE`, or `TRUNCATE` fails, including a `TRUNCATE ... CASCADE` from `users`. The integration tests switch the `TRUNCATE` guard off only while emptying the isolated test database. The triggers guard against mistakes in application code, but the table owner can disable them; full protection needs a restricted application database role (O-011).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `actor_user_id` | bigint, nullable | References `users.id`. NULL means the server-side admin CLI. |
| `action` | varchar(32) | `role_changed` or `setting_changed`. |
| `target_user_id` | bigint, nullable | References `users.id`. The account whose role changed. |
| `setting_key` | varchar(64), nullable | The setting that changed. |
| `old_value`, `new_value` | varchar(64), nullable | |
| `created_at` | timestamptz | |

Its retention period is not decided yet; audit records are kept separately from conversation content (requirements §20).

### `conversations`

One row per chat between two matched users (D-027).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `started_at` | timestamptz | |
| `ended_at` | timestamptz, nullable | NULL while the chat is open. |
| `end_reason` | varchar(16), nullable | `user_ended`, `unreachable`, `idle`, `blocked`, or `reported` (D-045); set together with `ended_at`, enforced by a constraint. |
| `ended_by_user_id` | bigint, nullable | References `users.id`. The user who ended it, blocked, or reported their partner; NULL for automatic endings. |
| `last_activity_at` | timestamptz | When a message was last relayed, or the chat started. Updated with the row locked, in the same transaction that records the message; if that message then cannot be delivered, the previous value (and warning) is restored, unless another message was recorded since. |
| `idle_warned_at` | timestamptz, nullable | When both users were warned that the quiet chat will end (D-039). Cleared by any relayed message. |

### `conversation_participants`

The two users in each conversation. The primary key is `(conversation_id, user_id)`; `conversation_id` references `conversations.id` with `ON DELETE CASCADE`, and `user_id` references `users.id`.

| Column | Type | Notes |
|---|---|---|
| `conversation_id`, `user_id` | bigint | |
| `left_at` | timestamptz, nullable | Set for both users when the conversation ends. |

A partial unique index on `user_id` where `left_at IS NULL` allows each user at most one open conversation. It is the final guard against double matches: if two matches for the same user are attempted at once, one insert fails. The index on `(user_id, conversation_id)` serves "this user's conversations", used for repeat-match avoidance. Ending a conversation locks its `conversations` row and re-reads it, so when both users end a chat at the same moment it is ended once. Conversations that end because a message could not be delivered to the partner have the end reason `unreachable`, and quiet ones `idle`, both with no `ended_by_user_id`. A partial index on `last_activity_at` for open conversations serves the worker's search for quiet chats. Recording a message locks the conversation row for update, as do the idle warning and ending (which skip rows that are locked), so a message and an idle ending never interleave, and with several workers each chat is warned and ended once.

### `chat_messages`

Each message relayed between chat partners (D-028). It maps the sender's message to the partner's copy, so replies and edits can be mirrored, and keeps the content for moderation.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `conversation_id` | bigint | References `conversations.id`, with `ON DELETE CASCADE`. |
| `sender_user_id` | bigint | References `users.id`. |
| `sender_message_id`, `recipient_message_id` | bigint (`recipient_message_id` nullable) | Telegram message ids: the original in the sender's chat, and the copy in the partner's chat. The row is written before the copy is sent, and `recipient_message_id` is filled in once the copy exists. It stays NULL if the bot stopped in between, in which case the message may not have been delivered. |
| `kind` | varchar(16) | `text`, `photo`, `video`, `animation`, `audio`, `voice`, `video_note`, `sticker`, `document`, `poll`, `dice`, or `other`. |
| `text` | text, nullable | The original text or caption. Never overwritten. |
| `edited_text`, `edited_at` | text, timestamptz, nullable | The latest edit and when it was made (D-043). A constraint requires `edited_at` whenever `edited_text` is set. |
| `file_id` | text, nullable | A Telegram file identifier for media; files are never downloaded. |
| `created_at` | timestamptz | |

`(sender_user_id, sender_message_id)` is unique and is claimed before sending, so a message delivered to the bot twice is relayed at most once, even if the bot stopped right after sending it. A record is removed if its copy could not be delivered, or was taken back because the chat ended. If linking the record to its copy fails, the copy is taken back but the record stays, with `recipient_message_id` NULL and its content, so a redelivery is still recognized; it is subject to the same retention as other content. The index on `(conversation_id, recipient_message_id)` serves reply lookups. Contact cards, locations, and refused text are never stored (D-042). The content columns (`text`, `edited_text`, `file_id`) are for moderation only and are never written to ordinary logs. Each row is deleted entirely once it is older than the retention period (D-028, D-046; admin setting, default 7 days), unless its conversation has an open report (see `reports`). The index on `created_at` serves that cleanup.

View-once media (D-048) uses four more columns: `view_once` (boolean, default false; set when a photo, video, voice, or video message is recorded in Private mode, and `recipient_message_id` then holds the tap-to-view placeholder), `media_seconds` (integer, nullable; the length of a video or voice message, added to the view timer), `viewed_at` (timestamptz, nullable; when it was opened, at most once), and `viewed_message_id` (bigint, nullable; the copy sent to the partner on opening). Constraints allow `viewed_at` only for view-once messages, and `viewed_message_id` only once `viewed_at` is set. Opening locks the conversation row, as ending Private mode does, so nothing can be opened after the session ends. The opened copy, the placeholder, and the original are queued in `telegram_deletions` for the end of the viewing time.

`private_session_id` (bigint, nullable, references `private_sessions.id`) links a message to the Private mode session it was sent in (D-047). It is set when the message is recorded, under the conversation's row lock, which turning Private mode on or off also takes, so a message is never linked to a session that has already ended. A partial index on it serves "the messages of this session".

### `private_sessions`

Periods of Private mode in a conversation (D-031, D-047).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `conversation_id` | bigint | References `conversations.id`, with `ON DELETE CASCADE`. |
| `started_by_user_id` | bigint | References `users.id`. |
| `started_at` | timestamptz | |
| `ended_at`, `end_reason` | timestamptz, varchar(16), nullable | Set together (constraint). `turned_off`, `chat_ended`, or `time_limit`. |
| `ended_by_user_id` | bigint, nullable | References `users.id`. The user who turned it off. |

A partial unique index on `conversation_id` where `ended_at IS NULL` allows one active session per conversation. A partial index on `started_at` for active sessions serves the worker, which ends sessions whose chat has ended or that reached the time limit (admin setting, default 24 hours, at most 46). It locks the conversation row before deciding, in the same order as turning Private mode off, recording a message, linking its copy, and ending the chat; so a message being recorded or linked is either seen and queued, or sees the session ended and queues itself, and the reason is never a stale `time_limit` for a chat that just ended. Ending a session updates only a row whose `ended_at` is still NULL, so it ends, and its messages are queued, once.

### `telegram_deletions`

The queue of messages to delete from users' Telegram chats (D-047). It holds Telegram identifiers only, no content, and is independent of the retention of `chat_messages`.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `chat_id`, `message_id` | bigint | The private chat (the user's Telegram id) and the message. Unique together. |
| `not_before` | timestamptz | Not tried before this time: when queued for most messages; 24 hours and 5 minutes after sending for dice, which Telegram refuses to delete earlier. A worker trying an entry pushes it back by one minute, so another worker leaves it alone and an attempt cut short is retried. |
| `expires_at` | timestamptz | 48 hours after the bot recorded the message (`chat_messages.created_at`), approximating when Telegram stops allowing deletion. The bot records a message moments after the user sends it, so this can be slightly later than Telegram's own limit; a late attempt is refused by Telegram and the entry removed. Entries past it are removed without being tried. A constraint requires `not_before < expires_at`. |
| `created_at` | timestamptz | |

Ending a session queues the sender's original and the partner's copy of each of its messages. A message whose copy was still on its way when the session (or the chat) ended is queued when the relay links it, or, if the chat ended, both its original and its copy are queued as the relay withdraws the copy, so a failed withdrawal is retried. Entries are removed once deleted, already gone, refused, or for a user who blocked the bot. Entries Telegram could not be asked about stay and are retried: after a network error once the one-minute lease runs out, and after a request to slow down once the time Telegram asked for has passed. The table references no other table, so the integration tests empty it explicitly.

### `user_blocks`

Blocks between users (D-045). Blocks do not affect random matching; they will stop direct messages and chat requests once those exist.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `blocker_user_id`, `blocked_user_id` | bigint | Reference `users.id`. A constraint stops a user blocking themselves. |
| `conversation_id` | bigint | References `conversations.id`. The chat the block was made from: only a past or current partner can be blocked. |
| `blocked_at` | timestamptz | |
| `unblocked_at` | timestamptz, nullable | Set when the block is lifted. The row is kept as history, and blocking again adds a new row. |

A partial unique index on `(blocker_user_id, blocked_user_id)` where `unblocked_at IS NULL` allows one block in force per pair, so repeated or simultaneous presses store one. Rows are kept when either account is deleted.

### `reports`

Reports about chat partners (D-037, D-045).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint identity | |
| `reporter_user_id`, `reported_user_id` | bigint | Reference `users.id`. A constraint stops a user reporting themselves. |
| `conversation_id` | bigint | References `conversations.id`. Only the other participant of a conversation the reporter was in can be reported. |
| `reason` | varchar(16) | `spam`, `harassment`, `sexual`, `under_18`, `threats`, or `other`. |
| `details` | varchar(500), nullable | Optional context from the reporter, 1–500 characters, added once. For moderators only; never written to ordinary logs. |
| `urgent` | boolean | True for `under_18`. |
| `status` | varchar(16) | `open` (default) or `closed`. Nothing closes reports yet; review comes with the moderation work. |
| `created_at` | timestamptz | |
| `closed_at` | timestamptz, nullable | Set exactly when the status is `closed`, enforced by a constraint. |

`(reporter_user_id, conversation_id)` is unique: one report per user per conversation. The index on `(reporter_user_id, created_at)` serves the per-user limit on reports in 24 hours (an admin setting); the reporter's `users` row is locked while counting (`FOR NO KEY UPDATE`), so simultaneous reports cannot pass the limit together. A block or report takes all its locks up front, in the same order as account deletion: the user's row, the partner's row (key share), then the conversation. So two users blocking or reporting each other in the same chat at once, or one doing so while the other deletes their account, cannot deadlock. A partial index on `conversation_id` for open reports serves the retention cleanup, which keeps those conversations' messages. Rows are kept when either account is deleted.

## Where rules are enforced

Profile rules are enforced twice:

1. **Service layer.** `backend/modules/users/validation.py` does the cleaning, length checks, and the link and @username rejection, and produces user-facing error messages.
2. **Database constraints.** These act as the last line of defence if a code path bypasses the service.

Rules that are hard to express in SQL, such as the link detection and the ISO country list, exist only in the service layer.

`alembic check` does not compare check constraints. A test therefore asserts that the check constraints declared on the models match the ones in the migrated database.

## Concurrency

`get_or_create_user` uses `INSERT … ON CONFLICT DO NOTHING` on `telegram_user_id`, followed by a select. Simultaneous first contacts from the same Telegram user therefore create exactly one user and one profile. This is covered by a concurrency test.

Role changes lock the `users` row. A setting change share-locks the acting admin's `users` row and re-checks the role in the same transaction, so a revocation cannot interleave with it. Setting changes also take a transaction-scoped advisory lock per setting key, so concurrent changes run one after the other and each audit entry records the value it actually replaced. Both are covered by concurrency tests.

## Deletion and retention

Account deletion (`service.delete_account`, D-016, D-025) runs in one transaction with the `users` row locked:

* The `profiles` row is replaced by an empty one, which erases every profile field.
* All `profile_changes` and `terms_acceptances` rows for the user are deleted.
* The `users` row is kept with its Telegram user ID and timestamps, and `deleted_at` is set. A moderator or admin role is reset to `user`, with an audit entry. Admin audit entries are kept. An `active` account becomes `deleted`. A `suspended` or `banned` account keeps its status, so deleting cannot be used to escape a restriction.

A deleted user who returns goes through onboarding again. Accepting the terms sets the status back to `active`; a restricted status is never changed this way.

Chat messages are deleted by a daily worker job (`backend/modules/chat/retention.py`, D-046):

* Rows in `chat_messages` older than the retention period (admin setting `chat_retention_days`, default 7) are deleted entirely, oldest first, in batches of 1,000, each in its own transaction. Rows locked by the relay at that moment are skipped and deleted on a later run.
* Messages in a conversation with an open report are kept until the report is closed.
* `conversations`, `conversation_participants`, `user_blocks`, `reports`, and `admin_audit_log` are not touched by it.
* A Redis key (`retention:chat_messages:last_run`) makes the job run normally once a day across workers: it is set for an hour when a run starts, renewed after every batch while the run lasts, and set for a day once the run succeeds, so a failed or interrupted run is retried within the hour. Only the number of deleted rows is logged.
* Deleting a stored message does not remove it from either user's Telegram chat: this job never calls Telegram.
