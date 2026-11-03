# Sunday Open backend: migration from Google Apps Script to Python

Version 1.0 · 03.11.2026
Source of truth for current behaviour: `github/Code.gs` (single Apps Script file, ~1,100 lines).

## 1. Goal

Rewrite the whole server side currently in `Code.gs` as a Python service with **the same behaviour and the same HTTP contract**. The two existing front-ends (Portal, Workspace) must keep working after changing a single constant, `SYNC_URL`, to the new address. No front-end rewrite is in scope.

## 2. What exists today (what is being migrated)

| # | Component | Current implementation |
|---|---|---|
| A | Sync API for the Portal and Workspace sites (read all data, propose changes) | `doGet`, `doPost` |
| B | Versioned storage with an append-only journal | sheets `Data`, `SundayHistory` |
| C | Moderation queue (changes wait for approval) | sheet `SundayProposals`, Sheet menu «Sunday Open» |
| D | Email notifications to the moderator about new proposals | trigger every 5 min, `MailApp` |
| E | AI helper proxy (OpenRouter, key kept server-side) | `sundayAi_` |
| F | Telegram bot (webhook) | `tgWebhook_` and the `tg*` functions |
| G | Telegram Mini App API (profile, event registration) | `tgAppApi_` |
| H | Club registration, contacts, RSVP, QR check-in, attendee lists | sheets `SundayContacts`, `SundayRSVP` |
| I | Daily jobs (reminders, deadlines, lists for organisers) | trigger at 09:00, `sundayBotDaily` |
| J | One-time setup (webhook, bot commands, menu button) | `setupSundayBot`, `setupSundayModeration`, `setupSundayNotifications` |

## 3. In scope

1. A Python 3.12 web service that serves A, C, E, F, G and H over HTTPS.
2. A scheduler for D and I.
3. A PostgreSQL database that replaces all the sheets (schema in §6).
4. A moderator web page (or Telegram-only flow) that replaces the Google Sheet menu «Одобрить / Отклонить выбранную правку» (§7.3).
5. A one-time import script from the current Google Sheet into PostgreSQL that preserves versions, history, proposals, contacts and RSVPs.
6. Configuration through environment variables (§10).
7. Automated tests for the contract in §5 and the acceptance criteria in §12.
8. Deployment: a Dockerfile plus a short README covering run, migrate, set the webhook, and back up.

## 4. Out of scope

- Any change to the Portal or Workspace UI, except the `SYNC_URL` constant.
- New features beyond what `Code.gs` does today.
- Hosting the static front-ends (they stay on GitHub Pages).
- User accounts or passwords on the website. Identity comes only from Telegram `initData`, as today.
- Payments, analytics, and multi-language support.

## 5. HTTP contract (must stay byte-compatible)

All responses are JSON with `Content-Type: application/json`. The front-ends send POST bodies as `text/plain;charset=utf-8` to avoid a CORS preflight, so the service must **parse JSON from a text/plain body** and return `Access-Control-Allow-Origin: *` on GET and POST. Apps Script answers every request with HTTP 200 and an `ok` flag; keep that pattern (errors go in the body, not in the status code).

### 5.1 `GET /` — read everything
Response:
```json
{"protocol": 4, "ok": true,
 "data": {"<table>": {"rows": {"<id>": {"row": {...}, "v": 3}}, "deleted": ["<id>"], "deletedVersions": {"<id>": 4}}}}
```
- Return one entry for every table in the allow-list (§6.1), even when it is empty.
- `protocol` is 4 when moderation is on and 3 when it is off.

### 5.2 `POST /` — propose or apply a change
Request (an "operation"):
```json
{"protocol": 4, "table": "portal", "id": "portal-abc", "mutationId": "portal:abc:lx2k9",
 "baseVersion": 2, "del": false, "row": {"_id": "portal-abc", "_o": 1730600000000, "...": "..."},
 "tg": "<optional Telegram initData>"}
```
Validation (reject with `{"ok": false, "error": "Invalid operation"}`):
- `table` is in the allow-list and is not `calc`.
- `id` starts with `table + "-"` and matches `^[a-zA-Z0-9_-]{1,120}$`.
- `mutationId` matches `^[a-zA-Z0-9_:-]{1,180}$`.
- `baseVersion` is an integer ≥ 0, and `del` is a boolean.
- If `del` is true, `row` is null. Otherwise `row` is an object with `row._id == id` and a numeric `row._o`.
- The serialised operation is at most 30,000 characters.

Protocol mismatch → `{"ok": false, "error": "upgrade_required"}`.

Hash = SHA-256 hex of the JSON array `[table, id, baseVersion, del, row]`, serialised exactly like JavaScript `JSON.stringify` (no spaces, keys in insertion order). It is used for idempotency.

