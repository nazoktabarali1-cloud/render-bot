# -*- coding: utf-8 -*-
# =====================================================================
# telegram_downloader_bot.py
# نسخه هوشمند: اجرا هم روی لپ‌تاپ و هم روی Render (Webhook mode)
# =====================================================================

import os
import re
import time
import uuid
import json
import threading
import traceback
import shutil
import zipfile
import ssl
import random
from queue import Queue
from urllib.parse import urlparse
from pathlib import Path
from threading import Lock
from datetime import datetime, timedelta

import yt_dlp
import psutil
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_downloader")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, MessageHandler, Filters, CallbackQueryHandler, CommandHandler
from telegram.error import NetworkError

# --- اضافه کردن Flask برای Render ---
from flask import Flask, request

# Telethon optional
try:
    from telethon import TelegramClient, errors as telethon_errors
except Exception:
    TelegramClient = None
    telethon_errors = None

# Try to import urllib3 SSLError type for robust exception checks
try:
    from telegram.vendor.ptb_urllib3.urllib3.exceptions import SSLError as Urllib3SSLError
except Exception:
    try:
        from urllib3.exceptions import SSLError as Urllib3SSLError
    except Exception:
        Urllib3SSLError = None

# Try to detect cryptg (optional speedup for Telethon)
try:
    import cryptg  # type: ignore
    HAS_CRYPTG = True
except Exception:
    HAS_CRYPTG = False

# -------------------------
# پیکربندی
# -------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8460981737:AAFVLyZbSkv6eIqVPXWnEuhjVJYy9TyCCUA")

TELETHON_API_ID = os.environ.get("TELETHON_API_ID", "39999874")
TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH", "f6c320a19abd4975daaaa2f9d61601ff")
TELETHON_SESSION = os.environ.get("TELETHON_SESSION", "user_session")

FORCE_TELETHON_ALWAYS = os.environ.get("FORCE_TELETHON_ALWAYS", "0") == "1"

# --- تشخیص محیط اجرا و تنظیم مسیر ذخیره‌سازی ---
# اگر روی Render باشیم (متغیر PORT ست شده)، از پوشه موقت /tmp استفاده می‌کنیم
IS_ON_RENDER = bool(os.environ.get("PORT"))
if IS_ON_RENDER:
    DOWNLOAD_ROOT = "/tmp/telegram_downloader"
else:
    DOWNLOAD_ROOT = os.path.join(os.getcwd(), "telegram_downloader")

os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

USERS_ROOT = os.path.join(DOWNLOAD_ROOT, "users")
os.makedirs(USERS_ROOT, exist_ok=True)

LOG_ROOT = Path(DOWNLOAD_ROOT) / "telegram_bot_logs"
LOG_ROOT.mkdir(parents=True, exist_ok=True)
LOG_LOCK = Lock()

BACKUP_ROOT = Path(DOWNLOAD_ROOT) / "telegram_bot_backups"
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
BACKUP_LOCK = Lock()

# --- تنظیم حداکثر دانلود همزمان ---
if IS_ON_RENDER:
    MAX_CONCURRENT_DOWNLOADS = 2
else:
    MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "6"))

PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "8"))
REQUEST_TTL_SECONDS = int(os.environ.get("REQUEST_TTL_SECONDS", "300"))
YTDLP_SOCKET_TIMEOUT = int(os.environ.get("YTDLP_SOCKET_TIMEOUT", "20"))
YTDLP_RETRIES = int(os.environ.get("YTDLP_RETRIES", "2"))
YTDLP_FRAGMENT_RETRIES = int(os.environ.get("YTDLP_FRAGMENT_RETRIES", "2"))
YTDLP_HTTP_CHUNK_SIZE = 1024 * 1024

HEAD_TIMEOUT = int(os.environ.get("HEAD_TIMEOUT", "4"))
MAX_HEAD_REQUESTS_PER_PARSE = int(os.environ.get("MAX_HEAD_REQUESTS_PER_PARSE", "1"))
USER_AGENT_HEAD = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

MAIN_RESOLUTIONS = [144, 240, 360, 480, 720, 1080, 1440, 2160]

ANIMATION_INTERVAL = 0.20

NETWORK_MAX_RETRIES = 6
NETWORK_BACKOFF_BASE = 1.5
NETWORK_RETRY_SLEEP_MIN = 1.0
NETWORK_RETRY_SLEEP_MAX = 30.0

CHUNK_SIZE = 48 * 1024 * 1024
MAX_SINGLE_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

# -------------------------
# وضعیت‌ها و صف‌ها
# -------------------------
REQUESTS = {}
REQUESTS_LOCK = threading.Lock()

download_queue = Queue()
active_workers = 0
active_workers_lock = threading.Lock()

progress_map = {}

CANCEL_FLAGS = {}
CANCEL_LOCK = threading.Lock()

USER_MAP = {}
USER_MAP_LOCK = Lock()

# -------------------------
# Telethon client (lazy init)
# -------------------------
telethon_client = None
telethon_lock = threading.Lock()

def ensure_telethon_client():
    global telethon_client
    with telethon_lock:
        try:
            if telethon_client and getattr(telethon_client, "is_connected", lambda: False)():
                return telethon_client
        except Exception:
            telethon_client = None

        if not TELETHON_API_ID or not TELETHON_API_HASH or TelegramClient is None:
            logger.info("Telethon API credentials not provided or Telethon not installed; Telethon disabled.")
            return None

        try:
            telethon_client = TelegramClient(TELETHON_SESSION, int(TELETHON_API_ID), TELETHON_API_HASH)
            telethon_client.start()
            if not HAS_CRYPTG:
                logger.warning("cryptg not available: Telethon will fall back to slower crypto.")
            else:
                logger.info("cryptg detected: Telethon will use optimized crypto for uploads.")
            logger.info("Telethon client started.")
            return telethon_client
        except Exception as e:
            logger.exception("Failed to start Telethon client: %s", e)
            telethon_client = None
            return None

# -------------------------
# توابع کمکی
# -------------------------
def ensure_user_dir(user_id_or_username):
    if isinstance(user_id_or_username, str) and user_id_or_username:
        safe = re.sub(r'[^0-9A-Za-z_\-@]', '_', user_id_or_username)
        user_dir = Path(USERS_ROOT) / f"user_{safe}"
    else:
        user_dir = Path(USERS_ROOT) / f"user_{user_id_or_username}"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "downloads").mkdir(exist_ok=True)
    (user_dir / "logs").mkdir(exist_ok=True)
    (user_dir / "backups").mkdir(exist_ok=True)
    return user_dir

