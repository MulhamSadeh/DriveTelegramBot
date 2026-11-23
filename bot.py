"""DriveTelegramBot: send files to a Telegram bot -> saved to a Google Drive folder.

Env vars:
  BOT_TOKEN          Telegram bot token (@BotFather)
  ADMIN_IDS          comma-separated Telegram user IDs of admins (@userinfobot)
  DATABASE_URL       Postgres connection string (Supabase pooler URI)
  GOOGLE_TOKEN_JSON  contents of token.json (authorized-user JSON w/ refresh token)
  FOLDER_NAME        optional, default "Telegram Uploads"
  BOT_API_URL        optional, self-hosted Bot API server URL (e.g. http://localhost:8081) -> files up to 2GB

Local first run (to create token.json): put credentials.json next to bot.py and run;
a browser opens for Google login. Then paste token.json contents into GOOGLE_TOKEN_JSON on the server.

Flow:
  - send 1+ files -> bot asks name for each (/skip, /skipall = default names)
  - after upload: button to rename the Drive file
  - status: /status or "📊 الحالة" button
  - /newfolder <name>, /folder
  - admin: /admin (users, ban/unban, approval mode), /ban <id>, /unban <id>
Download limit: 20MB (cloud Bot API) or 2GB with BOT_API_URL (local Bot API server).
"""
import asyncio, os, re, json, datetime, mimetypes, tempfile, traceback, time
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler,
    TypeHandler, ApplicationHandlerStop, ContextTypes, filters,
)
from telegram.error import NetworkError, TimedOut, BadRequest
from googleapiclient.errors import HttpError
from user_drive import UserDrive, FOLDER_MIME, FolderAccessError, safe_name
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_FOLDER = os.environ.get("FOLDER_NAME", "Telegram Uploads")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
STATUS_BTN = "📊 الحالة"
FILES_BTN = "📁 ملفاتي"
NEW_FOLDER_BTN = "➕ مجلد جديد"
UPLOAD_BTN = "📤 رفع ملف"
HELP_BTN = "❓ طريقة الاستخدام"
RECENT_BTN = "🕘 آخر ملفاتي"
SEARCH_BTN = "🔎 بحث عن ملف"
# Optional: URL of a self-hosted Telegram Bot API server (run with --local) -> files up to 2GB.
# Without it, Telegram's cloud Bot API limits downloads to 20MB.
BOT_API_URL = os.environ.get("BOT_API_URL", "").rstrip("/")
MAX_BYTES = (2000 if BOT_API_URL else 20) * 1024 * 1024
DRIVE_LOCK = asyncio.Lock()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}


# ---------- database (Supabase Postgres, schema "drivebot") ----------
def q(sql, args=(), fetch=None):
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
    finally:
        conn.close()


# Most folder operations read the same settings repeatedly. Small TTL = fewer
# cross-region Postgres connections while still noticing external changes.
_SETTINGS_CACHE = {}


def get_setting(key, default=None):
    now = time.monotonic()
    cached = _SETTINGS_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1] if cached[1] is not None else default
    r = q("select value from drivebot.settings where key=%s", (key,), "one")
    value = r["value"] if r else None
    _SETTINGS_CACHE[key] = (now + 45, value)
    return value if value is not None else default


def set_setting(key, value):
    q(
        "insert into drivebot.settings (key, value) values (%s, %s) "
        "on conflict (key) do update set value = excluded.value",
        (key, value),
    )
    _SETTINGS_CACHE[key] = (time.monotonic() + 45, value)


def touch_user(u, default_status):
    return q(
        "insert into drivebot.users (id, name, username, status) values (%s, %s, %s, %s) "
        "on conflict (id) do update set name = excluded.name, username = excluded.username, last_seen = now() "
        "returning *, (xmax = 0) as is_new",
        (u.id, u.full_name, u.username, default_status),
        "one",
    )


_USER_GATE_CACHE = {}


def set_status(uid, status):
    result = q("update drivebot.users set status=%s where id=%s returning *", (status, int(uid)), "one")
    _USER_GATE_CACHE.pop(int(uid), None)
    return result


def inc_uploads(uid):
    q("update drivebot.users set uploads = uploads + 1 where id=%s", (uid,))


def label(rec):
    return f"@{rec['username']}" if rec.get("username") else rec["name"]


# ---------- Google Drive ----------
def drive_service():
    creds = None
    tok = os.environ.get("GOOGLE_TOKEN_JSON")
    if tok:
        creds = Credentials.from_authorized_user_info(json.loads(tok), SCOPES)
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
        if not tok:
            with open("token.json", "w") as f:
                f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


drive = drive_service()


def make_folder(name):
    r = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id"
    ).execute()
    set_setting("folder_id", r["id"])
    return r["id"]


def folder_id():
    return get_setting("folder_id") or make_folder(DEFAULT_FOLDER)


def folder_url():
    return f"https://drive.google.com/drive/folders/{folder_id()}"


USER_DRIVE = UserDrive(drive, get_setting, set_setting, folder_id)


def clean(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def with_ext(name, ext):
    name = clean(name)
    return name if (not ext or name.lower().endswith(ext.lower())) else name + ext


# ---------- UI helpers ----------
def drive_file_url(file_id):
    """Link to the exact Google Drive file; does not change sharing permissions."""
    return f"https://drive.google.com/file/d/{file_id}/view"


def kb_after_upload(file_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 فتح الملف", url=drive_file_url(file_id))],
        [InlineKeyboardButton("✏️ غيّر الاسم", callback_data=f"ren:{file_id}"),
         InlineKeyboardButton("🗑 حذف", callback_data=f"trash:{file_id}")],
        [InlineKeyboardButton(FILES_BTN, callback_data="home"),
         InlineKeyboardButton(STATUS_BTN, callback_data="status")],
    ])


def kb_status():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ حفظ بالاسم الأصلي", callback_data="skipone"),
         InlineKeyboardButton("⚡ حفظ الباقي", callback_data="skipall")],
        [InlineKeyboardButton("❌ إلغاء هالملف", callback_data="upcancel_name")],
    ])

def status_text(ud, folder_name=None):
    qd = ud.get("queue", [])
    up = ud.get("uploaded", [])
    lines = [f"بالانتظار: {len(qd)}", f"تم الرفع بهالجلسة: {len(up)}"]
    if qd:
        lines.append(f"التالي: {qd[0]['label']}")
    if up:
        lines.append(f"آخر ملف: {up[-1]['name']}")
        lines.append(drive_file_url(up[-1]["id"]))
    if folder_name:
        lines.append(f"📁 مكان الرفع: {folder_name}")
    return "\n".join(lines)