Moderated mode (protocol 4), response `{"protocol": 4, "ok": ..., "status": ..., "table", "id", "mutationId", "error"}`:
- `mutationId` already in the queue with the same hash → return that proposal's current status. With a different hash → error `"Mutation ID was reused with different contents"`.
- `mutationId` already in the journal → `status: "approved"` together with the published version and row.
- `baseVersion` differs from the current version → `status: "conflict"` with the current version and row.
- More than 1,000 proposals pending → error `"Review queue is full; draft remains on your device"`.
- Otherwise add the proposal as `pending`, notify moderators (§7.2), record the Telegram author if `tg` verifies (§8.1), and return `status: "pending"`.

Direct mode (protocol 3): apply immediately (§7.1). Response `{"protocol": 3, "ok", "table", "id", "mutationId", "version", "row", "deleted", "error": null | "conflict"}`.

### 5.3 `POST /` with `{"ai": {"model": "...", "messages": [...]}}`
- Bypasses locking and moderation.
- Take at most the last 24 messages. Reject if the payload is over 60,000 characters (`bad_request`).
- Forward to `https://openrouter.ai/api/v1/chat/completions` with the server key and header `X-Title: Sunday Open`. The model is always `openrouter/free`.
- Responses: `{"ok": true, "content": "<assistant text>"}`, `{"ok": false, "error": "no_key"}`, or `{"ok": false, "error": "<message>"}`.

### 5.4 `POST /` with `{"me" | "reg" | "rsvp": {"initData": "...", ...}}` — Mini App API
- Verify `initData` (§8.1). On failure return `{"ok": false, "error": "auth"}`.
- `reg.profile` = `{firstName, lastName, email, club, role}`. Save it. The phone is **never** accepted from this endpoint.
- `rsvp` = `{event: {id, title, date, club}, going: bool}`. Rules are in §9.2.
- Every successful call returns the state:
```json
{"ok": true, "profile": {...}, "registered": true, "missing": ["телефон"],
 "rsvps": [{"eventId", "title", "date", "code", "qr", "checkedIn"}],
 "counts": {"<eventId>": 12}, "bot": "<bot username>"}
```
- Business errors return the same state plus `"ok": false` and `"error"`: `profile`, `past`, `bad_event` or `busy`.

### 5.5 `POST /?tg=<TG_SECRET>` — Telegram webhook
- Ignore the request if the secret does not match. Always answer `ok`.
- Deduplicate by `update_id` for 6 hours.
- Handle `message` (private chats only, text or contact) and `callback_query`.

New, recommended: also check Telegram's `X-Telegram-Bot-Api-Secret-Token` header, and register the webhook with `secret_token`.

## 6. Data model (PostgreSQL)

### 6.1 Table allow-list
`portal, steps, sundays, comms, guests, audiences, bank, pillars, channels, cycle, weekly, bookings, risks, requests, materials, briefs, lifecycle, charter, domains, raci, finq, standup, fwtasks, portfolio, pmotasks, perimeters, docs, robot, portalmap, route, roles, stakeholders, office, rules, calc` (`calc` is never writable).

### 6.2 Tables
| Table | Columns | Replaces sheet |
|---|---|---|
| `records` | `tbl, id (PK together), version int, row jsonb null, deleted bool, updated_at` | Data |
| `history` | `mutation_id PK, hash, time, tbl, id, action (create/update/delete), base_version, version, before jsonb, after jsonb` | SundayHistory |
| `proposals` | `mutation_id PK, hash, created_at, tbl, id, base_version, before jsonb, after jsonb, is_delete, status (pending/approving/approved/rejected/conflict), reviewed_at, reviewed_by, reason` | SundayProposals |
| `email_notified` | `mutation_id PK, notified_at` | SundayNotifications |
| `bot_users` | `tg_id PK, name, username, started_at, reminders bool` | SundayBotUsers |
| `authors` | `mutation_id PK, tg_id, name, tbl, row_id, created_at` | SundayAuthors |
| `contacts` | `tg_id PK, first_name, last_name, username, phone, email, club, role (Участник/Организатор/Волонтёр), updated_at` | SundayContacts |
| `rsvp` | `code PK (12 hex), event_id, title, date, club, tg_id, name, status (going/cancelled), created_at, checked_in_at, checked_by`; unique `(event_id, tg_id)` | SundayRSVP |
| `kv` | `key PK, value` | Script Properties set at runtime (TG_MODERATORS, TG_MOD_CODE, TG_SECRET, TG_BOT_USERNAME) |

Invariants:
- `history` is append-only, and `records.version` equals the latest `history.version` for that row.
- Every write runs in one DB transaction, replacing the global `LockService` lock. Use `SELECT … FOR UPDATE` on the record row.