def append_user_log(user_id_or_username, record: dict):
    try:
        udir = ensure_user_dir(user_id_or_username)
        log_file = udir / "logs" / "events.log"
        record["ts"] = int(time.time())
        record["datetime"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_LOCK:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

def sanitize_name(name, max_len=120):
    return re.sub(r'[<>:"/\\\\|?*]', '_', str(name))[:max_len]

def human_size(size):
    if not size:
        return "—"
    try:
        size = int(size)
        if size >= 1024**3:
            return f"{size/1024/1024/1024:.2f} GB"
        if size >= 1024**2:
            return f"{size/1024/1024:.2f} MB"
        return f"{size/1024:.2f} KB"
    except:
        return "—"

def is_instagram_or_x(url):
    try:
        net = urlparse(url).netloc.lower()
        return any(d in net for d in ("instagram.com", "www.instagram.com", "x.com", "www.x.com", "twitter.com", "www.twitter.com", "t.co", "facebook.com", "fb.watch", "tiktok.com"))
    except:
        return False

def is_youtube_url(url):
    try:
        net = urlparse(url).netloc.lower()
        return "youtube.com" in net or "youtu.be" in net
    except:
        return False

# -------------------------
# safe edit helper
# -------------------------
def safe_edit_message(bot, chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except:
        try:
            bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup)
        except:
            pass

# -------------------------
# === انیمیشن‌ها ===
# -------------------------

def format_eta(seconds):
    try:
        s = int(max(0, int(seconds)))
    except Exception:
        return "—"
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"

def render_I_with_inner_fill(pct, segments=8, inner_width=5):
    try:
        pct = max(0, min(100, int(pct)))
    except:
        pct = 0
    filled = int(round((pct / 100.0) * segments))
    if inner_width % 2 == 0:
        inner_width -= 1
        if inner_width < 1:
            inner_width = 1
    bar_width = inner_width + 4
    top_bar = " " * ((bar_width - 3)//2) + "███" + " " * ((bar_width - 3)//2)
    bottom_bar = top_bar
    middle_lines = []
    for i in range(segments):
        idx_from_bottom = segments - 1 - i
        inner = "█" * inner_width if idx_from_bottom < filled else "░" * inner_width
        line = " " * 2 + inner + " " * 2
        middle_lines.append(line)
    header = "A L I"
    footer = f"{pct}%"
    parts = [header, "", top_bar] + middle_lines + [bottom_bar, "", footer]
    return "\n".join(parts)

def render_horizontal_ali_bar(pct, length=20, left_label="ALI", fill_char="█", empty_char="░"):
    try:
        pct = max(0, min(100, int(pct)))
    except:
        pct = 0
    filled = int(round((pct / 100.0) * length))
    bar = fill_char * filled + empty_char * (length - filled)
    percent_text = f"{pct}%"
    line = f"{left_label} |{bar}| {percent_text}"
    return line

class QualityAnimationALI:
    def __init__(self):
        self.map = {}

    def start(self, key, bot, chat_id, title="در حال بررسی کیفیت…", min_interval=0.9):
        msg = bot.send_message(chat_id=chat_id, text=title)
        stop_flag = {"stop": False}
        info = {
            "bot": bot,
            "chat_id": chat_id,
            "msg_id": msg.message_id,
            "title": title,
            "min_interval": min_interval,
            "last_edit": 0,
            "stop_flag": stop_flag,
            "reply_markup": None
        }
        self.map[key] = info
        t = threading.Thread(target=self._runner, args=(key,), daemon=True)
        t.start()
        return msg.message_id

    def stop(self, key, final_text=None):
        info = self.map.get(key)
        if not info:
            return
        info["stop_flag"]["stop"] = True
        try:
            text = final_text or f"{info['title']}\n\n✅ بررسی کیفیت انجام شد"
            info["bot"].edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=text, reply_markup=info.get("reply_markup"))
        except:
            pass
        self.map.pop(key, None)

    def _runner(self, key):
        info = self.map.get(key)
        if not info:
            return
        frames = ["A", "Al", "ALI", "AL", "A", "AL"]
        i = 0
        while not info["stop_flag"]["stop"]:
            now = time.time()
            if now - info["last_edit"] >= info["min_interval"]:
                frame = frames[i % len(frames)]
                text = f"{info['title']}\n\n{frame}"
                try:
                    info["bot"].edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=text, reply_markup=info.get("reply_markup"))
                except:
                    pass
                info["last_edit"] = now
                i += 1
            time.sleep(0.12)

class QualityAnimation:
    def __init__(self):
        self.map = {}

    def start(self, key, bot, chat_id, title="در حال بررسی کیفیت…", min_interval=0.9, segments=8):
        msg = bot.send_message(chat_id=chat_id, text=title)
        info = {
            "bot": bot,
            "chat_id": chat_id,
            "msg_id": msg.message_id,
            "title": title,
            "min_interval": min_interval,
            "last_edit": 0,
            "segments": segments,
            "pct": 0,
            "reply_markup": None
        }
        self.map[key] = info
        return msg.message_id

    def update_i_fill(self, key, pct, extra_text=None, inner_width=5):
        info = self.map.get(key)
        if not info:
            return
        now = time.time()
        if now - info["last_edit"] < info["min_interval"]:
            return
        i_block = render_I_with_inner_fill(pct, segments=info.get("segments", 8), inner_width=inner_width)
        lines = [info["title"], "", i_block]
        if extra_text:
            lines += ["", extra_text]
        text = "\n".join(lines)
        try:
            info["bot"].edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=text, reply_markup=info.get("reply_markup"))
        except:
            pass
        info["last_edit"] = now
        info["pct"] = pct

    def stop(self, key, final_text=None):
        info = self.map.get(key)
        if not info:
            return
        try:
            text = final_text or f"{info['title']}\n\n✅ بررسی کیفیت انجام شد"
            info["bot"].edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=text, reply_markup=info.get("reply_markup"))
        except:
            pass
        self.map.pop(key, None)

class ProcessingAnimation:
    def __init__(self):
        self.map = {}

    def start(self, key, bot, chat_id, title="در حال پردازش فایل…", initial_stage="شروع", min_interval=0.9, bar_length=20):
        msg = bot.send_message(chat_id=chat_id, text=title)
        info = {
            "bot": bot,
            "chat_id": chat_id,
            "msg_id": msg.message_id,
            "title": title,
            "stage": initial_stage,
            "pct": 0,
            "min_interval": min_interval,
            "bar_length": bar_length,
            "last_edit": 0,
            "start_time": time.time(),
            "last_transferred": None,
            "last_transferred_time": None,
            "reply_markup": None
        }
        self.map[key] = info
        return msg.message_id

    def update(self, key, stage=None, pct=None, transferred=None, total=None, speed_bps=None, extra_text=None):
        info = self.map.get(key)
        if not info:
            return
        now = time.time()
        if now - info["last_edit"] < info["min_interval"]:
            return

        if stage is not None:
            info["stage"] = stage
        if pct is not None:
            info["pct"] = max(0, min(100, int(pct)))

        if speed_bps is None and transferred is not None:
            prev = info.get("last_transferred")
            prev_t = info.get("last_transferred_time")
            if prev is not None and prev_t is not None and now - prev_t > 0:
                speed_bps = (transferred - prev) / max(1e-6, (now - prev_t))

        speed_text = f"⚡ {speed_bps/1024/1024:.2f} MB/s" if speed_bps and speed_bps > 0 else ""
        eta_text = ""
        if transferred is not None and total:
            remaining = max(0, total - transferred)
            if speed_bps and speed_bps > 0:
                eta_text = f"⏳ {format_eta(remaining / speed_bps)}"
        size_text = ""
        if transferred is not None and total:
            size_text = f"📦 {human_size(transferred)} / {human_size(total)}"
        elif transferred is not None:
            size_text = f"📦 {human_size(transferred)}"

        extras = "  ".join([t for t in [speed_text, eta_text, size_text, extra_text] if t])

        bar_line = render_horizontal_ali_bar(info.get("pct", 0), length=info.get("bar_length", 20), left_label=info.get("stage", "processing"))
        elapsed = int(now - info.get("start_time", now))
        elapsed_text = f"⏱ {format_eta(elapsed)}"
        lines = [info.get("title"), "", bar_line]
        if extras:
            lines += ["", f"{elapsed_text}  {extras}"]
        else:
            lines += ["", elapsed_text]
        text = "\n".join(lines)

        try:
            info["bot"].edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=text, reply_markup=info.get("reply_markup"))
        except:
            pass

        info["last_edit"] = now
        if transferred is not None:
            info["last_transferred"] = transferred
            info["last_transferred_time"] = now

    def stop(self, key, final_text=None):
        info = self.map.get(key)
        if not info:
            return
        try:
            text = final_text or f"{info['title']}\n\n✅ پردازش انجام شد"
            info["bot"].edit_message_text(chat_id=info["chat_id"], message_id=info["msg_id"], text=text, reply_markup=info.get("reply_markup"))
        except:
            pass
        self.map.pop(key, None)

# global instances
quality_ali_anim = QualityAnimationALI()
quality_anim = QualityAnimation()
proc_anim = ProcessingAnimation()

# -------------------------
# Remote size helper
# -------------------------
def get_remote_size(url):
    try:
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return None
        headers = {"User-Agent": USER_AGENT_HEAD}
        try:
            r = requests.head(url, headers=headers, allow_redirects=True, timeout=HEAD_TIMEOUT)
            if 200 <= r.status_code < 400:
                cl = r.headers.get("Content-Length") or r.headers.get("content-length")
                if cl and cl.isdigit():
                    return int(cl)
        except Exception:
            pass
        try:
            with requests.get(url, headers=headers, stream=True, timeout=HEAD_TIMEOUT) as r2:
                if 200 <= r2.status_code < 400:
                    cl = r2.headers.get("Content-Length") or r2.headers.get("content-length")
                    if cl and cl.isdigit():
                        return int(cl)
        except Exception:
            pass
        return None
    except Exception:
        return None

