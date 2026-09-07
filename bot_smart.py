# -*- coding: utf-8 -*-
# telegram_downloader_bot.py
# نسخه نهایی: فقط Bot API + Webhook + محدودیت 50MB

import os
import re
import time
import uuid
import json
import threading
import traceback
import shutil
import ssl
from queue import Queue
from urllib.parse import urlparse
from pathlib import Path
from threading import Lock
from datetime import datetime

import yt_dlp
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_downloader")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, MessageHandler, Filters, CallbackQueryHandler, CommandHandler
from telegram.error import NetworkError

from flask import Flask, request

# --- تنظیمات ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8460981737:AAFVLyZbSkv6eIqVPXWnEuhjVJYy9TyCCUA")

IS_ON_RENDER = bool(os.environ.get("PORT"))
if IS_ON_RENDER:
    DOWNLOAD_ROOT = "/tmp/telegram_downloader"
else:
    DOWNLOAD_ROOT = os.path.join(os.getcwd(), "telegram_downloader")

os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
USERS_ROOT = os.path.join(DOWNLOAD_ROOT, "users")
os.makedirs(USERS_ROOT, exist_ok=True)

# --- محدودیت آپلود: 50 مگابایت ---
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = 2 if IS_ON_RENDER else 6
PAGE_SIZE = 8
YTDLP_SOCKET_TIMEOUT = 20
USER_AGENT_HEAD = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# --- وضعیت‌ها ---
REQUESTS = {}
REQUESTS_LOCK = threading.Lock()
download_queue = Queue()
active_workers = 0
active_workers_lock = threading.Lock()
CANCEL_FLAGS = {}
CANCEL_LOCK = threading.Lock()

# --- توابع کمکی ---
def human_size(size):
    if not size:
        return "—"
    size = int(size)
    if size >= 1024**3:
        return f"{size/1024/1024/1024:.2f} GB"
    if size >= 1024**2:
        return f"{size/1024/1024:.2f} MB"
    return f"{size/1024:.2f} KB"

def is_youtube_url(url):
    net = urlparse(url).netloc.lower()
    return "youtube.com" in net or "youtu.be" in net

def safe_edit_message(bot, chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except:
        pass

# --- ساخت کیبوردها ---
def make_cancel_markup(task_id, owner_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو دانلود", callback_data=f"cancel_dl:{task_id}:{owner_id}")]])

def make_quality_keyboard(parsed_formats, request_id, page=0):
    rows = []
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    for f in parsed_formats[start:end]:
        label = f"{f.get('height', '?')}p • {f.get('ext', '')} • {human_size(f.get('size'))}"
        cb = f"dl:{request_id}:{f['format_id']}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"page:{request_id}:{page-1}"))
    if end < len(parsed_formats):
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"page:{request_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("🎥 بهترین کیفیت", callback_data=f"dl:{request_id}:best"),
        InlineKeyboardButton("🎧 فقط صدا", callback_data=f"dl:{request_id}:audio"),
        InlineKeyboardButton("❌ لغو", callback_data="cancel")
    ])
    return InlineKeyboardMarkup(rows)

def make_output_mode_keyboard(request_id, format_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 ارسال به تلگرام", callback_data=f"mode:{request_id}:{format_id}:telegram")],
        [InlineKeyboardButton("💾 ذخیره محلی", callback_data=f"mode:{request_id}:{format_id}:local")],
        [InlineKeyboardButton("❌ لغو", callback_data="cancel")]
    ])

