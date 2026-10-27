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
| `status` | varchar(16) | One of `active`, `suspended`, `banned`, `deleted`. Defaults to `active`. A constraint requires `deleted_at` whenever the status is `deleted`. |
| `created_at` | timestamptz | |
| `last_active_at` | timestamptz | |
| `deleted_at` | timestamptz, nullable | Set when the user deletes their account (D-016). |

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

The index on `(user_id, field, changed_at)` serves the "latest change of this field" lookup. While a change is checked and written, the user's `profiles` row is locked (`SELECT ... FOR UPDATE`), so two simultaneous changes cannot both pass the cooldown. Account deletion (D-016) must erase these rows along with the rest of the profile data.

## Where rules are enforced

Profile rules are enforced twice:

1. **Service layer.** `backend/modules/users/validation.py` does the cleaning, length checks, and the link and @username rejection, and produces user-facing error messages.
2. **Database constraints.** These act as the last line of defence if a code path bypasses the service.

Rules that are hard to express in SQL, such as the link detection and the ISO country list, exist only in the service layer.

`alembic check` does not compare check constraints. A test therefore asserts that the check constraints declared on the models match the ones in the migrated database.

## Concurrency

`get_or_create_user` uses `INSERT … ON CONFLICT DO NOTHING` on `telegram_user_id`, followed by a select. Simultaneous first contacts from the same Telegram user therefore create exactly one user and one profile. This is covered by a concurrency test.

## Deletion and retention

**Planned, not implemented yet.** Account deletion (D-016) will erase the profile fields but keep the `users` row, with status `deleted` and `deleted_at` set. A deleted user then cannot bypass a moderation restriction by registering again. The deletion operation is part of the account safety controls issue. Until then, no code path deletes or erases accounts.