# -------------------------
# Retry decorator
# -------------------------
def retry_on_network_errors(max_retries=NETWORK_MAX_RETRIES, base_backoff=NETWORK_BACKOFF_BASE):
    def deco(func):
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    is_network = False
                    if isinstance(e, NetworkError):
                        is_network = True
                    elif Urllib3SSLError and isinstance(e, Urllib3SSLError):
                        is_network = True
                    elif isinstance(e, ssl.SSLError):
                        is_network = True
                    elif isinstance(e, requests.exceptions.RequestException):
                        is_network = True
                    if not is_network:
                        raise
                    if attempt > max_retries:
                        raise
                    sleep_for = min(NETWORK_RETRY_SLEEP_MAX, (base_backoff ** attempt))
                    sleep_for = max(NETWORK_RETRY_SLEEP_MIN, sleep_for)
                    time.sleep(sleep_for)
        return wrapper
    return deco

# -------------------------
# Improved extract_info_safe
# -------------------------
class ExtractError(Exception):
    pass

@retry_on_network_errors()
def extract_info_safe(url):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "fragment_retries": YTDLP_FRAGMENT_RETRIES,
        "http_chunk_size": YTDLP_HTTP_CHUNK_SIZE,
        "http_headers": {"User-Agent": USER_AGENT_HEAD, "Accept-Language": "en-US,en;q=0.9"},
        "extractor_args": {"generic": {"impersonate": "chrome"}},
        "skip_download": True,
        "nocheckcertificate": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "HTTP Error 403" in msg or "Cloudflare" in msg or "impersonate" in msg:
            raise ExtractError("منبع با محافظت Cloudflare یا نیاز به impersonation مواجه است.")
        if "Unable to extract" in msg or "flashvars" in msg:
            raise ExtractError("محتوا قابل استخراج نیست.")
        raise ExtractError(f"خطا در استخراج اطلاعات: {msg}")
    except Exception as e:
        raise ExtractError(f"خطای غیرمنتظره در استخراج: {str(e)}")

# -------------------------
# parse_formats_from_info
# -------------------------
def parse_formats_from_info(info):
    formats = info.get("formats", []) or []
    parsed = []
    seen = set()
    head_requests = 0

    size_map = {}
    for f in formats:
        try:
            h = f.get("height")
            ext0 = (f.get("ext") or "").lower()
            s = f.get("filesize") or f.get("filesize_approx")
            if not s and f.get("url") and head_requests < MAX_HEAD_REQUESTS_PER_PARSE:
                s = get_remote_size(f.get("url"))
                if s:
                    head_requests += 1
            if h is not None and ext0 and s:
                try:
                    size_map[(int(h), ext0)] = int(s)
                except:
                    size_map[(int(h), ext0)] = s
        except:
            continue

    head_requests = 0

    for f in formats:
        try:
            fid = str(f.get("format_id") or "")
            ext = (f.get("ext") or "").lower()
            height = f.get("height")
            res = f.get("resolution") or (str(height) + "p" if height else "")
            size = f.get("filesize") or f.get("filesize_approx") or None
            url = f.get("url") or None
            mime = f.get("mime_type") or ""
            acodec = f.get("acodec") or ""
            vcodec = f.get("vcodec") or ""
            abr = f.get("abr")
            tbr = f.get("tbr")
            format_note = f.get("format_note") or ""

            if not height and isinstance(res, str) and res.endswith("p"):
                try:
                    height = int(res.rstrip("p"))
                except:
                    height = None

            if not size and height and ext:
                s = size_map.get((int(height), ext))
                if s:
                    size = s

            if not size and url and head_requests < MAX_HEAD_REQUESTS_PER_PARSE:
                remote_size = get_remote_size(url)
                if remote_size:
                    size = remote_size
                head_requests += 1

            is_video_only = (acodec in (None, "none", "unknown") or acodec == "none") and (vcodec and vcodec != "none")
            is_audio_only = (vcodec in (None, "none", "unknown") or vcodec == "none") and (acodec and acodec != "none")
            is_muxed = not is_video_only and not is_audio_only

            label_parts = []
            if height:
                label_parts.append(f"{height}p")
            if is_video_only:
                label_parts.append("(video-only)")
            if is_audio_only:
                if abr:
                    label_parts.append(f"audio {int(abr)}kbps")
                else:
                    label_parts.append("audio")
            if format_note:
                label_parts.append(format_note)
            label = " ".join(label_parts) if label_parts else (ext.upper() if ext else "file")

            is_preview = False
            if size and size < 300 * 1024:
                is_preview = True
            if isinstance(mime, str) and mime.startswith("image/") and ext == "gif":
                is_preview = True

            key = (fid, ext, height, acodec, vcodec)
            if key in seen:
                continue
            seen.add(key)

            parsed.append({
                "format_id": fid,
                "ext": ext,
                "resolution": res or "",
                "height": height,
                "size": int(size) if isinstance(size, (int, float)) else size,
                "size_text": human_size(size),
                "label": label,
                "url": url,
                "is_preview": is_preview,
                "mime": mime,
                "note": format_note,
                "acodec": acodec,
                "vcodec": vcodec,
                "is_video_only": bool(is_video_only),
                "is_audio_only": bool(is_audio_only),
                "is_muxed": bool(is_muxed),
                "abr": abr,
                "tbr": tbr
            })
        except Exception:
            continue

    def sort_key(x):
        h = x.get("height") or 0
        mux = 2 if x.get("is_muxed") else (1 if x.get("is_video_only") else 0)
        return (h, mux, x.get("ext") or "")

    parsed.sort(key=lambda x: (-sort_key(x)[0], -sort_key(x)[1], x.get("ext") or ""))

    if not parsed:
        raw_formats = info.get("formats", []) or []
        for f in raw_formats:
            try:
                fid = str(f.get("format_id") or "")
                ext = (f.get("ext") or "").lower()
                height = f.get("height")
                size = f.get("filesize") or f.get("filesize_approx")
                label = f.get("resolution") or (str(height) + "p" if height else (f.get("format_note") or ext))
                parsed.append({
                    "format_id": fid,
                    "ext": ext,
                    "resolution": f.get("resolution") or (str(height) + "p" if height else ""),
                    "height": height,
                    "size": int(size) if isinstance(size, (int, float)) else size,
                    "size_text": human_size(size),
                    "label": label,
                    "url": f.get("url"),
                    "is_preview": False,
                    "mime": f.get("mime_type") or "",
                    "note": f.get("format_note") or ""
                })
            except:
                continue

    return parsed

# -------------------------
# UI helpers
# -------------------------
def make_request_cancel_markup(request_id, owner_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو درخواست", callback_data=f"cancel_req:{request_id}:{owner_id}")]])

def build_category_tabs(request_id, active_category):
    tabs = []
    mp4_label = "🔵 MP4" if active_category == "mp4" else "MP4"
    webm_label = "🟢 WEBM" if active_category == "webm" else "WEBM"
    other_label = "🟣 OTHER" if active_category == "other" else "OTHER"
    tabs.append(InlineKeyboardButton(mp4_label, callback_data=f"cat:{request_id}:mp4:0"))
    tabs.append(InlineKeyboardButton(webm_label, callback_data=f"cat:{request_id}:webm:0"))
    tabs.append(InlineKeyboardButton(other_label, callback_data=f"cat:{request_id}:other:0"))
    return tabs

def make_quality_keyboard(parsed_formats, request_id, category="mp4", page=0):
    mp4_formats = [f for f in parsed_formats if f.get("ext") == "mp4"]
    webm_formats = [f for f in parsed_formats if f.get("ext") == "webm"]
    other_formats = [f for f in parsed_formats if f.get("ext") not in ("mp4", "webm")]

    category_map = {"mp4": mp4_formats, "webm": webm_formats, "other": other_formats}
    chosen = category_map.get(category, mp4_formats)

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    rows = []
    tabs = build_category_tabs(request_id, category)
    rows.append(tabs)

    if not chosen:
        rows.append([InlineKeyboardButton("هیچ فرمت معتبری در این دسته وجود ندارد", callback_data=f"noop:{request_id}")])
    else:
        for p in chosen[start:end]:
            size_text = p.get("size_text") or "—"
            label = p.get("label") or (p.get("resolution") or p.get("ext") or "file")
            if p.get("is_audio_only"):
                text = f"{label} • {size_text} • {p['ext']} • audio-only"
            elif p.get("is_video_only"):
                text = f"{label} • {size_text} • {p['ext']} • video-only"
            else:
                text = f"{label} • {size_text} • {p['ext']}"
            if p.get("is_preview"):
                cb = f"dl:{request_id}:best"
            else:
                cb = f"dl:{request_id}:{p['format_id']}"
            rows.append([InlineKeyboardButton(text, callback_data=cb)])

    nav = []
    total = len(chosen)
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"cat:{request_id}:{category}:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"cat:{request_id}:{category}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("🎥 بهترین کیفیت", callback_data=f"dl:{request_id}:best"),
        InlineKeyboardButton("🎧 فقط صدا (MP3)", callback_data=f"dl:{request_id}:audio"),
        InlineKeyboardButton("❌ لغو", callback_data="cancel")
    ])
    return InlineKeyboardMarkup(rows)