# --- استخراج اطلاعات ---
def extract_info_safe(url):
    ydl_opts = {
        "quiet": True, "no_warnings": True, "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "http_headers": {"User-Agent": USER_AGENT_HEAD},
        "skip_download": True, "nocheckcertificate": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def parse_formats_from_info(info):
    formats = info.get("formats", [])
    parsed = []
    for f in formats:
        if f.get("vcodec") in (None, "none"):  # فقط فرمت‌های ویدیویی
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if size > MAX_FILE_SIZE:
            continue
        parsed.append({
            "format_id": str(f.get("format_id")),
            "ext": f.get("ext"),
            "height": f.get("height"),
            "size": size
        })
    if not parsed:
        parsed.append({"format_id": "best", "ext": "mp4", "height": "best", "size": 0})
    parsed.sort(key=lambda x: (x.get("height") or 0), reverse=True)
    return parsed

# --- دانلود ---
def yt_dlp_download(url, outtmpl, format_spec="best"):
    ydl_opts = {
        "outtmpl": outtmpl, "quiet": True, "no_warnings": True,
        "noprogress": True, "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "http_headers": {"User-Agent": USER_AGENT_HEAD}, "nocheckcertificate": True
    }
    if format_spec:
        ydl_opts["format"] = format_spec
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# --- آپلود (فقط Bot API) ---
def upload_file_with_progress(bot_token, chat_id, file_path, caption, progress_callback):
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    filename = os.path.basename(file_path)
    session = requests.Session()
    session.trust_env = False
    with open(file_path, "rb") as f:
        m = MultipartEncoder(fields={
            "chat_id": str(chat_id),
            "caption": caption or "",
            "document": (filename, f, "application/octet-stream")
        })
        monitor = MultipartEncoderMonitor(m, lambda mon: progress_callback(mon.bytes_read, m.len))
        r = session.post(url, data=monitor, headers={"Content-Type": monitor.content_type}, timeout=3600)
    return r

# --- Worker ---
def download_worker_thread(bot):
    global active_workers
    while True:
        task = download_queue.get()
        if task is None:
            break
        with active_workers_lock:
            active_workers += 1
        try:
            chat_id = task["chat_id"]
            url = task["url"]
            format_id = task.get("format_id") or "best"
            mode = task.get("mode") or "telegram"
            owner_id = task.get("user_id")
            task_id = uuid.uuid4().hex[:12]

            CANCEL_FLAGS[task_id] = {"cancel": False, "owner_id": owner_id}
            msg = bot.send_message(chat_id=chat_id, text="در حال آماده‌سازی...", reply_markup=make_cancel_markup(task_id, owner_id))
            msg_id = msg.message_id

            # استخراج
            info = extract_info_safe(url)
            safe_title = re.sub(r'[<>:"/\\\\|?*]', '_', info.get("title", "file"))[:100]
            outtmpl = os.path.join(DOWNLOAD_ROOT, f"{safe_title}.%(ext)s")

            # فرمت نهایی
            if format_id == "best":
                final_format = "bestvideo+bestaudio/best" if is_youtube_url(url) else "best"
            elif format_id == "audio":
                final_format = "bestaudio/best"
            else:
                final_format = f"{format_id}+bestaudio/best"

            try:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⬇️ در حال دانلود...", reply_markup=make_cancel_markup(task_id, owner_id))
                file_path = yt_dlp_download(url, outtmpl, final_format)
            except Exception as e:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"❗ خطا در دانلود: {str(e)[:200]}")
                CANCEL_FLAGS.pop(task_id, None)
                continue

            if not os.path.exists(file_path):
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="❗ فایل پیدا نشد")
                CANCEL_FLAGS.pop(task_id, None)
                continue

            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                os.remove(file_path)
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"❗ فایل بزرگتر از ۵۰ مگابایت است.")
                CANCEL_FLAGS.pop(task_id, None)
                continue

            if mode == "local":
                dest = os.path.join(DOWNLOAD_ROOT, f"user_{owner_id}")
                os.makedirs(dest, exist_ok=True)
                shutil.move(file_path, os.path.join(dest, os.path.basename(file_path)))
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="✅ ذخیره شد.")
                CANCEL_FLAGS.pop(task_id, None)
                continue

            # آپلود
            def progress(sent, total):
                pct = int(sent * 100 / total) if total else 0
                if pct % 10 == 0:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"📤 آپلود: {pct}%")
                    except:
                        pass

            try:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="📤 در حال آپلود...", reply_markup=make_cancel_markup(task_id, owner_id))
                resp = upload_file_with_progress(TOKEN, chat_id, file_path, info.get("title"), progress)
                if resp.status_code != 200:
                    raise Exception(f"Telegram API Error: {resp.text[:100]}")
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="✅ تمام شد!")
            except Exception as e:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"❗ خطا در آپلود: {str(e)[:200]}")
            finally:
                os.remove(file_path)
                CANCEL_FLAGS.pop(task_id, None)

        except Exception as e:
            logger.error(f"Worker error: {e}")
        finally:
            with active_workers_lock:
                active_workers -= 1