## 7. Moderation

### 7.1 Apply
Used for direct mode and for approval:
- If `current_version != base_version`, the result is a conflict.
- Otherwise write `history` (action create, update or delete; before; after), then upsert `records` with `version = base_version + 1`.
- Idempotent by `mutation_id`.

### 7.2 Notify on a new proposal
Send a Telegram message to each moderator in `kv.TG_MODERATORS` containing:
- the table, the kind of change (new card, edit or delete request), title, club and date;
- the stage change and the list of changed field labels (labels as in `TG_LABELS`);
- the first 500 characters of `what`;
- the author.

Buttons: `✅ Одобрить` / `❌ Отклонить`, with callback data `a:<proposalRef>:<hash[0:10]>` / `r:…`. In Python, `proposalRef` = proposal id instead of the sheet row number.

### 7.3 Review
Moderators can approve or reject from two places:
- **Telegram:** the callback buttons, allowed only for ids in `TG_MODERATORS`.
- **Web page `/admin`:** replaces the Sheet menu. Access is limited to emails in `SUNDAY_MODERATOR_EMAILS` (Google OAuth or a magic link). It shows the pending queue with a before/after diff and approve/reject buttons.

State changes:
- `pending → approving → approved | conflict`
- `pending → rejected` (reason `Rejected by moderator`)

After a change, the original Telegram message is edited to show the decision and the reviewer. The author is messaged when the status is approved, rejected or conflict; for portal rows that are not deletions, the message has an «Открыть карточку» Web App button.

### 7.4 Email digest (every 5 min)
- Send one email to `SUNDAY_NOTIFICATION_EMAIL` listing new pending proposals that have not been emailed yet (up to 20 listed).
- Include a link to `/admin`.
- Then record them in `email_notified`.

## 8. Telegram

### 8.1 `initData` verification
- `secret = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)`.
- `check = hex(HMAC_SHA256(key=secret, msg=data_check_string))`.
- `data_check_string` = all pairs except `hash`, URL-decoded, sorted, and joined with `\n`.
- Reject if the result differs from `hash`, if `auth_date` is older than 7 days, or if the string is longer than 4,096 characters.
- The user is `JSON.parse(user)`.

### 8.2 Commands (private chat)
| Command | Behaviour |
|---|---|
| `/start`, `/help` | Register in `bot_users` and send the help text with an «Открыть портал» Web App button. |
| `/start ci_<code>` | QR check-in (§9.3). |
| `/start reg`, `/register` | Registration dialog (§9.1). |
| `/new` | 9-step 5W1H dialog with fields title, club, date (DD.MM, «-» = none; warns if the date is under 28 days away), what, why, who, where, how, need. It ends with a preview and the buttons `Отправить в штаб` / `Отменить`, then creates a `portal` proposal with stage `Идея` and the author's contact, and sends an «Открыть карточку» button. |
| `/ideas` | Portal rows grouped by stage in the order Идея, Одобрена, Паспорт, Утверждён, Подготовка, Отложен, Проведён; at most 30 lines; Web App button `tab=bank`. |
| `/calendar` | Next 15 dated rows (not rejected, not closed, date ≥ today); marks «сегодня» / «завтра»; Web App button `tab=cal`. |
| `/remind` | Toggle `bot_users.reminders`. |
| `/list` | For moderators or organisers: events with going and checked-in counts as buttons `l:<eventId>`; tapping one sends a CSV (§9.4). |
| `/mod <code>` | Adds the sender to `TG_MODERATORS` if the code equals `TG_MOD_CODE`. |
| `/unmod` | Removes the sender from `TG_MODERATORS`. |
| `/cancel` | Ends any running dialog and removes the reply keyboard. |

Dialog state lives in Redis or a DB table with a 6-hour TTL (today: CacheService keys `tgd<chat>` and `tgr<chat>`).

Legacy stage names are mapped when reading: `На доработке → Идея`, `Паспорт на проверке → Паспорт`, `Go → Утверждён`.

### 8.3 Setup (CLI command `setup-bot`)
1. Generate `TG_SECRET` and the 6-digit `TG_MOD_CODE` if they are missing.
2. Call `getMe` and store the username.
3. Call `setWebhook` with `allowed_updates=[message, callback_query]` and `drop_pending_updates`.
4. Call `setMyCommands` (register, new, ideas, calendar, list, remind, help).
5. Call `setChatMenuButton` with the Web App «Портал» = `TG_WEBAPP_URL`.
6. Call `setMyDescription`.
7. Print the moderator code.

## 9. Registration, RSVP and check-in

### 9.1 Registration
Five steps in the bot: name (pre-filled from Telegram), phone, email (or «-»), club, role.
- The phone comes only from a `contact` message whose `contact.user_id == from.id`. The Mini App triggers it with `WebApp.requestContact`, and it arrives at the webhook as the same kind of message.
- The profile is **complete** when it has a first name, phone, club and role.

