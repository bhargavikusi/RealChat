# RealChat

RealChat is a Telegram-first anonymous chat and social discovery platform. Users interact primarily through a Telegram bot, with an optional Telegram Mini App for richer features.

## Status

Early development. The infrastructure runs locally: an API with health checks, PostgreSQL, Redis, and database migrations. The Telegram bot supports `/start` with a welcome screen to accept the User Agreement and Privacy Policy (a blue ✅ Continue button), conversational profile creation, profile viewing and editing (`/profile`), a coin balance with recent activity and an invite link (`/coins`), a welcome bonus, invite rewards, a daily reward that admins can switch on for promotions, notification settings (Silent mode, visit and like alerts, reminders) and account deletion in Advanced settings (it removes the profile, coins, VIP, and invite link, after a warning listing them, and only once the user replies DELETE), `/help`, `/terms`, and `/paysupport`. `/coins` also sells coin packs for Telegram Stars (four packs, admin settings): coins are added only once Telegram confirms the payment, exactly once per payment, and a payment that arrives after the account was deleted is refunded. `/vip` shows VIP status and sells the VIP pass (one payment, 180 days and 3,750 coins by default; buying again while VIP is running extends it, once at most one pass's length is left); VIP ends at its end date and coins are kept. While VIP lasts, ✅ 👑 VIP shows after the nickname on cards and in People lists and ✅ in notices (a VIP purchase, not identity verification), and VIP searches are matched first. Admins can refund a purchase, and refunds Telegram reports are applied too: the purchase's coins (as many as are left) and a VIP pass's days are taken back, and the user is told. VIP users also have 👻 Ghost mode in Advanced settings, on at the start of each VIP period: while it is on, the profiles they open get no visit alert and don't list the visit. Suspended and banned accounts are limited to help, the terms, payment support, and deletion. Users can search for a random partner for free, or pay coins to meet a chosen gender (the coins are held while searching, taken only when a match is made, and returned if the search ends without one), get matched by the worker, exchange messages and media (relayed anonymously, protected from forwarding and saving in Telegram apps that honour that setting), view their partner's profile, end the chat, turn on Private mode (messages sent while it is on are queued for deletion from both chats when it ends, usually gone within seconds; dice and games after about a day, as Telegram requires; photos, videos, voice messages, and video messages can be opened once and are deleted shortly after, unless the bot is interrupted at that moment; media sent within 30 seconds of your partner turning it off, or of its time limit, is held and you choose to send it or not), and block or report their partner. From their profile, 👥 People lists recent chat partners (from the last 10 days by default), whose cards can be opened again, and 👀 Visited you, with everyone who opened their card in the last 30 days by default (accounts that are no longer active are left out). Each visit from People notifies the person visited, except while the two are chatting together or when they turned visit alerts off. Every other person's card has ❤️ Like (then 💔 Unlike): a like notifies the person liked unless their like alerts are off, at most once per pair within an hour by default, and People lists ❤️ Liked you and 💘 You liked. You can't like someone you blocked. A card also offers ✉️ Message (1 coin by default): the message is previewed before sending, the coin is taken only once Telegram accepts the recipient's notice (and returned if it fails), the recipient opens it from that notice, the sender is told when it is opened, and one free reply is allowed. Direct messages can't be sent while either person has blocked the other, and are deleted after the chat retention period. A card also offers 💬 Chat request (2 coins by default) to someone who used RealChat in the last 10 minutes and isn't chatting or in Silent mode: the coins are taken once the request is delivered and aren't returned, the recipient can accept or decline within an hour, and accepting connects the two (someone busy is asked first, and a busy sender has 90 seconds to switch). A card also offers 🔔 Alert when active (1 coin by default): one alert the next time that person uses RealChat, within 7 days by default, or the coin back (not if you cancel it); it's RealChat activity, not Telegram online status, and anyone can turn off alerts about themselves in Advanced settings. While the daily reward is offered, users with Reminders on get at most one reminder a day, about the time they usually claim it (or 18:00 in their country by default), with a Claim button. Every profile card shows a RealChat ID as `/userid_<id>`: tapping it, or sending `/userid <id>`, opens that card (as a visit when it is someone else's), and `/userid` alone shows your own ID. After 10 wrong IDs in an hour (by default) lookups pause until the hour is over. People met this way can block and report each other from their cards without having chatted. Contact cards, locations, links, phone numbers, and @usernames can't be sent in chats. Stored chat messages are deleted automatically after the retention period (7 days by default), except while a report about the chat is open.

## Prerequisites

* [Python 3.13](https://www.python.org/downloads/)
* [uv](https://docs.astral.sh/uv/), the Python package manager
* [Docker Desktop](https://www.docker.com/products/docker-desktop/), or another Docker engine with Compose
* [Node.js 24](https://nodejs.org/), only to build the moderation Mini App (`apps/webapp`)

## Local setup

Install the Python dependencies. `uv` creates `.venv/` using Python 3.13:

```bash
uv sync
```

Copy the environment template, then edit `.env` with your local values. `.env` is ignored by Git and must never be committed.

```bash
cp .env.example .env
```

If a required setting is missing or invalid, the application stops at startup with an error. The error names the setting but never prints its value.

Start PostgreSQL and Redis, then apply the database migrations:

```bash
docker compose up -d --wait
uv run alembic upgrade head
```

PostgreSQL listens on `localhost:5433`, not the default 5432, so it doesn't clash with a PostgreSQL installed directly on the machine. Redis listens on `localhost:6379`. Both ports accept connections from this machine only.

Start the API:

```bash
uv run uvicorn --factory apps.api.main:create_app --reload
```

To check that it's running:

* `GET /health` returns 200 while the process is running.
* `GET /ready` returns 200 when PostgreSQL and Redis are both reachable. Otherwise it returns 503 and names the failing service.

The API also serves the User Agreement at `/terms` and the Privacy Policy at `/privacy`, as web pages with the same text and version the bot shows (the text lives in `backend/modules/users/legal/`). Once the API is reachable on the internet, set `PUBLIC_BASE_URL` to its https address so the bot's /terms links to them, and set the privacy policy link in @BotFather to `<PUBLIC_BASE_URL>/privacy`.

### Moderation Mini App

Moderators and admins will review reports in a Telegram Mini App (D-071), a React app in `apps/webapp` that the API serves at `/moderation/` once it is built. It lists open reports (urgent first) and shows each one with the reported person's card and record and the stored content: the chat, or for a profile report the direct messages and chat requests between the two (up to the latest 2,000 messages). Media can be sent to the moderator's own Telegram chat, where it is queued for deletion after 10 minutes (the worker deletes it then, or later if Telegram is slow to respond). Each report is closed with no action, a warning, a suspension, or a ban, confirmed in Telegram first. Every opening and media view is audited. Lookup finds an account by its RealChat ID (as on profile cards, e.g. `/userid_k7m3qxa`) and shows its card, status, record, and reports, with warn, suspend, ban, and lift (only admins lift bans); lifting is offered here, not from a report. Admins can also open the account's stored chats and direct messages, one at a time, after typing a reason that is saved in the audit log. Admins also get a Users tab: every account, searchable by RealChat ID or nickname, with filters (VIP, status, role, activity, sign-up date, country, gender, age, coins, reports) and sorting, opening into the account view. Moderators and admins are alerted in the bot to each urgent report as soon as it is filed, and, on days when any report is open, get a summary of open reports at an hour admins set (India time, 09:00 by default). The API also needs `TELEGRAM_BOT_TOKEN` now, because it checks Telegram's signature on the Mini App's launch data with every request; data older than an hour is refused, and the account's role is checked each time.

Build it after installing exactly the locked packages (their install scripts are off, see `apps/webapp/.npmrc`):

```bash
cd apps/webapp
npm ci
npm run build
```

Telegram opens Mini Apps only from an https address, so to try it on a phone during development, run a temporary Cloudflare quick tunnel to the local API (no account needed; the address changes each time, and development traffic passes through Cloudflare while it runs, so use test data only):

```bash
cloudflared tunnel --url http://localhost:8000
```

Put the printed `https://….trycloudflare.com` address in your local `.env` as `PUBLIC_BASE_URL`, then restart the bot and the API. Moderators and admins then see 🛡 Moderation in Advanced settings. Remove it from `.env` when you stop the tunnel.

To stop the services, run `docker compose down`. Data is kept in Docker volumes. Add `-v` to delete it as well.

### Telegram bot

Create a development bot with [@BotFather](https://t.me/BotFather), keeping it separate from any production bot. Put its token in `.env` as `TELEGRAM_BOT_TOKEN`. Then, with PostgreSQL and Redis running, start the bot:

```bash
uv run python -m apps.bot.main
```

During development the bot uses long polling, so it needs no public URL. It stops on Ctrl+C. The minimum age is `MIN_AGE` (default 18, and never lower, since the User Agreement and Privacy Policy say the Service is for adults only; see D-009 and D-070).

At startup the bot registers its command menu (`/start`, `/profile`, `/coins`, `/vip`, `/userid`, `/help`, `/terms`, `/paysupport`) with Telegram for private chats. After onboarding, the keyboard under the message box offers 🎭 New Anonymous Chat, which opens a menu of chats to choose from (a free random chat, or a paid chat with men, women, or other genders); during a chat it shows 👤 Partner profile (whose card offers Block and Report), 🔒 Private mode, and ⏹ End chat, and other messages the user sends are relayed to their partner (except commands, messages refused under the chat rules, and answers to a pending step such as a profile edit). The user's own profile card (`/profile`) also offers 👥 People, with past chat partners. Age and gender can each be changed once per `PROFILE_CHANGE_COOLDOWN_DAYS` (default 30). `/help` shows the announcement channel link only when `ANNOUNCEMENT_CHANNEL_URL` is set, and the support link only when `SUPPORT_CONTACT` is set. Each must be an `https://` link. `/paysupport` also uses `SUPPORT_CONTACT`, which is required when `APP_ENV=production`, because Telegram requires support for bots that take payments.

While Redis is available, two protections apply:

* An update that Telegram delivers again after being handled successfully is dropped for `BOT_UPDATE_DEDUP_TTL_SECONDS`. If the bot stops or crashes while handling an update, Telegram redelivers it after the restart and it is processed. An update whose handler fails with an error is **not** retried automatically. The user is told something went wrong and can try again.
* Each user is subject to a flood limit of `BOT_FLOOD_LIMIT` updates per `BOT_FLOOD_WINDOW_SECONDS`.

If Redis is unavailable, both protections let updates through so the bot stays online. Handlers with side effects, such as charges or matches, must therefore enforce their own idempotency in the database.

#### Testing Stars payments without real Stars

Telegram's [test environment](https://core.telegram.org/bots/webapps#using-bots-in-the-test-environment) is a separate network with its own accounts and bots, where Stars payments cost nothing. Log a Telegram app into a test account, create a bot with the test environment's @BotFather, then set `TELEGRAM_TEST_ENVIRONMENT=true` and that bot's token as `TELEGRAM_BOT_TOKEN`, and restart the bot and worker. Run it instead of the normal development bot, not alongside it, since they share the database and Redis. The bot refuses this setting with `APP_ENV=production`.

### Worker

Matching needs the worker running alongside the bot. About once a second it pairs users who are searching, ends searches that pass the time limit (D-041), and warns and then ends quiet chats (D-039):

```bash
uv run python -m apps.worker.main
```

It uses the same `.env` as the bot, because it sends Telegram messages. It stops on Ctrl+C.

### Roles and admin settings

Roles (`user`, `moderator`, `admin`) are changed only from the server, with the same `.env` as the bot. The person must have started the bot first. Every change is recorded in the admin audit log.

```bash
uv run python -m apps.admin_cli set-role <telegram_user_id> admin
uv run python -m apps.admin_cli revoke <telegram_user_id>
uv run python -m apps.admin_cli list
```

The same tool grants or corrects coins, with a reason that is kept in the ledger, and shows an account's coins. With `--key`, running the same adjustment again changes nothing; each key is for one adjustment only, and reusing it for a different one is refused:

```bash
uv run python -m apps.admin_cli coins <telegram_user_id> <amount> --reason "<why>" [--key <id>]
uv run python -m apps.admin_cli balance <telegram_user_id>
```

It also lists an account's Stars purchases and refunds one. A refund is refused if any of the purchase's coins were spent; it takes the coins back, and a VIP pass's days, at once, and the worker returns the Stars within about a minute. Running it again changes nothing:

```bash
uv run python -m apps.admin_cli purchases <telegram_user_id>
uv run python -m apps.admin_cli refund <purchase_id> --reason "<why>"
```

To check purchases against Telegram's own record of the bot's Stars transactions, run `reconcile`. It needs the bot token (the same `.env` as the bot), changes nothing, and lists each difference with its charge id and Telegram user id, such as a payment Telegram has that wasn't recorded, or a refund recorded on only one side. `refund` and `coins` fix some of them, and `record-payment` records a payment that was never recorded, as its confirmation would have, dated when it is recorded (a VIP pass starts then, or after VIP already running). It also needs the bot token. The user gets the usual receipt, or, for an account deleted meanwhile, the Stars are refunded. A payment that matches none of our invoices needs manual handling:

```bash
uv run python -m apps.admin_cli reconcile
uv run python -m apps.admin_cli record-payment <charge_id>
```

Admins then find **🛠 Admin settings** under `/profile` → ⚙️ Advanced settings, where runtime settings such as the search time limit and chat retention can be changed without a restart (D-035). Deleting an account resets its role to `user`.

## Checks

GitHub Actions runs these checks on every pull request to `develop`, `staging`, or `main`, and again after each merge (`.github/workflows/ci.yml`). The workflow also cycles the migrations on a clean database, runs `alembic check` to catch model changes that lack a migration, and scans dependencies for known vulnerabilities. Its PostgreSQL and Redis are throwaway containers with CI-only credentials; no repository secrets are used.

Run all four locally before opening a pull request:

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # type checks
uv run pytest                # tests
```

For the moderation Mini App, CI also runs these, and audits its packages:

```bash
cd apps/webapp
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

Integration tests use the `realchat_test` database and Redis database 15, configured by `TEST_DATABASE_URL` and `TEST_REDIS_URL` in `.env`. They need `docker compose up` to be running. Without those settings the tests are skipped. Set `REQUIRE_INTEGRATION=1` to make missing settings a failure instead, as CI does.

The test database is created only when the PostgreSQL volume is first created. If your volume is older than the init script, create the database once with:

```bash
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" realchat_test'
```

## Documentation

* [Product requirements](docs/requirements.md)
* [Architecture](docs/architecture.md)
* [Database schema](docs/database.md)
* [Decisions and open questions](docs/decisions.md)
* [Contributor instructions](CLAUDE.md)

## Branches

* `main` — production-ready code
* `staging` — staging validation
* `develop` — integrated development work

Work happens on short-lived issue branches created from `develop`.