async def ask_next(ctx, chat_id, ud):
    queue = ud.get("queue", [])
    if not queue:
        return
    item = queue[0]
    if not item.get("target_folder"):
        item["pending_count"] = len(queue)
        await _show_upload_folders(ctx, chat_id, item)
        return
    await ctx.bot.send_message(
        chat_id,
        f"📁 {item['target_name']}\n"
        f"✏️ اكتب اسم «{item['label']}» الجديد، أو احفظ بالاسم الأصلي.\n"
        "للإلغاء: /cancel",
        reply_markup=kb_status(),
    )


def _upload_folder_snapshot(uid, display, folder_id):
    USER_DRIVE.ensure_defaults(uid, display)
    root, folder, breadcrumbs = USER_DRIVE.folder_path(uid, folder_id, display)
    items, more = USER_DRIVE.list_children(uid, folder["id"], display)
    folders = [i for i in items if i.get("mimeType") == FOLDER_MIME]
    files = [i for i in items if i.get("mimeType") != FOLDER_MIME]
    preferred = {"فويسات": 0, "محاضرات": 1, "ملفات عامة": 2}
    folders.sort(key=lambda f: (preferred.get(f["name"], 9), f["name"].casefold()))
    return root, folder, folders, files, more, breadcrumbs

async def _show_upload_folders(ctx, chat_id, item, folder_id=None, page=0, cq=None):
    if not folder_id:
        # Start where the user last saved a file, including after a bot restart.
        recent = await _drive_call(USER_DRIVE.current, item["uid"], item["owner_name"])
        item["recent_target"] = recent
        folder_id = recent["id"]
    root, folder, folders, files, more, breadcrumbs = await _drive_call(
        _upload_folder_snapshot, item["uid"], item["owner_name"], folder_id,
    )
    token = str(item["token"])
    # This picker selects a destination; files remain available under 'ملفاتي'.
    page_size = 7
    last = max(0, (len(folders) - 1) // page_size)
    page = max(0, min(int(page), last))
    rows = []

    nav = []
    if folder["id"] != root["id"]:
        parent_id = (folder.get("parents") or [root["id"]])[0]
        nav.append(InlineKeyboardButton(
            "⬅️ رجوع", callback_data=f"upnav:{token}:{parent_id}",
        ))
        nav.append(InlineKeyboardButton(
            "🏠 الرئيسية", callback_data=f"upnav:{token}:{root['id']}",
        ))
    if nav:
        rows.append(nav)

    quick = item.get("recent_target")
    if quick and quick["id"] != folder["id"]:
        rows.append([InlineKeyboardButton(
            f"📌 مجلد الرفع الحالي: {quick['name'][:22]}",
            callback_data=f"upset:{token}:{quick['id']}",
        )])

    for child in folders[page * page_size: (page + 1) * page_size]:
        rows.append([InlineKeyboardButton(
            f"📂 {child['name'][:38]}", callback_data=f"upnav:{token}:{child['id']}",
        )])

    pager = []
    if page:
        pager.append(InlineKeyboardButton(
            "⬅️ السابق", callback_data=f"uppage:{token}:{folder['id']}:{page - 1}",
        ))
    if page < last:
        pager.append(InlineKeyboardButton(
            "التالي ➡️", callback_data=f"uppage:{token}:{folder['id']}:{page + 1}",
        ))
    if pager:
        rows.append(pager)

    rows.append([InlineKeyboardButton(
        "⚡ حفظ بالاسم الأصلي", callback_data=f"upfast:{token}:{folder['id']}",
    )])
    rows.append([InlineKeyboardButton(
        "✏️ تغيير الاسم قبل الحفظ", callback_data=f"upset:{token}:{folder['id']}",
    )])
    if item.get("pending_count", 1) > 1:
        rows.append([InlineKeyboardButton(
            f"⚡ حفظ كل الملفات هون ({item['pending_count']})",
            callback_data=f"upbatch:{token}:{folder['id']}",
        )])
    rows.append([InlineKeyboardButton(
        "➕ مجلد جديد هون", callback_data=f"upadd:{token}:{folder['id']}",
    )])
    rows.append([InlineKeyboardButton(
        "❌ إلغاء هالملف", callback_data=f"upcancel:{token}",
    )])

    path = " / ".join(breadcrumbs)
    if len(path) > 130:
        path = "… / " + " / ".join(breadcrumbs[-3:])
    title = (
        f"وين بدك تحفظ «{item['label']}»؟\n"
        f"📍 {path}\n"
        f"📂 مجلدات: {len(folders)} | 📄 ملفات: {len(files)}\n\n"
        + ("افتح المجلد لتشوف جوّاته. الحفظ بالمجلد المكتوب فوق."
           if folders else "ما في مجلدات فرعية. احفظ هون أو أنشئ مجلد.")
        + ("\n(عم نعرض أول 1000 عنصر)" if more else "")
    )
    markup = InlineKeyboardMarkup(rows)
    if cq is None:
        await ctx.bot.send_message(chat_id, title, reply_markup=markup)
    else:
        try:
            await cq.edit_message_text(title, reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                await ctx.bot.send_message(chat_id, title, reply_markup=markup)

async def _safe_callback_answer(cq, text=None, show_alert=False):
    try:
        await cq.answer(text=text, show_alert=show_alert)
        return True
    except BadRequest as exc:
        if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
            return False
        raise
    except (TimedOut, NetworkError):
        return False


async def _finish_queued_file(ctx, chat_id, user_data, chosen_name):
    queue = user_data.get("queue", [])
    if not queue or not queue[0].get("target_folder"):
        return False
    item = queue[0]
    if item.get("_uploading"):
        return False
    item["_uploading"] = True
    item["retry_name"] = chosen_name
    try:
        done = await upload_item(ctx, chat_id, user_data, item, chosen_name)
        if done:
            if queue and queue[0] is item:
                queue.pop(0)
            return True
        return False
    finally:
        item.pop("_uploading", None)


async def on_upload_folder_button(update, ctx):
    cq = update.callback_query
    if not await _safe_callback_answer(cq):
        return
    if not await _private_ui(update):
        return
    queue = ctx.user_data.get("queue", [])
    if not queue:
        await cq.message.reply_text("ما في ملفات ناطرة.")
        return
    item = queue[0]
    parts = cq.data.split(":")
    action, token = parts[0], parts[1]
    if str(item["token"]) != token or item["uid"] != update.effective_user.id:
        await cq.message.reply_text("هالزر قديم. استعمل آخر رسالة من البوت.")
        return
    chat_id = cq.message.chat_id
    ud = ctx.user_data
    if action == "upcancel":
        if item.get("_uploading"):
            await cq.message.reply_text("الملف قيد الرفع، ما فينا نلغيه هلق.")
            return
        queue.pop(0)
        ud.pop("creating_for_upload", None)
        ud.pop("creating_folder_in", None)
        try:
            await cq.edit_message_text("❌ ألغيت هالملف من قائمة الانتظار.")
        except BadRequest:
            pass
        await ask_next(ctx, chat_id, ud)
        return
    if action == "upretry":
        if not item.get("target_folder"):
            await cq.message.reply_text("اختار المجلد أولاً.")
            return
        if await _finish_queued_file(ctx, chat_id, ud, item.get("retry_name")):
            await ask_next(ctx, chat_id, ud)
        return
    if item.get("target_folder"):
        await cq.message.reply_text("هالاختيار صار قديم. استعمل آخر رسالة من البوت.")
        return
    uid, display = item["uid"], item["owner_name"]
    if action == "uppage":
        fid, page = parts[2], int(parts[3])
        await _show_upload_folders(ctx, chat_id, item, fid, page, cq)
    elif action == "upnav":
        await _show_upload_folders(ctx, chat_id, item, parts[2], 0, cq)
    elif action in ("upset", "upfast", "upbatch"):
        selected = await _drive_call(USER_DRIVE.select, uid, parts[2], display)
        item["recent_target"] = selected
        item["target_folder"] = selected["id"]
        item["target_name"] = selected["name"]
        try:
            await cq.edit_message_text(f"📁 المجلد: «{selected['name']}»")
        except BadRequest:
            pass
        if action == "upset":
            await ask_next(ctx, chat_id, ud)
        elif action == "upfast":
            if await _finish_queued_file(ctx, chat_id, ud, None):
                await ask_next(ctx, chat_id, ud)
        else:
            total = len(queue)
            await cq.message.reply_text(f"⚡ عم احفظ {total} ملفات في «{selected['name']}».")
            while queue:
                pending = queue[0]
                pending["target_folder"] = selected["id"]
                pending["target_name"] = selected["name"]
                if not await _finish_queued_file(ctx, chat_id, ud, None):
                    return  # Failed file remains in queue with an explicit retry button.
            await cq.message.reply_text(f"✅ انحفظت الملفات الـ {total} بنجاح.")
    elif action == "upadd":
        await _drive_call(USER_DRIVE.require_folder, uid, parts[2], display)
        ud.pop("renaming", None)
        ud["creating_folder_in"] = parts[2]
        ud["creating_for_upload"] = token
        await cq.message.reply_text("➕ اكتب اسم المجلد الجديد (أو /cancel للإلغاء):")

# ---------- upload ----------
def _format_eta(seconds):
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانية"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} دقيقة و{seconds} ثانية"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ساعة و{minutes} دقيقة"


def _upload_progress_text(name, uploaded, total, elapsed):
    fraction = min(1.0, max(0.0, uploaded / total)) if total else 0.0
    percent = int(fraction * 100)
    blocks = int(fraction * 12)
    bar = "█" * blocks + "░" * (12 - blocks)
    if uploaded > 0 and uploaded < total:
        remaining = (total - uploaded) * elapsed / uploaded
        eta = f"⏳ باقي تقريباً: {_format_eta(remaining)}"
    elif uploaded >= total and total > 0:
        eta = "عم أتأكد من حفظ الملف..."
    else:
        eta = "عم احسب الوقت المتبقي..."
    mb = 1024 * 1024
    short_name = name if len(name) <= 65 else name[:62] + "…"
    return (
        f"عم ارفع «{short_name}» على Drive\n"
        f"{bar} {percent}%\n"
        f"{uploaded / mb:.1f} / {total / mb:.1f} MB\n"
        f"{eta}"
    )


async def _edit_upload_status(message, text, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
        return True
    except Exception as exc:
        print(f"Could not update progress message: {type(exc).__name__}")
        return False


async def _get_telegram_file(bot, file_id, status_msg):
    """A local Bot API server may need minutes to prepare a large Telegram file."""
    for attempt in range(3):
        started = time.monotonic()
        task = asyncio.create_task(bot.get_file(
            file_id, read_timeout=600, connect_timeout=30, pool_timeout=30,
        ))
        while not task.done():
            await asyncio.wait({task}, timeout=12)
            if not task.done():
                elapsed = int(time.monotonic() - started)
                await _edit_upload_status(
                    status_msg,
                    f"عم نزّل الملف من Telegram... مرّت {elapsed} ثانية.\n"
                    "بس يجهز الملف، بفرجيك نسبة الرفع.",
                )
        try:
            return await task
        except (TimedOut, NetworkError) as exc:
            print(f"Telegram get_file attempt {attempt + 1}/3 failed: {type(exc).__name__}", flush=True)
            if attempt == 2:
                raise
            await _edit_upload_status(
                status_msg, f"🔄 اتصال Telegram تأخر، عم جرّب مرة {attempt + 2} من 3...",
            )
            await asyncio.sleep(2 ** (attempt + 1))


async def _download_telegram_file(tg_file, path, status_msg):
    for attempt in range(3):
        try:
            await tg_file.download_to_drive(
                path, read_timeout=600, connect_timeout=30, pool_timeout=30,
            )
            return
        except (TimedOut, NetworkError) as exc:
            print(f"Telegram download attempt {attempt + 1}/3 failed: {type(exc).__name__}", flush=True)
            if attempt == 2:
                raise
            await _edit_upload_status(
                status_msg, f"🔄 تنزيل الملف انقطع، عم جرّب مرة {attempt + 2} من 3...",
            )
            await asyncio.sleep(2 ** (attempt + 1))


async def upload_item(ctx, chat_id, ud, item, chosen_name):
    name = with_ext(chosen_name, item["ext"]) if chosen_name else item["default_name"]
    status_msg = await ctx.bot.send_message(chat_id, f"عم جهّز «{name[:65]}» للرفع...")
    tmp_path = None
    try:
        tg_file = await _get_telegram_file(ctx.bot, item["file_id"], status_msg)
        parent = (await _drive_call(
            USER_DRIVE.require_folder, item["uid"], item["target_folder"],
            item.get("owner_name", "مستخدم"),
        ))["id"]
        # With local Bot API, get_file() already returns a shared on-disk path.
        # Reuse it instead of duplicating multi-GB files on Render's ephemeral disk.
        local_path = tg_file.file_path
        if BOT_API_URL and local_path and os.path.isabs(local_path) and os.path.isfile(local_path):
            upload_path = local_path
        else:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=item["ext"] or ".bin")
            tmp_path = tmp.name
            tmp.close()
            await _download_telegram_file(tg_file, tmp_path, status_msg)
            upload_path = tmp_path
        total_bytes = os.path.getsize(upload_path)
        state = {"uploaded": 0}  # One writer (upload thread), one reader (event loop).

        def _create():
            media = MediaFileUpload(
                upload_path, mimetype=item["mime"], resumable=True,
                chunksize=8 * 1024 * 1024,
            )
            request = drive.files().create(
                body={"name": name, "parents": [parent],
                      "appProperties": {"tg_owner": str(item["uid"]), "tg_role": "file"}},
                media_body=media, fields="id"
            )
            result = None
            while result is None:
                progress, result = request.next_chunk(num_retries=3)
                if progress is not None:
                    state["uploaded"] = min(total_bytes, progress.resumable_progress)
            state["uploaded"] = total_bytes
            return result

        await _edit_upload_status(status_msg, "الملف جاهز، عم استنى بدء الرفع...")
        async with DRIVE_LOCK:
            started = time.monotonic()
            worker = asyncio.create_task(asyncio.to_thread(_create))
            last_text = None
            while not worker.done():
                body = _upload_progress_text(name, state["uploaded"], total_bytes, time.monotonic() - started)
                if body != last_text:
                    await _edit_upload_status(status_msg, body)
                    last_text = body
                await asyncio.wait({worker}, timeout=4)
            result = await worker

        ud.setdefault("uploaded", []).append({"id": result["id"], "name": name})
        try:
            await asyncio.to_thread(inc_uploads, item["uid"])
        except Exception as exc:
            print(f"Upload counter update failed: {type(exc).__name__}")
        await _edit_upload_status(status_msg, "✅ اكتمل الرفع إلى Drive (100%)")
        try:
            await ctx.bot.send_message(
                chat_id,
                f"✅ انحفظ الملف على Google Drive\n"
                f"📄 {name}\n"
                f"📁 المجلد: {item['target_name']}\n"
                f"🔗 {drive_file_url(result['id'])}",
                reply_markup=kb_after_upload(result["id"]),
            )
        except Exception:
            traceback.print_exc()  # Upload succeeded even if notification failed.
        return True
    except Exception as exc:
        traceback.print_exc()
        if isinstance(exc, (TimedOut, NetworkError)):
            notice = "اتصال Telegram تأخر. جرّب مرة تانية."
        elif isinstance(exc, HttpError):
            notice = "Drive ما ردّ هالمرة. جرّب بعد شوي."
        else:
            notice = "ما زبط الرفع هالمرة. جرّب مرة تانية."
        await _edit_upload_status(
            status_msg, f"{notice}\nالملف بعدها بالدور، ما بدك تبعته من جديد.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 إعادة المحاولة",
                                     callback_data=f"upretry:{item['token']}"),
            ]]),
        )
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------- handlers ----------
def _welcome_text():
    return (
        "☕ أهلاً فيك ببوت Ctrl+Tea\n\n"
        "احفظ ملفاتك والفويسات على Google Drive بمجلدات خاصة فيك.\n"
        "📤 ابعت ملف أو فويس، اختار المجلد، واضغط «حفظ بالاسم الأصلي» "
        "أو غيّر الاسم.\n"
        "📁 من «ملفاتي» بتتصفّح المجلدات وبتوصل لروابط ملفاتك.\n\n"
        "— فريق Ctrl+Tea"
    )

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        _welcome_text(), reply_markup=_menu_keyboard(update.effective_user.id),
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "❓ طريقة الاستخدام\n\n"
        "📤 ابعت ملف أو صورة أو فويس للبوت.\n"
        "اختار المجلد، واضغط «⚡ حفظ بالاسم الأصلي»، أو غيّر الاسم قبل الرفع.\n"
        "فيك تحفظ كل الملفات المنتظرة بنفس المجلد بضغطة.\n\n"
        "📁 ملفاتي: افتح مجلداتك، اضغط على الملف لتشوف تفاصيله، "
        "أو على 🔗 حتى تفتح رابطه مباشرة.\n"
        "➕ لإنشاء مجلد، افتح المكان المطلوب واضغط «مجلد جديد».\n"
        "🕘 آخر ملفاتي، 🔎 بحث عن ملف، /status للحالة.\n"
        "للإلغاء: /cancel\n"
        "الحذف بيحتاج تأكيد، والملفات بتنقل لسلة Drive.",
        reply_markup=_menu_keyboard(update.effective_user.id),
    )

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    person = update.effective_user
    active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
    await update.effective_message.reply_text(
        status_text(ctx.user_data, active["name"]), reply_markup=_menu_keyboard(person.id),
    )