### 9.2 RSVP
- Requires a complete profile and an event date ≥ today.
- The event id matches `^portal-[a-zA-Z0-9_-]{1,100}$`, and the date is `YYYY-MM-DD`.
- Event data (title, date, club) comes from the client, because some events exist only as seed data in the front-end.
- Calls are idempotent per `(event_id, tg_id)`:
  - Going creates a row (random 12-hex code) or re-activates an existing one.
  - Cancelling sets the status to `cancelled` and keeps the title and club.
- After the change, the bot messages the user:
  - when they sign up: the QR photo with a confirmation;
  - when they cancel: a cancellation notice.
- The QR encodes `https://t.me/<bot>?start=ci_<code>`. Generate the image locally (e.g. `segno`) instead of using api.qrserver.com.

### 9.3 Check-in
Allowed for moderators, and for contacts with role `Организатор` whose club (case-insensitive) is a substring of the event's club.

Bot replies:
- the scanner is the attendee themselves: «покажите QR организатору»;
- the registration was cancelled: a warning;
- the attendee is already checked in: the time of the earlier check-in;
- otherwise: set `checked_in_at` and `checked_by`, reply «✅ Name отмечен · Пришли X из Y», and notify the attendee.

### 9.4 CSV
- Separator `;`, UTF-8 with BOM, CRLF line endings.
- Columns: Имя, Фамилия, Telegram, Телефон, Email, Клуб, Роль, Статус (идёт/отменил), Записался, Отмечен на входе.
- Dates are formatted `dd.MM.yyyy HH:mm`.

## 10. Configuration (environment variables)
`DATABASE_URL, TG_BOT_TOKEN, TG_WEBAPP_URL, PUBLIC_BASE_URL, OPENROUTER_KEY, SUNDAY_MODERATION (4 = on), SUNDAY_MODERATOR_EMAILS (JSON list), SUNDAY_NOTIFICATION_EMAIL, SMTP_* , TZ=Asia/Almaty`.

## 11. Scheduled jobs (TZ Asia/Almaty)
- **Every 5 min:** email digest (§7.4).
- **Daily at 09:00:**
  1. All subscribers with reminders on: events 7 days and 1 day away whose stage is Одобрена, Паспорт, Утверждён or Подготовка.
  2. Authors (from `authors`), per card:
     - stage Идея and 31 days to the event: «через 3 дня закрывается приём»;
     - stage Одобрена or Паспорт and 14 days to the event: «паспорт не отправлен».
  3. Moderators: the count of proposals pending for more than 24 hours, plus stage-Идея cards whose event is 0–27 days away.
  4. Tomorrow's RSVPs: send each attendee the QR again; send moderators and the club's organisers the CSV.

## 12. Acceptance criteria
1. The Portal and Workspace, with only `SYNC_URL` changed, load data, save drafts, submit proposals, and receive approved, rejected and conflict statuses exactly as today.
2. A recorded set of current GET/POST request–response pairs replays against the new service with identical JSON, ignoring timestamps.
3. Replaying the same `mutationId` never creates a duplicate history row. Two concurrent proposals on one row: one is approved, the other gets a conflict.
4. Forged `initData` is rejected. A phone number sent from the web is ignored.
5. Every bot command and button in §8.2 and §9 works in a staging bot.
6. The import from the current Google Sheet reproduces the same GET `/` output as Apps Script at cut-over.
7. The jobs in §11 fire once per period (protected against duplicates if the scheduler restarts).
8. p95 response time is under 500 ms for GET `/` with 5,000 records.

## 13. Suggested stack (not mandatory)
- FastAPI, SQLAlchemy 2 and Alembic, PostgreSQL 16.
- httpx for the Telegram and OpenRouter calls (raw Bot API, or aiogram 3 in webhook mode).
- APScheduler, or a systemd timer or cron, with a DB lock.
- `segno` for QR codes, Redis or a DB table for dialog state.
- Docker, deployed on any VPS or a managed platform with HTTPS.

## 14. Cut-over plan
1. Run the staging service with a separate test bot.
2. Freeze writes (switch Apps Script to read-only, or announce a 15-minute window).
3. Run the import.
4. Point the production bot's webhook at the new URL with `setup-bot`.
5. Change `SYNC_URL` in both front-ends, rebuild, and push to GitHub Pages.
6. Keep the Apps Script deployment read-only for 2 weeks as a rollback.

## 15. Open decisions for the owner
- Keep the Google Sheet as a read-only mirror for staff (nightly export) or not.
- `/admin` login method: Google OAuth or an email magic link.
- Hosting provider and domain.