def make_output_mode_keyboard(request_id, format_id):
    rows = [
        [InlineKeyboardButton("💾 ذخیره در لپ‌تاپ", callback_data=f"mode:{request_id}:{format_id}:local")],
        [InlineKeyboardButton("📤 ارسال به تلگرام", callback_data=f"mode:{request_id}:{format_id}:telegram")],
        [InlineKeyboardButton("❌ لغو", callback_data=f"cancel")]
    ]
    return InlineKeyboardMarkup(rows)

def make_cancel_markup(task_id, owner_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو دانلود/آپلود", callback_data=f"cancel_dl:{task_id}:{owner_id}")]])

# -------------------------
# progress hooks
# -------------------------
def ytdl_progress_hook_factory(task_id):
    def hook(d):
        d['task_id'] = task_id
        with CANCEL_LOCK:
            flag = CANCEL_FLAGS.get(task_id)
            if flag and flag.get("cancel"):
                raise Exception("دانلود توسط کاربر لغو شد")
        ytdl_progress_hook(d)
    return hook

def ytdl_progress_hook(d):
    task_id = d.get("task_id")
    if not task_id:
        return
    info_map = progress_map.get(task_id)
    if not info_map:
        return
    bot = info_map.get("bot")
    chat_id = info_map.get("chat_id")
    msg_id = info_map.get("msg_id")
    owner_id = info_map.get("owner_id")
    status = d.get("status")

    with CANCEL_LOCK:
        cancel_entry = CANCEL_FLAGS.get(task_id)
    reply_markup = None
    if cancel_entry:
        owner = cancel_entry.get("owner_id")
        reply_markup = make_cancel_markup(task_id, owner)

    try:
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

            if not total:
                info = info_map.get("info") or {}
                total = info.get("filesize") or 0
                if not total:
                    req_fmts = info.get("requested_formats") or info.get("formats") or []
                    ssum = 0
                    for rf in req_fmts:
                        s = rf.get("filesize") or rf.get("filesize_approx") or 0
                        ssum += s or 0
                    if ssum:
                        total = ssum

            speed = d.get("speed") or 0
            eta = d.get("eta")
            if total:
                try:
                    pct = int(downloaded * 100 / total)
                except:
                    pct = 0
            else:
                pct = 0

            bar_len = 10
            filled = int(bar_len * pct / 100) if pct else 0
            bar = "🟦" * filled + "⬜" * (bar_len - filled)

            speed_mb = (speed / 1024 / 1024) if speed else 0
            total_mb = (total / 1024 / 1024) if total else 0
            downloaded_mb = (downloaded / 1024 / 1024) if downloaded else 0
            eta_text = time.strftime("%M:%S", time.gmtime(eta)) if eta else "—"

            text = (
                f"📥 دانلود:\n"
                f"{bar} {pct}%\n"
                f"⚡ سرعت: {speed_mb:.2f} MB/s\n"
                f"📦 حجم: {downloaded_mb:.2f} MB از {total_mb:.2f} MB\n"
                f"⏳ زمان باقی‌مانده: {eta_text}"
            )

            now = time.time()
            last_log = info_map.get("last_log_ts", 0)
            if now - last_log > 5:
                append_user_log(get_log_key_for_user(owner_id), {
                    "event": "download_progress",
                    "task_id": task_id,
                    "downloaded_bytes": downloaded,
                    "total_bytes": total,
                    "speed": speed,
                    "eta": eta,
                    "pct": pct
                })
                info_map["last_log_ts"] = now
                progress_map[task_id] = info_map

            if now - info_map.get("last_edit", 0) > 0.9:
                try:
                    if reply_markup:
                        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=reply_markup)
                    else:
                        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
                except:
                    pass
            info_map["last_edit"] = now

        elif status == "finished":
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="دانلود تمام شد. در حال آماده‌سازی...")
            except:
                pass
    except Exception:
        pass

# -------------------------
# yt-dlp download wrapper
# -------------------------
@retry_on_network_errors()
def yt_dlp_download_with_hook(url, outtmpl, format_spec=None, postprocessors=None, task_id=None, bot=None, chat_id=None, msg_id=None):
    ydl_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "fragment_retries": YTDLP_FRAGMENT_RETRIES,
        "http_chunk_size": YTDLP_HTTP_CHUNK_SIZE,
        "concurrent_fragment_downloads": 1,
        "http_headers": {"User-Agent": USER_AGENT_HEAD},
        "nocheckcertificate": True,
    }
    if format_spec:
        ydl_opts["format"] = format_spec
    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors
    hook = ytdl_progress_hook_factory(task_id)
    ydl_opts["progress_hooks"] = [hook]
    if task_id:
        progress_map[task_id] = progress_map.get(task_id, {})
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

# -------------------------
# upload with progress (Bot API)
# -------------------------
def upload_file_with_progress(bot_token, chat_id, file_path, caption, progress_callback, timeout=3600, task_id=None, api_method="sendDocument"):
    if api_method not in ("sendDocument", "sendAnimation"):
        api_method = "sendDocument"
    url = f"https://api.telegram.org/bot{bot_token}/{api_method}"
    filename = os.path.basename(file_path)
    field_name = "document" if api_method == "sendDocument" else "animation"

    session = requests.Session()
    session.trust_env = False
    session.headers.update({"Connection": "keep-alive", "User-Agent": USER_AGENT_HEAD})

    max_attempts = 4
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        fobj = None
        try:
            fobj = open(file_path, "rb")
            m = MultipartEncoder(fields={
                "chat_id": str(chat_id),
                "caption": caption or "",
                field_name: (filename, fobj, "application/octet-stream")
            })

            def monitor_callback(monitor):
                try:
                    if task_id:
                        with CANCEL_LOCK:
                            flag = CANCEL_FLAGS.get(task_id)
                            if flag and flag.get("cancel"):
                                raise Exception("Upload cancelled by user")
                    progress_callback(monitor.bytes_read, m.len)
                except Exception:
                    raise

            monitor = MultipartEncoderMonitor(m, monitor_callback)
            headers = {"Content-Type": monitor.content_type}

            r = session.post(
                url,
                data=monitor,
                headers=headers,
                timeout=timeout,
                proxies={"http": None, "https": None},
            )
            return r
        except Exception as e:
            last_exc = e
            with CANCEL_LOCK:
                flag = CANCEL_FLAGS.get(task_id)
                if flag and flag.get("cancel"):
                    raise Exception("Upload cancelled by user") from e

            is_network = False
            if isinstance(e, (requests.exceptions.RequestException, ssl.SSLError, NetworkError)):
                is_network = True
            elif Urllib3SSLError and isinstance(e, Urllib3SSLError):
                is_network = True

            if not is_network or attempt >= max_attempts:
                raise

            wait = min(6 * attempt, 20)
            logger.warning(
                "Bot API upload attempt %d/%d failed: %s. Retrying in %ds...",
                attempt, max_attempts, str(e)[:150], wait
            )
            time.sleep(wait)
        finally:
            if fobj:
                try:
                    fobj.close()
                except Exception:
                    pass

    try:
        session.close()
    except Exception:
        pass
    raise last_exc if last_exc else RuntimeError("Upload failed after retries")