async def newfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _private_ui(update):
        return
    person = update.effective_user
    active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if name:
        await _create_and_select(update, ctx, active["id"], name)
    else:
        ctx.user_data.pop("renaming", None)
        ctx.user_data["creating_folder_in"] = active["id"]
        await update.effective_message.reply_text("شو بدك تسمي المجلد؟")


async def folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_browser(update, ctx)

async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if not await _private_ui(update):
        return
    ud = ctx.user_data
    att = m.effective_attachment
    if isinstance(att, (list, tuple)):  # photo -> biggest size
        att = att[-1]
    if not hasattr(att, "file_id"):
        return
    size = getattr(att, "file_size", None)
    if size and size > MAX_BYTES:
        await m.reply_text(
            f"❌ الملف كبير ({size / 1024 / 1024:.0f}MB). الحد الحالي {MAX_BYTES // 1024 // 1024}MB."
        )
        return

    mime = getattr(att, "mime_type", None)
    orig = getattr(att, "file_name", None)
    if m.photo:
        mime, ext, kind = "image/jpeg", ".jpg", "صورة"
    elif m.voice:
        mime, ext, kind = mime or "audio/ogg", ".ogg", "فويس"
    elif m.video_note:
        mime, ext, kind = "video/mp4", ".mp4", "فيديو"
    else:
        ext = (os.path.splitext(orig)[1] if orig else "") or mimetypes.guess_extension(mime or "") or ""
        kind = "ملف"
    mime = mime or "application/octet-stream"

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    u = m.from_user
    if orig:
        default_name = clean(orig)
    else:
        prefix = "فويس" if m.voice else ("صوت" if m.audio else ("صورة" if m.photo else "ملف"))
        default_name = f"{prefix}_{ts}_{m.message_id}{ext}"
    item = {
        "file_id": att.file_id,
        "owner_name": u.full_name,
        "token": str(m.message_id),
        "target_folder": None,
        "target_name": None,
        "uid": u.id,
        "mime": mime,
        "ext": ext,
        "label": orig or f"{kind} {m.message_id}",
        "default_name": default_name,
    }
    queue = ud.setdefault("queue", [])
    was_empty = not queue
    queue.append(item)

    if was_empty:
        await ask_next(ctx, m.chat_id, ud)
    elif not m.media_group_id or ud.get("last_group") != m.media_group_id:
        ud["last_group"] = m.media_group_id
        await m.reply_text(f"تمام، صار عندك {len(queue)} ملفات بالدور.", reply_markup=kb_status())


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    chat_id = update.message.chat_id
    text = update.message.text
    person = update.effective_user

    if text == FILES_BTN:
        ud.pop("renaming", None)
        ud.pop("creating_folder_in", None)
        ud.pop("searching_files", None)
        await user_files(update, ctx)
        return
    if text == RECENT_BTN:
        ud.pop("searching_files", None)
        await recent_command(update, ctx)
        return
    if text == SEARCH_BTN:
        await search_command(update, ctx)
        return
    if text == NEW_FOLDER_BTN:
        if not await _private_ui(update):
            return
        active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
        ud.pop("renaming", None)
        ud["creating_folder_in"] = active["id"]
        await update.message.reply_text("شو بدك تسمي المجلد؟")
        return
    if text == UPLOAD_BTN:
        await upload_help(update, ctx)
        return
    if text == STATUS_BTN:
        await status(update, ctx)
        return
    if text == HELP_BTN:
        await help_command(update, ctx)
        return

    if ud.pop("searching_files", False):
        try:
            await _show_file_results(update, ctx, text)
        except ValueError as exc:
            ud["searching_files"] = True
            await update.message.reply_text(str(exc))
        return

    if ud.get("creating_folder_in"):
        if not await _private_ui(update):
            return
        parent = ud["creating_folder_in"]
        token = ud.get("creating_for_upload")
        if token:
            queue = ud.get("queue", [])
            if not queue or str(queue[0].get("token")) != token:
                ud.pop("creating_for_upload", None)
                ud.pop("creating_folder_in", None)
                await update.message.reply_text("هالطلب انتهى. ابعت الملف من جديد.")
                return
            made = await _drive_call(USER_DRIVE.create_folder, person.id, parent, text, person.full_name)
            await _drive_call(USER_DRIVE.select, person.id, made["id"], person.full_name)
            queue[0]["target_folder"] = made["id"]
            queue[0]["target_name"] = made["name"]
            ud.pop("creating_for_upload", None)
            ud.pop("creating_folder_in", None)
            await update.message.reply_text(f"عملت مجلد «{made['name']}».")
            await ask_next(ctx, chat_id, ud)
        else:
            await _create_and_select(update, ctx, parent, text)
            ud.pop("creating_folder_in", None)
        return

    if ud.get("renaming"):
        if not await _private_ui(update):
            return
        fid = ud["renaming"]
        old = await _drive_call(USER_DRIVE.require_item, person.id, fid, person.full_name)
        name = safe_name(text)
        if old.get("mimeType") != FOLDER_MIME:
            name = with_ext(name, os.path.splitext(old.get("name", ""))[1])
        renamed = await _drive_call(USER_DRIVE.rename, person.id, fid, name, person.full_name)
        ud.pop("renaming", None)
        for x in ud.get("uploaded", []):
            if x["id"] == fid:
                x["name"] = renamed["name"]
        if old.get("mimeType") == FOLDER_MIME:
            await update.message.reply_text(f"✅ صار اسم المجلد «{renamed['name']}».")
            await show_browser(update, ctx, fid)
        else:
            await update.message.reply_text(
                f"تغيّر الاسم لـ «{renamed['name']}».\n"
                f"رابطه: {drive_file_url(fid)}",
                reply_markup=kb_after_upload(fid),
            )
        await ask_next(ctx, chat_id, ud)
        return

    queue = ud.get("queue", [])
    if not queue:
        return
    if not queue[0].get("target_folder"):
        await update.message.reply_text("اختار المجلد من الأزرار بالأول.")
        return
    if await _finish_queued_file(ctx, chat_id, ud, text):
        await ask_next(ctx, chat_id, ud)

