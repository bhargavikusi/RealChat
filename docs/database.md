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

An append-only record of administrative changes. Rows are never updated or deleted, including on account deletion. A trigger (`admin_audit_log_append_only`) enforces this in the database: any `UPDATE` or `DELETE` of a row fails. `TRUNCATE`, used only to reset test databases, is not affected.

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