def start_workers(bot):
    for _ in range(MAX_CONCURRENT_DOWNLOADS):
        threading.Thread(target=download_worker_thread, args=(bot,), daemon=True).start()

# --- هندلرها ---
def start(update, context):
    update.message.reply_text("سلام! لینک بفرست تا کیفیت‌ها رو ببینی.")

def handle_message(update, context):
    text = update.message.text or ""
    if text.startswith("http"):
        chat_id = update.message.chat_id
        user = update.message.from_user
        req_id = uuid.uuid4().hex[:12]
        bot = context.bot

        try:
            info = extract_info_safe(text)
        except Exception as e:
            bot.send_message(chat_id=chat_id, text=f"❗ خطا: {str(e)[:200]}")
            return

        formats = parse_formats_from_info(info)
        REQUESTS[req_id] = {"url": text, "info": info, "user_id": user.id}
        meta = f"🎬 {info.get('title')}\n⏱ {info.get('duration')} ثانیه"
        bot.send_message(chat_id=chat_id, text=meta, reply_markup=make_quality_keyboard(formats, req_id, 0))

def handle_callback(update, context):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id
    user = query.from_user
    bot = context.bot
    message_id = query.message.message_id

    if data == "cancel":
        query.edit_message_text("لغو شد.")
        return

    if data.startswith("cancel_dl:"):
        _, task_id, owner_id = data.split(":", 2)
        if int(owner_id) == user.id:
            CANCEL_FLAGS[task_id] = {"cancel": True, "owner_id": user.id}
            query.edit_message_text("در حال لغو...")
        return

    if data.startswith("page:"):
        _, req_id, page = data.split(":", 2)
        req = REQUESTS.get(req_id)
        if req:
            formats = parse_formats_from_info(req["info"])
            query.edit_message_reply_markup(reply_markup=make_quality_keyboard(formats, req_id, int(page)))
        return

    if data.startswith("dl:"):
        _, req_id, fmt = data.split(":", 2)
        req = REQUESTS.get(req_id)
        if req and req["user_id"] == user.id:
            query.edit_message_text("کجا ذخیره بشه؟", reply_markup=make_output_mode_keyboard(req_id, fmt))
        return

    if data.startswith("mode:"):
        _, req_id, fmt, mode = data.split(":", 3)
        req = REQUESTS.get(req_id)
        if req and req["user_id"] == user.id:
            download_queue.put({
                "chat_id": chat_id,
                "url": req["url"],
                "format_id": fmt if fmt != "best" else "best",
                "mode": mode,
                "user_id": user.id
            })
            query.edit_message_text("✅ به صف اضافه شد.")

# --- تنظیمات Flask و Webhook ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return 'OK', 200

# --- Main ---
bot = None
dispatcher = None

def main():
    global bot, dispatcher
    updater = Updater(TOKEN, use_context=True)
    bot = updater.bot
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(CallbackQueryHandler(handle_callback))

    start_workers(bot)

    if IS_ON_RENDER:
        # Webhook
        webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_URL', 'render-bot-87no.onrender.com')}/webhook/{TOKEN}"
        bot.set_webhook(url=webhook_url)
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        updater.start_polling()
        updater.idle()

if __name__ == "__main__":
    main()