# -------------------------
# Telethon send helper
# -------------------------
def telethon_send_file(chat_id, file_path, caption=None, progress_callback=None, task_id=None):
    client = ensure_telethon_client()
    if not client:
        raise RuntimeError("Telethon client not configured or not available.")
    file_size = None
    try:
        file_size = os.path.getsize(file_path)
    except Exception:
        file_size = None

    def choose_part_size_kb(size_bytes):
        if not size_bytes:
            return 512
        mb = size_bytes / (1024 * 1024)
        if mb <= 50:
            return 512
        if mb <= 200:
            return 1024
        if mb <= 1024:
            return 2048
        return 4096

    part_size_kb = choose_part_size_kb(file_size)

    max_attempts = 5
    attempt = 0
    last_exc = None

    while attempt < max_attempts:
        attempt += 1
        try:
            def wrapped_progress(sent, total):
                if task_id:
                    with CANCEL_LOCK:
                        flag = CANCEL_FLAGS.get(task_id)
                        if flag and flag.get("cancel"):
                            raise Exception("Upload cancelled by user")
                if progress_callback:
                    progress_callback(sent, total)

            coro = client.send_file(entity=chat_id, file=file_path, caption=caption or "", progress_callback=wrapped_progress, part_size_kb=part_size_kb)
            client.loop.run_until_complete(coro)
            return True
        except Exception as e:
            last_exc = e
            with CANCEL_LOCK:
                flag = CANCEL_FLAGS.get(task_id)
                if flag and flag.get("cancel"):
                    raise Exception("Upload cancelled by user")
            wait = min(10 * attempt, 60)
            jitter = random.uniform(0, 2.0)
            logger.warning("Telethon upload attempt %d failed: %s. Retrying in %ds (+%.2fs jitter) (part_size_kb=%d).", attempt, str(e), wait, jitter, part_size_kb)
            time.sleep(wait + jitter)
            if part_size_kb < 4096:
                part_size_kb = min(4096, int(part_size_kb * 2))
            continue

    raise last_exc if last_exc else RuntimeError("Unknown error during Telethon upload")

# -------------------------
# upload selection
# -------------------------
def upload_with_smart_choice(bot_token, bot, chat_id, file_path, caption, progress_update_fn=None, task_id=None, as_animation=False):
    total_size = os.path.getsize(file_path)

    def telethon_progress(sent, total):
        try:
            if progress_update_fn:
                progress_update_fn(sent, total)
        except Exception:
            raise

    if FORCE_TELETHON_ALWAYS:
        if total_size > MAX_SINGLE_UPLOAD_BYTES:
            raise RuntimeError(f"File size {human_size(total_size)} exceeds 2 GiB limit.")
        return telethon_send_file(chat_id, file_path, caption=caption, progress_callback=telethon_progress, task_id=task_id)

    if total_size > MAX_SINGLE_UPLOAD_BYTES:
        raise RuntimeError(f"File size {human_size(total_size)} exceeds 2 GiB limit.")

    threshold = 30 * 1024 * 1024
    if total_size <= threshold:
        api_method = "sendAnimation" if as_animation else "sendDocument"
        try:
            resp = upload_file_with_progress(
                bot_token, chat_id, file_path, caption,
                progress_update_fn, timeout=3600, task_id=task_id, api_method=api_method
            )
            if resp is None:
                raise RuntimeError("No response from Telegram Bot API during upload.")
            if resp.status_code != 200:
                raise RuntimeError(f"Upload failed: {resp.status_code} {resp.text[:400]}")
            return resp
        except Exception as e:
            err_str = str(e).lower()
            network_keywords = (
                "proxy", "ssl", "connection", "timeout", "max retries",
                "eof", "ssleof", "unable to connect", "network"
            )
            if any(k in err_str for k in network_keywords):
                logger.warning("Bot API failed with network/proxy error, falling back to Telethon: %s", str(e)[:150])
            else:
                raise

    return telethon_send_file(
        chat_id, file_path,
        caption=caption,
        progress_callback=telethon_progress,
        task_id=task_id
    )

# -------------------------
# Worker logic
# -------------------------
def download_worker_thread(bot):
    global active_workers
    while True:
        task = download_queue.get()
        if task is None:
            break
        with active_workers_lock:
            active_workers += 1
        try:
            chat_id = task.get("chat_id")
            url = task.get("url")
            action = task.get("action")
            format_id = task.get("format_id")
            owner_id = task.get("user_id")
            username = task.get("username") or None
            request_id = task.get("request_id")
            request_info = task.get("request_info")
            mode = task.get("mode") or "telegram"

            task_id = uuid.uuid4().hex[:12]
            with CANCEL_LOCK:
                CANCEL_FLAGS[task_id] = {"cancel": False, "owner_id": owner_id}

            try:
                progress_msg = bot.send_message(chat_id=chat_id, text="در حال آماده‌سازی دانلود...", reply_markup=make_cancel_markup(task_id, owner_id))
                progress_msg_id = progress_msg.message_id
            except Exception:
                progress_msg = bot.send_message(chat_id=chat_id, text="در حال آماده‌سازی دانلود...")
                progress_msg_id = progress_msg.message_id

            append_user_log(get_log_key_for_user(owner_id), {
                "event": "download_request",
                "url": url,
                "format_requested": format_id or "best",
                "request_id": request_id,
                "task_id": task_id,
                "mode": mode
            })

            if request_id:
                with REQUESTS_LOCK:
                    req = REQUESTS.get(request_id)
                if req and req.get("cancel"):
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text="❌ این درخواست قبلاً لغو شده است.")
                    except:
                        pass
                    with CANCEL_LOCK:
                        CANCEL_FLAGS.pop(task_id, None)
                    continue

            info = None
            if request_info:
                info = request_info
            else:
                try:
                    info = extract_info_safe(url)
                except Exception as e:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❗ خطا در استخراج اطلاعات: {str(e)}")
                    except:
                        pass
                    with CANCEL_LOCK:
                        CANCEL_FLAGS.pop(task_id, None)
                    continue

            final_format_spec = None
            if action == "audio":
                final_format_spec = "bestaudio/best"
            else:
                if not format_id or format_id == "best":
                    if is_youtube_url(url):
                        final_format_spec = "bestvideo+bestaudio/best"
                    else:
                        final_format_spec = "best"
                else:
                    parsed = parse_formats_from_info(info) if info else []
                    found = any(p.get("format_id") == format_id for p in parsed)
                    if not found:
                        if is_youtube_url(url):
                            final_format_spec = "bestvideo+bestaudio/best"
                        else:
                            final_format_spec = "best"
                    else:
                        final_format_spec = f"{format_id}+bestaudio/best"

            safe_base = sanitize_name((info.get("title") if info else "file") or "file")
            outtmpl = os.path.join(DOWNLOAD_ROOT, f"{safe_base}.%(ext)s")
            progress_map[task_id] = {
                "bot": bot,
                "chat_id": chat_id,
                "msg_id": progress_msg_id,
                "owner_id": owner_id,
                "username": username,
                "info": info,
                "last_edit": 0,
                "last_log_ts": 0
            }

            try:
                filename = yt_dlp_download_with_hook(url, outtmpl, format_spec=final_format_spec, task_id=task_id, bot=bot, chat_id=chat_id, msg_id=progress_msg_id)
            except Exception as e:
                with CANCEL_LOCK:
                    flag = CANCEL_FLAGS.get(task_id)
                    was_cancelled = flag and flag.get("cancel")
                if was_cancelled:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text="❌ دانلود توسط کاربر لغو شد.")
                    except:
                        pass
                else:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❗ خطا در دانلود: {str(e)}")
                    except:
                        pass
                with CANCEL_LOCK:
                    CANCEL_FLAGS.pop(task_id, None)
                continue

            found = None
            try:
                if os.path.exists(filename):
                    found = filename
                else:
                    base_no_ext = os.path.splitext(os.path.basename(outtmpl))[0]
                    for p in Path(DOWNLOAD_ROOT).glob(f"{base_no_ext}.*"):
                        if p.is_file():
                            found = str(p)
                            break
            except Exception:
                found = None

            if not found:
                try:
                    bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text="❗ فایل دانلود شده پیدا نشد")
                except:
                    pass
                with CANCEL_LOCK:
                    CANCEL_FLAGS.pop(task_id, None)
                continue

            try:
                file_size = os.path.getsize(found)
            except Exception:
                file_size = None

            def upload_progress_cb(sent, total):
                try:
                    pct = int(sent * 100 / total) if total else 0
                except:
                    pct = 0
                bar_len = 10
                filled = int(bar_len * pct / 100) if pct else 0
                bar = "🟩" * filled + "⬜" * (bar_len - filled)
                text = (
                    f"📤 آپلود:\n"
                    f"{bar} {pct}%\n"
                    f"📦 ارسال شده: {human_size(sent)} از {human_size(total)}"
                )
                now = time.time()
                last = progress_map.get(task_id, {}).get("last_edit", 0)
                if now - last > 1.0:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=text, reply_markup=make_cancel_markup(task_id, owner_id))
                    except:
                        pass
                    progress_map[task_id]["last_edit"] = now

            try:
                if file_size and file_size > MAX_SINGLE_UPLOAD_BYTES:
                    bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❗ فایل بسیار بزرگ است ({human_size(file_size)}).", reply_markup=make_cancel_markup(task_id, owner_id))
                    with CANCEL_LOCK:
                        CANCEL_FLAGS.pop(task_id, None)
                    try:
                        if os.path.exists(found):
                            os.remove(found)
                    except:
                        pass
                    continue

                if mode == "local":
                    try:
                        udir = ensure_user_dir(username or owner_id)
                        dest_dir = udir / "downloads"
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_name = os.path.basename(found)
                        dest_path = dest_dir / dest_name
                        if dest_path.exists():
                            base, ext = os.path.splitext(dest_name)
                            dest_path = dest_dir / f"{base}_{int(time.time())}{ext}"
                        shutil.move(found, str(dest_path))
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"💾 فایل در پوشهٔ محلی ذخیره شد: {str(dest_path)}")
                        with CANCEL_LOCK:
                            CANCEL_FLAGS.pop(task_id, None)
                        continue
                    except Exception as e:
                        try:
                            bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❗ خطا در ذخیره محلی: {str(e)}")
                        except:
                            pass

                ext = os.path.splitext(found)[1].lower()
                is_gif = ext == ".gif"
                bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"⬆️ در حال آپلود فایل ({human_size(file_size)}) ...", reply_markup=make_cancel_markup(task_id, owner_id))

                if is_gif:
                    if file_size and file_size <= CHUNK_SIZE:
                        upload_with_smart_choice(TOKEN, bot, chat_id, found, caption=info.get("title") or "", progress_update_fn=upload_progress_cb, task_id=task_id, as_animation=True)
                    else:
                        upload_with_smart_choice(TOKEN, bot, chat_id, found, caption=info.get("title") or "", progress_update_fn=upload_progress_cb, task_id=task_id, as_animation=False)
                else:
                    upload_with_smart_choice(TOKEN, bot, chat_id, found, caption=info.get("title") or "", progress_update_fn=upload_progress_cb, task_id=task_id, as_animation=False)

                try:
                    bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text="✅ آپلود انجام شد")
                except:
                    pass
            except Exception as e:
                with CANCEL_LOCK:
                    flag = CANCEL_FLAGS.get(task_id)
                    was_cancelled = flag and flag.get("cancel")
                if was_cancelled:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text="❌ عملیات توسط کاربر لغو شد.")
                    except:
                        pass
                else:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❗ خطا در آپلود: {str(e)}")
                    except:
                        pass
                with CANCEL_LOCK:
                    CANCEL_FLAGS.pop(task_id, None)
                try:
                    if os.path.exists(found):
                        os.remove(found)
                except:
                    pass
                continue

            try:
                if os.path.exists(found):
                    os.remove(found)
            except:
                pass

            log_entry = {"event": "job_finished", "url": url, "request_id": request_id, "user_id": owner_id, "task_id": task_id}
            append_user_log(get_log_key_for_user(owner_id), log_entry)

            with CANCEL_LOCK:
                CANCEL_FLAGS.pop(task_id, None)

        except Exception as e:
            try:
                with open(LOG_ROOT / "worker_exceptions.log", "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().isoformat()} - Worker exception: {str(e)}\n")
                    f.write(traceback.format_exc())
                    f.write("\n" + ("-" * 60) + "\n")
            except:
                pass
        finally:
            with active_workers_lock:
                active_workers -= 1