async def cancel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel current prompt or waiting item; never delete a Drive file."""
    ud = ctx.user_data
    if ud.pop("searching_files", False):
        msg = "❌ ألغيت البحث."
    elif ud.pop("renaming", None):
        msg = "❌ ألغيت تغيير الاسم."
    elif ud.pop("creating_folder_in", None):
        ud.pop("creating_for_upload", None)
        msg = "❌ ألغيت إنشاء المجلد."
    elif ud.get("queue"):
        if ud["queue"][0].get("_uploading"):
            await update.effective_message.reply_text("الرفع شغّال. ما فينا نلغيه أثناء النقل.")
            return
        ud["queue"].pop(0)
        await update.effective_message.reply_text(
            "❌ ألغيت هالملف من الانتظار.", reply_markup=_menu_keyboard())
        await ask_next(ctx, update.effective_chat.id, ud)
        return
    else:
        msg = "ما في عملية ناطرة إلغاء."
    await update.effective_message.reply_text(msg, reply_markup=_menu_keyboard())


async def skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    queue = ctx.user_data.get("queue", [])
    if not queue:
        return
    if not queue[0].get("target_folder"):
        await update.message.reply_text("اختار المجلد بالأول.")
        return
    if await _finish_queued_file(ctx, update.effective_chat.id, ctx.user_data, None):
        await ask_next(ctx, update.effective_chat.id, ctx.user_data)


async def skipall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    queue = ctx.user_data.get("queue", [])
    if not queue:
        return
    while queue and queue[0].get("target_folder"):
        if not await _finish_queued_file(ctx, update.effective_chat.id, ctx.user_data, None):
            return
    await ask_next(ctx, update.effective_chat.id, ctx.user_data)

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not await _safe_callback_answer(cq):
        return
    if cq.data == "status":
        person = update.effective_user
        active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
        await cq.message.reply_text(status_text(ctx.user_data, active["name"]))
    elif cq.data.startswith("ren:"):
        if not await _private_ui(update):
            return
        person = update.effective_user
        fid = cq.data[4:]
        meta = await _drive_call(USER_DRIVE.require_item, person.id, fid, person.full_name)
        if (meta.get("appProperties") or {}).get("tg_role") == "root":
            await cq.message.reply_text("❌ ما فيك تغيّر اسم المجلد الأساسي.")
            return
        ctx.user_data.pop("creating_folder_in", None)
        ctx.user_data["renaming"] = fid
        await cq.message.reply_text(f"✏️ شو الاسم الجديد لـ «{meta.get('name', 'الملف')}»؟")


# ---------- per-user browser ----------
def _menu_keyboard(uid=None):
    # Keep the persistent keyboard focused; contextual folder tools live inside the browser.
    rows = [[FILES_BTN, UPLOAD_BTN], [RECENT_BTN, SEARCH_BTN], [HELP_BTN]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def _drive_call(fn, *args):
    async with DRIVE_LOCK:
        return await asyncio.to_thread(fn, *args)


async def _private_ui(update):
    if update.effective_chat.type == "private":
        return True
    await update.effective_message.reply_text("افتح الخاص مع البوت لتدير ملفاتك.")
    return False


def _browser_snapshot(uid, display, folder_id):
    USER_DRIVE.ensure_defaults(uid, display)
    root = USER_DRIVE.ensure_root(uid, display)
    root, folder, breadcrumbs = USER_DRIVE.folder_path(
        uid, folder_id or root["id"], display,
    )
    active = USER_DRIVE.current(uid, display)
    items, more = USER_DRIVE.list_children(uid, folder["id"], display)
    return root, folder, active, items, more, breadcrumbs


def _item_button(label, verb, file_id):
    payload = f"{verb}:{file_id}"
    if len(payload.encode("utf-8")) > 64:
        return None
    return InlineKeyboardButton(label, callback_data=payload)


async def show_browser(update, ctx, folder_id=None, page=None):
    """Render the contents of one Drive folder inside a single Telegram message."""
    if not await _private_ui(update):
        return
    person = update.effective_user
    root, folder, current, items, more, breadcrumbs = await _drive_call(
        _browser_snapshot, person.id, person.full_name, folder_id,
    )
    limit = 8
    total_pages = max(0, (len(items) - 1) // limit)
    saved_pages = ctx.user_data.setdefault("browser_pages", {}) if ctx is not None else {}
    if page is None:
        page = saved_pages.get(folder["id"], 0)
    page = max(0, min(int(page), total_pages))
    saved_pages[folder["id"]] = page
    rows = []

    # Keep navigation at the top so it is always easy to go up a level.
    if folder["id"] != root["id"]:
        parent_id = (folder.get("parents") or [root["id"]])[0]
        rows.append([
            InlineKeyboardButton("⬅️ رجوع", callback_data=f"nav:{parent_id}"),
            InlineKeyboardButton("🏠 الرئيسية", callback_data="home"),
        ])

    if current["id"] != folder["id"]:
        rows.append([InlineKeyboardButton(
            "📌 مجلد الرفع الحالي", callback_data=f"nav:{current['id']}",
        )])

    # Every folder is a button. On tap, replace this message with its contents.
    # Every file has a detail button plus a direct Drive link.
    for meta in items[page * limit: (page + 1) * limit]:
        is_folder = meta.get("mimeType") == FOLDER_MIME
        entry = _item_button(
            ("📂 " if is_folder else "📄 ") + meta.get("name", "ملف")[:33],
            "nav" if is_folder else "item", meta["id"],
        )
        if entry:
            if is_folder:
                rows.append([entry])
            else:
                rows.append([
                    entry, InlineKeyboardButton("🔗", url=drive_file_url(meta["id"])),
                ])

    pager = []
    if page:
        pager.append(InlineKeyboardButton(
            "⬅️ السابق", callback_data=f"page:{folder['id']}:{page-1}",
        ))
    if page < total_pages:
        pager.append(InlineKeyboardButton(
            "التالي ➡️", callback_data=f"page:{folder['id']}:{page+1}",
        ))
    if pager:
        rows.append(pager)

    rows.append([
        InlineKeyboardButton("➕ مجلد جديد", callback_data=f"add:{folder['id']}"),
        InlineKeyboardButton("📌 تعيين للرفع", callback_data=f"choose:{folder['id']}"),
    ])
    rows.append([
        InlineKeyboardButton("🕘 الأحدث", callback_data="recent"),
        InlineKeyboardButton("🔎 بحث", callback_data="findprompt"),
        InlineKeyboardButton("🔄 تحديث", callback_data=f"nav:{folder['id']}"),
    ])
    if folder["id"] != root["id"]:
        rename = _item_button("✏️ تغيير اسم المجلد", "ren", folder["id"])
        deletion = _item_button("🗑 حذف المجلد", "trash", folder["id"])
        if rename and deletion:
            rows.append([rename, deletion])

    path = " / ".join(breadcrumbs)
    if len(path) > 130:
        path = "… / " + " / ".join(breadcrumbs[-3:])
    folder_count = sum(meta.get("mimeType") == FOLDER_MIME for meta in items)
    file_count = len(items) - folder_count
    summary = (
        f"📁 {path}\n"
        f"📂 {folder_count} مجلدات  |  📄 {file_count} ملفات\n"
        f"📌 الرفع حالياً: {current.get('name', '')}\n\n"
        + ("افتح المجلدات، واضغط على اسم الملف للتفاصيل أو 🔗 للرابط."
           if items else "ما في ملفات هون. فيك تعمل مجلد أو تبعت ملف.")
        + (f"\nصفحة {page + 1} من {total_pages + 1}" if total_pages else "")
        + ("\n(في عناصر إضافية ما ظهرت بالقائمة)" if more else "")
    )
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(summary, reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                await update.effective_message.reply_text(summary, reply_markup=markup)
    else:
        await update.effective_message.reply_text(summary, reply_markup=markup)

async def show_item(update, ctx, file_id):
    if not await _private_ui(update):
        return
    person = update.effective_user
    meta = await _drive_call(USER_DRIVE.require_item, person.id, file_id, person.full_name)
    if meta.get("mimeType") == FOLDER_MIME:
        await show_browser(update, ctx, file_id)
        return
    parent = (meta.get("parents") or [None])[0]
    size = int(meta.get("size") or 0) / (1024 * 1024)
    link = drive_file_url(meta["id"])
    text = (
        f"📄 {meta.get('name', 'ملف')}\n"
        f"الحجم: {size:.1f} MB\n\n"
        f"🔗 رابط الملف:\n{link}\n\n"
        "فيك تضغط على الرابط أو تنسخه بأي وقت."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 فتح على Drive", url=link)],
        [InlineKeyboardButton("✏️ تغيير الاسم", callback_data=f"ren:{file_id}"),
         InlineKeyboardButton("🗑 حذف", callback_data=f"trash:{file_id}")],
        [InlineKeyboardButton("⬅️ رجوع للمجلد", callback_data=f"nav:{parent}"),
         InlineKeyboardButton("🏠 ملفاتي", callback_data="home")],
    ])
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)

async def user_files(update, ctx):
    await show_browser(update, ctx)



async def _show_file_results(update, ctx, phrase=None):
    if not await _private_ui(update):
        return
    person = update.effective_user
    if phrase is None:
        found = await _drive_call(USER_DRIVE.recent_files, person.id, person.full_name)
        title = "🕘 آخر ملفاتك"
    else:
        found = await _drive_call(USER_DRIVE.search_files, person.id, person.full_name, phrase)
        title = f"🔎 نتائج «{phrase[:55]}»"
    rows = []
    for meta in found:
        title_button = _item_button("📄 " + meta.get("name", "ملف")[:35], "item", meta["id"])
        if title_button:
            rows.append([
                title_button,
                InlineKeyboardButton("🔗", url=drive_file_url(meta["id"])),
            ])
    rows.append([InlineKeyboardButton("📁 كل المجلدات", callback_data="home"),
                 InlineKeyboardButton("🔎 بحث", callback_data="findprompt")])
    text = (f"{title}\n\n"
            + ("اضغط على اسم الملف للتفاصيل، أو 🔗 لتفتحه مباشرة."
               if found else "ما لقيت ملفات بهالاسم. جرّب كلمة تانية."))
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def recent_command(update, ctx):
    await _show_file_results(update, ctx)


async def search_command(update, ctx):
    if not await _private_ui(update):
        return
    ctx.user_data["searching_files"] = True
    await update.effective_message.reply_text("اكتب جزء من اسم الملف اللي بدك رابطه. حرفين على الأقل.")


async def upload_help(update, ctx):
    if not await _private_ui(update):
        return
    person = update.effective_user
    active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
    await update.effective_message.reply_text(
        f"📤 ابعت الملف أو الفويس هون.\n"
        f"📁 مجلد الرفع الحالي: «{active['name']}»\n"
        "بعد الإرسال اضغط «⚡ حفظ بالاسم الأصلي»، أو غيّر المجلد والاسم.", reply_markup=_menu_keyboard(person.id),
    )


async def _create_and_select(update, ctx, parent_id, text):
    person = update.effective_user
    made = await _drive_call(
        USER_DRIVE.create_folder, person.id, parent_id, text, person.full_name,
    )
    await _drive_call(USER_DRIVE.select, person.id, made["id"], person.full_name)
    await update.effective_message.reply_text(f"عملت مجلد «{made['name']}» وصار مكان الرفع الحالي.")
    await show_browser(update, ctx, made["id"])


async def on_browser_button(update, ctx):
    cq = update.callback_query
    if not await _safe_callback_answer(cq):
        return
    if not await _private_ui(update):
        return
    person = update.effective_user
    uid, display = person.id, person.full_name
    action, _, arg = cq.data.partition(":")
    try:
        if action == "home":
            root = await _drive_call(USER_DRIVE.ensure_root, uid, display)
            await show_browser(update, ctx, root["id"], 0)
        elif action == "recent":
            await _show_file_results(update, ctx)
        elif action == "findprompt":
            ctx.user_data["searching_files"] = True
            await cq.message.reply_text("اكتب جزء من اسم الملف اللي عم تدور عليه.")
        elif action == "nav":
            await show_browser(update, ctx, arg)
        elif action == "page":
            fid, pg = arg.rsplit(":", 1)
            await show_browser(update, ctx, fid, int(pg))
        elif action == "item":
            await show_item(update, ctx, arg)
        elif action == "choose":
            selected = await _drive_call(USER_DRIVE.select, uid, arg, display)
            await show_browser(update, ctx, selected["id"])
        elif action == "add":
            await _drive_call(USER_DRIVE.require_folder, uid, arg, display)
            ctx.user_data.pop("renaming", None)
            ctx.user_data["creating_folder_in"] = arg
            await cq.message.reply_text("اكتب اسم المجلد الجديد:")
        elif action == "trash":
            meta = await _drive_call(USER_DRIVE.require_item, uid, arg, display)
            root = await _drive_call(USER_DRIVE.ensure_root, uid, display)
            if meta["id"] == root["id"]:
                raise FolderAccessError("المجلد الأساسي ما بينحذف.")
            warn = ("⚠️ حذف المجلد بينقل كل محتوياته كمان للسلة.\n"
                    if meta.get("mimeType") == FOLDER_MIME else "")
            await cq.edit_message_text(
                f"🗑 متأكد بدك تحذف «{meta['name']}»؟\n{warn}"
                "ممكن تستعيده من سلة Google Drive.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 نعم، للسلة", callback_data=f"confirm:{arg}"),
                    InlineKeyboardButton("إلغاء", callback_data=f"cancel:{arg}"),
                ]]),
            )
        elif action == "confirm":
            deleted = await _drive_call(USER_DRIVE.trash, uid, arg, display)
            await cq.message.reply_text(f"انتقل للسلة: {deleted.get('name', '')}")
            parent = (deleted.get("parents") or [None])[0]
            await show_browser(update, ctx, parent)
        elif action == "cancel":
            meta = await _drive_call(USER_DRIVE.require_item, uid, arg, display)
            if meta.get("mimeType") == FOLDER_MIME:
                await show_browser(update, ctx, meta["id"])
            else:
                await show_item(update, ctx, arg)
        elif action == "upcancel_name":
            queue = ctx.user_data.get("queue", [])
            if not queue:
                await cq.message.reply_text("ما في ملف ناطر.")
                return
            if queue[0].get("_uploading"):
                await cq.message.reply_text("الرفع شغّال، ما فينا نلغيه.")
                return
            queue.pop(0)
            await cq.message.reply_text("❌ ألغيت هالملف.")
            await ask_next(ctx, cq.message.chat_id, ctx.user_data)
        elif action in ("skipone", "skipall"):
            queue = ctx.user_data.get("queue", [])
            if not queue:
                await cq.message.reply_text("ما في ملف ناطر اسم.")
                return
            if not queue[0].get("target_folder"):
                await cq.message.reply_text("اختار المجلد بالأول.")
                return
            while queue and queue[0].get("target_folder"):
                if not await _finish_queued_file(ctx, cq.message.chat_id, ctx.user_data, None):
                    return
                if action == "skipone":
                    break
            await ask_next(ctx, cq.message.chat_id, ctx.user_data)
    except (FolderAccessError, ValueError):
        await cq.message.reply_text("هالمجلد مو متاح. افتح «ملفاتي» وجرّب من جديد.")
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            await cq.message.reply_text("❌ الملف أو المجلد ما عاد موجود.")
        else:
            raise

# ---------- access gate + admin panel ----------
def is_admin(update):
    return update.effective_user and update.effective_user.id in ADMIN_IDS


def user_button(rec):
    uid, st = rec["id"], rec["status"]
    if st == "banned":
        return InlineKeyboardButton(f"♻️ فك حظر {label(rec)}", callback_data=f"adm:set:{uid}:active")
    if st == "pending":
        return InlineKeyboardButton(f"✅ قبول {label(rec)}", callback_data=f"adm:set:{uid}:active")
    return InlineKeyboardButton(f"🚫 حظر {label(rec)}", callback_data=f"adm:set:{uid}:banned")


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler: records users, blocks banned / unapproved."""
    u = update.effective_user
    if not u or u.id in ADMIN_IDS:
        return
    cached = _USER_GATE_CACHE.get(u.id)
    if cached and time.monotonic() - cached[0] < 12:
        status = cached[1]
    else:
        default = "pending" if get_setting("approval_mode", "false") == "true" else "active"
        rec = await asyncio.to_thread(touch_user, u, default)
        status = rec["status"]
        _USER_GATE_CACHE[u.id] = (time.monotonic(), status)
        if rec["is_new"]:
            for aid in ADMIN_IDS:
                status_label = {"pending": "بانتظار الموافقة", "active": "مفعّل", "banned": "محظور"}.get(status, status)
                await ctx.bot.send_message(
                    aid, f"مستخدم جديد: {label(rec)} ({u.id})\nالحالة: {status_label}",
                    reply_markup=InlineKeyboardMarkup([[user_button(rec)]]),
                )
    if status == "banned":
        raise ApplicationHandlerStop
    if status == "pending":
        if update.effective_chat:
            await ctx.bot.send_message(update.effective_chat.id, "طلبك ناطر موافقة الإدارة.")
        raise ApplicationHandlerStop


