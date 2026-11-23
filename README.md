# DriveTelegramBot
Telegram bot saves any file sent to it into a Google Drive folder. Creates folder automatically.

## Features
- Any file type (voice, audio, video, photo, documents)
- Multiple files at once (albums / many messages) — bot asks a name for each
- `/skip` default name for current file, `/skipall` default names for all queued
- Rename after upload (inline button)
- Status: `/status` or the "📊 الحالة" button
- Each Telegram user gets a separate managed Drive root folder (under the bot's existing main Drive folder)
- Compact Arabic reply keyboard: 📤 رفع ملف and 📁 ملفاتي; recent files and search are inside the browser, with instructions under /help; folder controls are contextual within the browser (legacy /newfolder and /status remain).
- One-tap original-name upload, optional rename, one-tap bulk save for pending files, /cancel for prompts, and retry after failed uploads without re-sending.
- Browse folders/files with inline buttons, select upload destination, create nested folders, rename, and move items to Google Drive Trash after confirmation
- `/files` browse your managed files, `/newfolder <name>` create inside the selected folder and switch to it, `/folder` open the file browser
- The upload picker opens in the last selected folder and shows folders only. "✅ احفظ هون بالاسم الأصلي" saves there with one tap, and the selection is persisted in `drivebot.settings` across Render restarts.
- The file browser has a shortcut to the active upload folder, plus folder rename, back/home navigation and file links
- `/skip` and `/skipall` still work; file naming now also has inline buttons

## User folder security and deletion
- Every new folder and upload has app-private Google Drive `appProperties.tg_owner` metadata with the Telegram user ID. All browse, select, rename and trash operations re-check ownership and parent ancestry on Drive.
- The main folder for each user cannot be deleted from the bot. Folder deletion is confirmed and moves the folder and its contents to Google Drive Trash (recoverable by the connected Google account).
- Users operate through **private Telegram chats** for file management. Uploads from groups still use the sender's own selected folder.
- **Important:** these are logically isolated folders within the **single Google Drive account connected to this bot**. This feature does not grant each user access to their own separate Google Drive account.
- Older files uploaded before this feature remain unchanged in the legacy shared folder and are **not** automatically associated with a Telegram user. This avoids accidentally claiming another user's files.
- The root and selected folders are persisted in the existing `drivebot.settings` table; no new environment variables or database tables are needed.
- The file browsing menu displays up to the first 1,000 items per folder; large folders may require a future paging enhancement.

## Admin panel
Set `ADMIN_IDS` (comma-separated Telegram IDs, get yours from @userinfobot).
- `/admin`: stats, user list (last seen, upload count), ban / unban / approve buttons
- Approval mode: new users blocked until admin approves (toggle in panel)
- Admins get a notification on each new user; `/ban <id>`, `/unban <id>`
- Users stored in Supabase (`drivebot.users`)

## Setup
1. Create bot with @BotFather -> `BOT_TOKEN`
2. Google Cloud Console -> enable Drive API -> OAuth client (Desktop app) -> `credentials.json`
3. Run `pip install -r requirements.txt` and `python drive_auth.py` locally next to `credentials.json`. Sign in to the Drive account that owns the bot’s files. This writes `token.json` without starting Telegram. If Google later reports `invalid_grant`, repeat this step with the same OAuth client and account, replace `GOOGLE_TOKEN_JSON` on the active Render service and redeploy.
4. Database: schema `drivebot` (tables `users`, `settings`) in Supabase Postgres. Use the Session pooler connection string as `DATABASE_URL`.
5. Deploy (Railway): start command `python bot.py`, set variables:

| Var | Value |
|---|---|
| `BOT_TOKEN` | Telegram token |
| `ADMIN_IDS` | your Telegram ID(s), comma-separated |
| `DATABASE_URL` | Supabase pooler URI |
| `GOOGLE_TOKEN_JSON` | full contents of `token.json` |

## Big files (> 20MB)
Telegram's cloud Bot API limits bot downloads to 20MB. To go up to 2GB, run a local Bot API server and point the bot at it:
1. Get `api_id` / `api_hash` at my.telegram.org
2. Log the bot out of the cloud API once: `https://api.telegram.org/bot<TOKEN>/logOut`
3. Run the server on the same machine as the bot:
   `telegram-bot-api --api-id=ID --api-hash=HASH --local --dir=$HOME/tgapi --http-port=8081`
   (NixOS: `nix-shell -p telegram-bot-api`; Docker: `aiogram/telegram-bot-api`)
4. Start the bot with `BOT_API_URL=http://localhost:8081`

Without `BOT_API_URL` the limit is 20MB per file.

### Server deploy (no local server, files never pass through your own connection)
The included `Dockerfile` runs the local Bot API server + the bot in one container (`start.sh`).
Deploy it on any Docker host (Railway, Fly.io, a VM...) with these variables:
`BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`, `GOOGLE_TOKEN_JSON`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`.
Log the bot out of the cloud API once before first start (see step 2 above).
Never commit `token.json` / `credentials.json` / tokens (see `.gitignore`).

### Free hosts that sleep (Render free, etc.)
If `PORT` is set, the bot serves `GET /health` -> `ok`. Free web services sleep after ~15 min without HTTP traffic and the polling bot stops with them, so point a free pinger (UptimeRobot / cron-job.org) at `https://<your-service>/health` every 5-10 min. Note: the per-user queue lives in memory, so a restart drops files still waiting for a name.