# -------------------------
# Channel / playlist helpers
# -------------------------
def build_channel_url_from_info(info):
    channel_url = info.get("channel_url") or info.get("webpage_url") or info.get("url")
    if channel_url and isinstance(channel_url, str) and channel_url.startswith("http"):
        return channel_url
    uploader_id = info.get("uploader_id") or info.get("uploader")
    if uploader_id:
        if str(uploader_id).startswith("UC"):
            return f"https://www.youtube.com/channel/{uploader_id}"
        handle = str(uploader_id).lstrip("@")
        return f"https://www.youtube.com/@{handle}"
    return None

def fetch_channel_info(channel_query):
    ydl_opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            if isinstance(channel_query, str) and (channel_query.startswith("http") or "youtube.com" in channel_query):
                info = ydl.extract_info(channel_query, download=False)
                if info and info.get("extractor"):
                    return info
        except Exception:
            pass
        try:
            search_q = f"ytsearch5:channel {channel_query}"
            res = ydl.extract_info(search_q, download=False)
            entries = res.get("entries", []) or []
            for e in entries:
                if e and e.get("extractor") and "channel" in e.get("extractor"):
                    return e
        except Exception as e:
            raise e

def fetch_channel_videos(channel_url_or_id, max_results=200):
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(channel_url_or_id, download=False)
        except Exception:
            try:
                info = ydl.extract_info(channel_url_or_id.rstrip("/") + "/videos", download=False)
            except Exception:
                try:
                    info = ydl.extract_info(f"ytsearch{max_results}:channel {channel_url_or_id}", download=False)
                except Exception as e:
                    raise e
    entries = info.get("entries", []) or []
    videos = []
    for e in entries:
        vid = e.get("id")
        url = e.get("url") or vid
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        videos.append({
            "id": vid,
            "title": e.get("title"),
            "url": url,
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnail")
        })
        if len(videos) >= max_results:
            break
    return videos

def fetch_channel_playlists(channel_url_or_id):
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url_or_id, download=False)
    playlists = []
    for k in ("playlists", "entries"):
        for e in info.get(k, []) or []:
            pid = e.get("id")
            url = e.get("url") or pid
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/playlist?list={url}"
            playlists.append({
                "id": pid,
                "title": e.get("title"),
                "url": url
            })
    return playlists

# -------------------------
# Handlers and multi-link support
# -------------------------
def start(update, context):
    update.message.reply_text("سلام! لینک یا یوزرنیم کانال را ارسال کن یا از /channel استفاده کن.")

def process_single_link(update, context, link):
    chat_id = update.message.chat_id
    user = update.message.from_user
    user_id = user.id
    username = user.username or str(user_id)

    with USER_MAP_LOCK:
        USER_MAP[user_id] = username

    request_id = uuid.uuid4().hex[:12]
    anim_key = f"quality_anim_{request_id}"

    try:
        quality_ali_anim.start(anim_key, bot=context.bot, chat_id=chat_id, title="در حال بررسی کیفیت…", min_interval=0.9)
    except Exception:
        pass

    try:
        info = extract_info_safe(link)
    except ExtractError as e:
        try:
            quality_ali_anim.stop(anim_key, final_text=f"❗ خطا در استخراج اطلاعات: {str(e)}")
        except:
            pass
        context.bot.send_message(chat_id=chat_id, text=f"❗ خطا: {str(e)}")
        return
    except Exception as e:
        try:
            quality_ali_anim.stop(anim_key, final_text=f"❗ خطا در استخراج اطلاعات: {str(e)}")
        except:
            pass
        context.bot.send_message(chat_id=chat_id, text=f"❗ خطا در استخراج اطلاعات: {str(e)}")
        return

    parsed_formats = parse_formats_from_info(info)
    title = info.get("title") or "بدون عنوان"
    uploader = info.get("uploader") or info.get("uploader_id") or ""
    duration = info.get("duration")
    views = info.get("view_count")
    meta = f"🎬 {title}\n📺 {uploader}\n⏱ {time.strftime('%M:%S', time.gmtime(duration)) if duration else '—'}  •  👁 {views or '—'}"

    with REQUESTS_LOCK:
        REQUESTS[request_id] = {
            "type": "formats",
            "url": link,
            "formats": parsed_formats,
            "info": info,
            "created": time.time(),
            "user_id": user_id,
            "cancel": False,
            "error": None,
            "progress_msg_id": None
        }

    try:
        msg = context.bot.send_message(chat_id=chat_id, text=f"{meta}\n\nدر حال آماده‌سازی کیبورد...", reply_markup=make_request_cancel_markup(request_id, user_id))
        with REQUESTS_LOCK:
            REQUESTS[request_id]["progress_msg_id"] = msg.message_id
    except Exception:
        msg = context.bot.send_message(chat_id=chat_id, text=f"{meta}\n\nدر حال آماده‌سازی کیبورد...")
        with REQUESTS_LOCK:
            REQUESTS[request_id]["progress_msg_id"] = msg.message_id

    def build_and_attach_keyboard(rid, bot, chat_id, msg_id):
        with REQUESTS_LOCK:
            req = REQUESTS.get(rid)
        if not req:
            try:
                quality_ali_anim.stop(anim_key)
            except:
                pass
            return
        if req.get("cancel"):
            try:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="❌ این درخواست لغو شد.")
            except:
                pass
            try:
                quality_ali_anim.stop(anim_key, final_text="❌ این درخواست لغو شد.")
            except:
                pass
            return
        parsed = req.get("formats") or []
        exts = {f.get("ext") for f in parsed}
        default_cat = "mp4" if "mp4" in exts else ("webm" if "webm" in exts else "other")
        kb = make_quality_keyboard(parsed, rid, category=default_cat, page=0)
        try:
            bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=kb)
            try:
                quality_ali_anim.stop(anim_key)
            except:
                pass
        except:
            try:
                bot.send_message(chat_id=chat_id, text="کیبورد آماده شد.", reply_markup=kb)
                try:
                    quality_ali_anim.stop(anim_key)
                except:
                    pass
            except:
                try:
                    quality_ali_anim.stop(anim_key)
                except:
                    pass

    threading.Thread(target=build_and_attach_keyboard, args=(request_id, context.bot, chat_id, msg.message_id), daemon=True).start()