def panel_markup():
    approval = get_setting("approval_mode", "false") == "true"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ طلبات الموافقة", callback_data="adm:pending"),
         InlineKeyboardButton("👥 المستخدمين", callback_data="adm:list")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="adm:stats"),
         InlineKeyboardButton(f"الموافقة: {'مفعّلة' if approval else 'متوقفة'}", callback_data="adm:mode")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm:home")],
    ])


def stats_text():
    rows = q(
        "select status, count(*) as c, coalesce(sum(uploads), 0) as up from drivebot.users group by status",
        fetch="all",
    )
    by = {r["status"]: int(r["c"]) for r in rows}
    uploads = sum(int(r["up"]) for r in rows)
    approval = get_setting("approval_mode", "false") == "true"
    return (
        "🛡️ لوحة السوبر أدمن\n\n"
        f"المستخدمين: {sum(by.values())}\n"
        f"الفعّالين: {by.get('active', 0)} | بانتظار الموافقة: {by.get('pending', 0)}\n"
        f"المحظورين: {by.get('banned', 0)}\n"
        f"الملفات المرفوعة: {uploads}\n"
        f"الموافقة على الأعضاء الجدد: {'مفعّلة' if approval else 'متوقفة'}"
    )


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("هالخيار للإدارة فقط.")
        return
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("افتح الخاص مع البوت لتدخل لوحة الإدارة.")
        return
    await update.effective_message.reply_text(stats_text(), reply_markup=panel_markup())