def handle_channel_cmd(update, context):
    q = " ".join(context.args).strip()
    if not q:
        update.message.reply_text("مثال: /channel @honarnewsofficial_ یا /channel soheilprank")
        return
    if q.startswith("@"):
        q = q[1:]
    msg = update.message.reply_text("در حال جستجوی کانال...")
    try:
        info = fetch_channel_info(q)
    except Exception as e:
        msg.edit_text(f"خطا در یافتن کانال: {str(e)}")
        return
    if not info:
        msg.edit_text("کانالی یافت نشد.")
        return

    channel_url = build_channel_url_from_info(info)
    if not channel_url:
        try:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                search_res = ydl.extract_info(f"ytsearch:channel {info.get('title')}", download=False)
                entries = search_res.get("entries", []) or []
                if entries:
                    channel_url = entries[0].get("webpage_url") or entries[0].get("url")
        except:
            channel_url = None

    title = info.get("title") or info.get("uploader") or "کانال"
    username = info.get("uploader_id") or info.get("webpage_url") or ""
    subs = info.get("subscriber_count") or "—"
    videos_count = info.get("video_count") or "—"
    views = info.get("view_count") or "—"
    created = info.get("upload_date") or ""
    thumb = info.get("thumbnail")
    text = (
        f"📛 {title}\n"
        f"👤 {username}\n"
        f"📊 مشترکین: {subs}  •  ویدیوها: {videos_count}  •  بازدیدها: {views}\n"
        f"🗓 {created}"
    )

    req_id = uuid.uuid4().hex[:12]
    with REQUESTS_LOCK:
        REQUESTS[req_id] = {
            "type": "channel_card",
            "channel_url": channel_url,
            "info": info,
            "created": time.time(),
            "user_id": update.message.from_user.id,
            "cancel": False,
            "error": None,
            "progress_msg_id": None
        }

    buttons = [
        [InlineKeyboardButton("🎬 مشاهده ویدیوها", callback_data=f"chan:videos:{req_id}")],
        [InlineKeyboardButton("📁 مشاهده پلی‌لیست‌ها", callback_data=f"chan:playlists:{req_id}")]
    ]
    try:
        msg.delete()
    except:
        pass
    if thumb:
        update.message.reply_photo(photo=thumb, caption=text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

def handle_message(update, context):
    text = (update.message.text or "").strip()
    chat_id = update.message.chat_id
    user = update.message.from_user
    user_id = user.id
    username = user.username or str(user_id)

    with USER_MAP_LOCK:
        USER_MAP[user_id] = username

    if not text:
        update.message.reply_text("لینک یا عبارت را ارسال کن.")
        return

    links = re.findall(r'https?://\S+', text)
    if len(links) > 1:
        for link in links:
            try:
                context.bot.send_message(chat_id=chat_id, text="لینک دریافت شد. در حال آماده‌سازی...")
            except:
                pass
            process_single_link(update, context, link)
        return

    if text.startswith("@"):
        uname = text[1:].strip()
        if not uname:
            update.message.reply_text("یوزرنیم نامعتبر است.")
            return
        return search_youtube_channel(uname, update, context)

    if text.startswith("http"):
        try:
            ack_msg = context.bot.send_message(chat_id=chat_id, text="لینک دریافت شد. در حال بررسی...")
        except:
            ack_msg = None
        if is_instagram_or_x(text):
            try:
                context.bot.send_message(chat_id=chat_id, text="🔎 لینک شبکه اجتماعی شناسایی شد — در حال بررسی کیفیت و آماده‌سازی گزینه‌ها...")
            except:
                pass
            process_single_link(update, context, text)
            return
        process_single_link(update, context, text)
        return

    update.message.reply_text("متن دریافتی لینک نیست؛ برای جستجو از /search یا /channel استفاده کن یا یوزرنیم کانال را با @ ارسال کن.")

# -------------------------
# Callback handler
# -------------------------
def send_channel_videos_page_by_req(req_id, page, bot, callback_query):
    with REQUESTS_LOCK:
        req = REQUESTS.get(req_id)
    if not req:
        try:
            safe_edit_message(bot, callback_query.message.chat_id, callback_query.message.message_id, "زمان درخواست منقضی شد.")
        except:
            pass
        return
    items = req.get("items", [])
    per = 10
    start = page*per
    end = start+per
    text = "ویدیوهای کانال:\n\n"
    buttons = []
    for idx, v in enumerate(items[start:end], start+1):
        dur = v.get("duration")
        text += f"{start+idx}. {time.strftime('%M:%S', time.gmtime(dur)) if dur else '—'}  {v['title'][:60]}\n"
        buttons.append([InlineKeyboardButton("⬇ دانلود", callback_data=f"dl_direct:{v['url']}")])
    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"chanpage:{req_id}:{page-1}"))
    if end < len(items):
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"chanpage:{req_id}:{page+1}"))
    if nav:
        buttons.append(nav)
    try:
        callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except:
        try:
            callback_query.answer()
        except:
            pass

def channel_callback_handler(update, context):
    query = update.callback_query
    data = query.data
    user = query.from_user
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    if data == "cancel":
        try:
            safe_edit_message(context.bot, chat_id, message_id, "لغو شد.")
        except:
            pass
        return

    if data.startswith("cancel_req:"):
        try:
            _, request_id, owner_id = data.split(":", 2)
            owner_id = int(owner_id)
        except:
            query.answer()
            return
        if user.id != owner_id:
            query.answer("فقط صاحب درخواست می‌تواند آن را لغو کند.")
            return
        with REQUESTS_LOCK:
            req = REQUESTS.get(request_id)
            if req:
                req["cancel"] = True
                req["error"] = "درخواست توسط کاربر لغو شد."
                REQUESTS[request_id] = req
        with CANCEL_LOCK:
            for t_id, entry in list(CANCEL_FLAGS.items()):
                if entry.get("owner_id") == owner_id:
                    CANCEL_FLAGS[t_id]["cancel"] = True
        try:
            query.edit_message_text("❌ درخواست لغو شد.")
        except:
            pass
        query.answer("درخواست لغو شد")
        return

    if data.startswith("chan:videos:"):
        _, _, req_id = data.split(":", 2)
        with REQUESTS_LOCK:
            req = REQUESTS.get(req_id)
        if not req:
            safe_edit_message(context.bot, chat_id, message_id, "زمان درخواست منقضی شده.")
            return
        if not req.get("items"):
            try:
                channel_url = req.get("channel_url")
                videos = fetch_channel_videos(channel_url, max_results=200)
                req["items"] = videos
                with REQUESTS_LOCK:
                    REQUESTS[req_id] = req
            except Exception as e:
                safe_edit_message(context.bot, chat_id, message_id, f"خطا در دریافت ویدیوها: {str(e)}")
                return
        send_channel_videos_page_by_req(req_id, 0, context.bot, query)
        return

    if data.startswith("chan:playlists:"):
        _, _, req_id = data.split(":", 2)
        with REQUESTS_LOCK:
            req = REQUESTS.get(req_id)
        if not req:
            safe_edit_message(context.bot, chat_id, message_id, "زمان درخواست منقضی شده.")
            return
        try:
            channel_url = req.get("channel_url")
            playlists = fetch_channel_playlists(channel_url)
            req["playlists"] = playlists
            with REQUESTS_LOCK:
                REQUESTS[req_id] = req
            buttons = []
            for pl in playlists[:20]:
                buttons.append([InlineKeyboardButton(pl.get("title")[:50], callback_data=f"playlist_queue:{pl.get('url')}")])
            try:
                query.edit_message_text("پلی‌لیست‌ها:", reply_markup=InlineKeyboardMarkup(buttons))
            except:
                pass
        except Exception as e:
            safe_edit_message(context.bot, chat_id, message_id, f"خطا در دریافت پلی‌لیست‌ها: {str(e)}")
        return

    if data.startswith("playlist_queue:"):
        try:
            _, url = data.split(":", 1)
        except:
            query.answer()
            return
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = info.get("entries", []) or []
            videos = []
            for e in entries:
                vid = e.get("id")
                vurl = e.get("url") or vid
                if vurl and not vurl.startswith("http"):
                    vurl = f"https://www.youtube.com/watch?v={vurl}"
                videos.append(vurl)
            for v in videos:
                download_queue.put({
                    "group_id": url,
                    "user_id": user.id,
                    "username": user.username or str(user.id),
                    "url": v,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "action": "video",
                    "format_id": "best",
                    "mode": "telegram",
                    "request_id": None,
                    "request_info": None
                })
            safe_edit_message(context.bot, chat_id, message_id, f"پلی‌لیست با {len(videos)} ویدیو به صف اضافه شد.")
        except Exception as e:
            safe_edit_message(context.bot, chat_id, message_id, f"خطا در صف‌بندی پلی‌لیست: {str(e)}")
        return

    if data.startswith("cancel_dl:"):
        try:
            _, task_id, owner_id = data.split(":", 2)
            owner_id = int(owner_id)
        except:
            query.answer()
            return
        if user.id != owner_id:
            query.answer("فقط کاربری که دانلود را شروع کرده می‌تواند آن را لغو کند.")
            return
        with CANCEL_LOCK:
            if task_id in CANCEL_FLAGS:
                CANCEL_FLAGS[task_id]["cancel"] = True
        try:
            safe_edit_message(context.bot, chat_id, message_id, "❌ درخواست لغو ارسال شد. در حال متوقف کردن دانلود/آپلود...")
        except:
            pass
        query.answer("درخواست لغو ارسال شد")
        return

    if data.startswith("cat:"):
        try:
            _, request_id, category, page = data.split(":", 3)
            page = int(page)
        except:
            query.answer()
            return
        with REQUESTS_LOCK:
            req = REQUESTS.get(request_id)
        if not req:
            query.answer("زمان درخواست منقضی شده؛ لطفاً لینک را دوباره ارسال کن.")
            return
        parsed = req.get("formats", [])
        kb = make_quality_keyboard(parsed, request_id, category=category, page=page)
        try:
            query.edit_message_reply_markup(reply_markup=kb)
        except:
            try:
                query.answer()
            except:
                pass
        return

    if data.startswith("dl:"):
        try:
            _, request_id, fmt = data.split(":", 2)
        except:
            query.answer()
            return
        with REQUESTS_LOCK:
            req = REQUESTS.get(request_id)
        if not req:
            safe_edit_message(context.bot, chat_id, message_id, "زمان درخواست منقضی شده؛ لطفاً لینک را دوباره ارسال کن.")
            return
        if req.get("user_id") != user.id:
            query.answer("فقط کاربری که لینک را ارسال کرده می‌تواند این گزینه را انتخاب کند.")
            return
        if req.get("cancel"):
            safe_edit_message(context.bot, chat_id, message_id, "این درخواست قبلاً لغو شده است.")
            return
        url = req["url"]
        if fmt == "best":
            action = "video"
            format_id = "best"
        elif fmt == "audio":
            action = "audio"
            format_id = None
        else:
            action = "video"
            format_id = fmt
        try:
            query.edit_message_text(text="می‌خوای فایل چطور تحویل داده بشه؟", reply_markup=make_output_mode_keyboard(request_id, format_id or "best"))
        except:
            try:
                query.answer()
            except:
                pass
        return

    if data.startswith("mode:"):
        try:
            _, request_id, fmt, mode = data.split(":", 3)
        except:
            query.answer()
            return
        with REQUESTS_LOCK:
            req = REQUESTS.get(request_id)
        if not req:
            safe_edit_message(context.bot, chat_id, message_id, "زمان درخواست منقضی شده؛ لطفاً لینک را دوباره ارسال کن.")
            return
        if req.get("user_id") != user.id:
            query.answer("فقط کاربری که لینک را ارسال کرده می‌تواند این گزینه را انتخاب کند.")
            return
        if req.get("cancel"):
            safe_edit_message(context.bot, chat_id, message_id, "این درخواست قبلاً لغو شده است.")
            return
        url = req["url"]
        action = "audio" if fmt == "audio" else "video"
        format_id = fmt if fmt != "best" else "best"
        download_queue.put({
            "group_id": None,
            "user_id": user.id,
            "username": user.username or str(user.id),
            "url": url,
            "chat_id": chat_id,
            "message_id": message_id,
            "action": action,
            "format_id": format_id,
            "mode": mode,
            "request_id": request_id,
            "request_info": req.get("info")
        })
        try:
            safe_edit_message(context.bot, chat_id, message_id, "✅ درخواست دریافت شد و به صف اضافه شد.")
        except:
            pass
        query.answer("به صف اضافه شد")
        return

    if data.startswith("dl_direct:"):
        try:
            _, vurl = data.split(":", 1)
        except:
            query.answer()
            return
        download_queue.put({
            "group_id": None,
            "user_id": user.id,
            "username": user.username or str(user.id),
            "url": vurl,
            "chat_id": chat_id,
            "message_id": message_id,
            "action": "video",
            "format_id": "best",
            "mode": "telegram",
            "request_id": None,
            "request_info": None
        })
        try:
            safe_edit_message(context.bot, chat_id, message_id, "✅ درخواست دانلود مستقیم به صف اضافه شد.")
        except:
            pass
        query.answer("به صف اضافه شد")
        return

    if data.startswith("noop:"):
        try:
            query.answer()
        except:
            pass
        return

    query.answer()

# -------------------------
# main (Webhook mode for Render, Polling for Laptop)
# -------------------------
def start_workers(bot, n=4):
    for _ in range(n):
        t = threading.Thread(target=download_worker_thread, args=(bot,), daemon=True)
        t.start()

def get_log_key_for_user(user_id):
    with USER_MAP_LOCK:
        uname = USER_MAP.get(user_id)
    return uname if uname else user_id

# ایجاد وب‌سرور Flask
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

# مسیر دریافت Webhook از تلگرام
@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return 'OK', 200

def main():
    global bot, dispatcher
    
    updater = Updater(TOKEN, use_context=True)
    bot = updater.bot
    dispatcher = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("channel", handle_channel_cmd))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(CallbackQueryHandler(channel_callback_handler))

    start_workers(bot, n=MAX_CONCURRENT_DOWNLOADS)

    if TELETHON_API_ID and TELETHON_API_HASH:
        try:
            ensure_telethon_client()
        except Exception as e:
            logger.exception("Telethon init failed: %s", e)

    if IS_ON_RENDER:
        # تنظیم Webhook در تلگرام
        WEBHOOK_URL = f"https://{os.environ.get('RENDER_EXTERNAL_URL', 'your-service-name.onrender.com')}/webhook/{TOKEN}"
        bot.set_webhook(url=WEBHOOK_URL)
        
        # اجرای وب‌سرور Flask برای دریافت پیام‌ها
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        # اگر روی لپ‌تاپ هستیم، فقط Polling عادی را اجرا می‌کنیم
        print("ربات روی لپ‌تاپ اجرا شد! (Polling Mode)")
        updater.start_polling()
        updater.idle()

if __name__ == "__main__":
    main()