async def ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or update.effective_chat.type != "private":
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("استخدم /ban وبعده رقم المستخدم.")
        return
    if int(ctx.args[0]) in ADMIN_IDS:
        await update.message.reply_text("ما فيك تحظر أدمن من هون.")
        return
    rec = set_status(ctx.args[0], "banned")
    await update.message.reply_text(f"انحظر {label(rec)}." if rec else "ما لقيت هالمستخدم.")


async def unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or update.effective_chat.type != "private":
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("استخدم /unban وبعده رقم المستخدم.")
        return
    rec = set_status(ctx.args[0], "active")
    await update.message.reply_text(f"انفك الحظر عن {label(rec)}." if rec else "ما لقيت هالمستخدم.")


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in ADMIN_IDS or cq.message.chat.type != "private":
        await _safe_callback_answer(cq, "هالخيار للإدارة فقط.", True)
        return
    if not await _safe_callback_answer(cq):
        return
    parts = cq.data.split(":")
    action = parts[1]

    if action == "set":
        if len(parts) != 4 or parts[3] not in ("active", "banned") or not parts[2].isdigit():
            return
        uid = int(parts[2])
        if uid in ADMIN_IDS:
            await cq.message.reply_text("ما فيك تغيّر صلاحية أدمن من هون.")
            return
        rec = set_status(uid, parts[3])
        if rec:
            message = "انحظر حسابك من البوت." if parts[3] == "banned" else "صار فيك تستخدم البوت."
            try:
                await ctx.bot.send_message(uid, message)
            except Exception:
                pass
        action = "list"
    elif action == "mode":
        enabled = get_setting("approval_mode", "false") == "true"
        set_setting("approval_mode", "false" if enabled else "true")
        await cq.edit_message_text(stats_text(), reply_markup=panel_markup())
        return

    if action in ("home", "stats"):
        await cq.edit_message_text(stats_text(), reply_markup=panel_markup())
    elif action in ("list", "pending"):
        if action == "pending":
            users = q(
                "select * from drivebot.users where status='pending' order by last_seen desc limit 15",
                fetch="all",
            )
        else:
            users = q("select * from drivebot.users order by last_seen desc limit 15", fetch="all")
        icons = {"active": "✅", "pending": "⏳", "banned": "🚫"}
        lines = [
            f"{icons.get(r['status'], '•')} {label(r)} | {r['id']} | رفع: {r['uploads']}"
            for r in users
        ]
        heading = "⏳ طلبات الموافقة" if action == "pending" else "👥 آخر 15 مستخدم"
        buttons = [[user_button(r)] for r in users]
        buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:home")])
        await cq.edit_message_text(
            heading + "\n\n" + ("\n".join(lines) if lines else "ما في طلبات حالياً."),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Keep traceback on Render; users and admins see a short, readable message."""
    err = ctx.error
    traceback.print_exception(type(err), err, err.__traceback__)
    chat_id = update.effective_chat.id if isinstance(update, Update) and update.effective_chat else None
    if not chat_id:
        return
    if isinstance(err, (TimedOut, NetworkError)):
        message = "الاتصال تأخر شوي. جرّب مرة تانية."
    elif isinstance(err, FolderAccessError):
        message = "ما عندك صلاحية لهالمجلد."
    elif isinstance(err, (BadRequest, ValueError)):
        message = "هالطلب ما زبط. افتح القائمة وجرّب من جديد."
    elif isinstance(err, HttpError):
        message = "في مشكلة بالاتصال مع Drive. جرّب بعد شوي."
    else:
        message = "صار خلل بسيط. جرّب مرة تانية."
    try:
        await ctx.bot.send_message(chat_id, message)
    except Exception:
        pass

if not ADMIN_IDS:
    print("WARNING: ADMIN_IDS not set -> no admin panel. Get your ID from @userinfobot")

def start_health_server():
    """Tiny HTTP endpoint for hosts that require an open port (Render/Koyeb free) + uptime pingers."""
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()


start_health_server()

builder = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(3)
if BOT_API_URL:
    builder = builder.base_url(f"{BOT_API_URL}/bot").base_file_url(f"{BOT_API_URL}/file/bot").local_mode(True)
app = builder.build()
app.add_error_handler(on_error)
app.add_handler(TypeHandler(Update, gate), group=-1)
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CommandHandler("ban", ban))
app.add_handler(CommandHandler("unban", unban))
app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^adm:"))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("newfolder", newfolder))
app.add_handler(CommandHandler("folder", folder))
app.add_handler(CommandHandler("files", user_files))
app.add_handler(CommandHandler("recent", recent_command))
app.add_handler(CommandHandler("find", search_command))
app.add_handler(CommandHandler("skip", skip))
app.add_handler(CommandHandler("skipall", skipall))
app.add_handler(CommandHandler("cancel", cancel_command))
app.add_handler(CallbackQueryHandler(on_upload_folder_button, pattern=r"^(upnav:|upset:|upfast:|upbatch:|upretry:|upcancel:|uppage:|upadd:)"))
app.add_handler(CallbackQueryHandler(on_browser_button, pattern=r"^(home$|recent$|findprompt$|nav:|page:|item:|choose:|add:|trash:|confirm:|cancel:|skipone$|skipall$|upcancel_name$)"))
app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(status|ren:)"))
app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(STATUS_BTN)}$"), status))
app.add_handler(MessageHandler(filters.ATTACHMENT, on_file))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
app.run_polling()
