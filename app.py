from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ── Settings ────────────────────────────────────────────────────────────────
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
MAX_UPLOAD_MB = max(5, min(int(os.getenv("MAX_UPLOAD_MB", "47")), 47))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_VIDEO_HEIGHT = max(144, min(int(os.getenv("MAX_VIDEO_HEIGHT", "1080")), 2160))
MAX_ACTIVE_DOWNLOADS = max(1, min(int(os.getenv("MAX_ACTIVE_DOWNLOADS", "1")), 4))
MAX_JOB_AGE_SECONDS = 20 * 60
MAX_JOBS = 200
REQUEST_COOLDOWN_SECONDS = max(0, int(os.getenv("REQUEST_COOLDOWN_SECONDS", "5")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ALLOWED_USER_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
NODE_PATH = os.getenv("YTDLP_NODE_PATH", shutil.which("node") or "node")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ahm-x-downloader-bot")

bot_app: Application | None = None
jobs: dict[str, dict[str, Any]] = {}
active_users: set[int] = set()
active_job_by_user: dict[int, str] = {}
last_request_at: dict[int, float] = {}
maintenance_mode = False
download_semaphore = asyncio.Semaphore(MAX_ACTIVE_DOWNLOADS)

HEIGHT_STEPS = [144, 240, 360, 480, 540, 720, 1080, 1440, 2160]
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".aac", ".opus", ".ogg"}


# ── Errors and utility functions ────────────────────────────────────────────
class DownloadTooLarge(Exception):
    pass


class DownloadCancelled(Exception):
    pass


def extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text.strip(), flags=re.IGNORECASE)
    return match.group(0).rstrip(".,!؟)]}>") if match else None


def validate_public_url(url: str) -> None:
    """Reject malformed links, embedded credentials, and private/local hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("الرابط لازم يبدأ بـ http:// أو https://")
    if parsed.username or parsed.password:
        raise ValueError("الروابط اللي تحتوي بيانات دخول مش مسموح بيها.")
    if len(url) > 4096:
        raise ValueError("الرابط طويل جدًا.")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("الرابط لازم يكون من موقع عام على الإنترنت.")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            found = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {ipaddress.ip_address(row[4][0]) for row in found}
        except (socket.gaierror, OSError) as exc:
            raise ValueError("مش قادر أوصل للدومين الموجود في الرابط. راجع الرابط وجرب تاني.") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("الرابط لازم يكون من موقع عام على الإنترنت.")


def ytdlp_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "cachedir": False,
        "restrictfilenames": True,
        "js_runtimes": {"node": {"path": NODE_PATH}},
    }


def extract_info(url: str) -> dict[str, Any]:
    validate_public_url(url)
    options = ytdlp_options()
    options.update({"skip_download": True, "extract_flat": False})
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError("الموقع ما رجّعش بيانات فيديو صالحة.")
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = info.get("entries") or []
        info = next((entry for entry in entries if entry), None)
        if not isinstance(info, dict):
            raise ValueError("الرابط ده مفيهوش فيديو متاح.")
    return info


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "youtu.be" or host.endswith(("youtube.com", "youtube-nocookie.com"))


def friendly_error(url: str, exc: Exception) -> str:
    raw = str(exc or "")
    low = raw.lower()
    if is_youtube(url) and any(t in low for t in (
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
    )):
        return (
            "YouTube رفض الطلب من عنوان الاستضافة وطلب تحققًا إضافيًا. ده قرار من المنصة، "
            "ومش هنتجاوز تسجيل الدخول أو فحص الحماية. جرّب رابطًا عامًا آخر أو منصة مدعومة مختلفة."
        )
    if any(t in low for t in ("private video", "video is private", "login required", "members-only", "sign in")):
        return "الفيديو خاص أو يحتاج تسجيل دخول/صلاحية مش متوفرة للبوت. استخدم رابطًا عامًا لديك إذن بتنزيله."
    if "unsupported url" in low or "no suitable extractor" in low:
        return "الموقع أو نوع الرابط ده مش مدعوم حاليًا. جرّب رابط الفيديو نفسه بدل رابط الصفحة العامة."
    if "max-filesize" in low or "file is larger than" in low or "larger than max" in low:
        return f"حجم الفيديو أكبر من حد الإرسال الحالي ({MAX_UPLOAD_MB} MB). جرّب جودة أقل أو فيديو أقصر."
    if "timed out" in low or "timeout" in low:
        return "الموقع اتأخر في الرد. جرّب مرة تانية بعد شوية."
    if not raw or len(raw) > 350:
        return "الموقع رفض الطلب أو الرابط مش متاح حاليًا. جرّب رابطًا عامًا آخر."
    return raw


def get_size(fmt: dict[str, Any] | None) -> int | None:
    if not fmt:
        return None
    value = fmt.get("filesize") or fmt.get("filesize_approx")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def format_bytes(size: int | None) -> str:
    if not size or size <= 0:
        return "الحجم غير معروف"
    mib = size / (1024 * 1024)
    return f"≈ {mib:.1f} MB" if mib < 1024 else f"≈ {mib / 1024:.2f} GB"


def duration_text(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        return "غير معروف"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def video_height_choices(info: dict[str, Any]) -> list[int]:
    available = set()
    for fmt in info.get("formats") or []:
        try:
            height = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if height > 0 and height <= MAX_VIDEO_HEIGHT and fmt.get("vcodec") not in (None, "none"):
            available.add(height)
    if not available:
        # Some extractors only expose one progressive format at the top level.
        try:
            height = int(info.get("height") or 0)
            if 0 < height <= MAX_VIDEO_HEIGHT:
                available.add(height)
        except (TypeError, ValueError):
            pass
    choices: list[int] = []
    for ceiling in HEIGHT_STEPS:
        if ceiling > MAX_VIDEO_HEIGHT:
            continue
        candidates = [height for height in available if height <= ceiling]
        if candidates and max(candidates) not in choices:
            choices.append(max(candidates))
    if available:
        highest = max(available)
        if highest not in choices:
            choices.append(highest)
    return sorted(set(choices))


def estimate_video_size(info: dict[str, Any], height: int) -> int | None:
    formats = info.get("formats") or []
    candidates = []
    for fmt in formats:
        try:
            fmt_height = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if fmt_height and fmt_height <= height and fmt.get("vcodec") not in (None, "none"):
            candidates.append(fmt)
    if not candidates:
        return get_size(info)
    candidates.sort(key=lambda f: (int(f.get("height") or 0), float(f.get("tbr") or 0)), reverse=True)
    video = candidates[0]
    size = get_size(video)
    if video.get("acodec") not in (None, "none") and size:
        return size
    audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio_formats.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 0), reverse=True)
    audio = audio_formats[0] if audio_formats else None
    audio_size = get_size(audio)
    if size and audio_size:
        return size + audio_size
    try:
        duration = float(info.get("duration") or 0)
        video_rate = float(video.get("vbr") or video.get("tbr") or 0)
        audio_rate = float((audio or {}).get("abr") or 0)
        estimated = int((video_rate + audio_rate) * 1000 * duration / 8)
        return estimated if estimated > 0 else size
    except (TypeError, ValueError):
        return size


def estimate_audio_size(info: dict[str, Any]) -> int | None:
    duration = info.get("duration")
    try:
        duration_value = int(duration or 0)
    except (TypeError, ValueError):
        duration_value = 0
    if duration_value > 0:
        # MP3 at 128 kbps + metadata overhead; deliberately conservative.
        return int((128_000 / 8) * duration_value * 1.08)
    audio_formats = [f for f in info.get("formats") or [] if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio_formats.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 0), reverse=True)
    return get_size(audio_formats[0]) if audio_formats else None


def caption_for(info: dict[str, Any]) -> str:
    title = str(info.get("title") or "فيديو بدون عنوان").strip()[:360]
    uploader = str(info.get("uploader") or info.get("channel") or "غير معروف").strip()[:100]
    views = info.get("view_count")
    views_text = f"\n👁 المشاهدات: {int(views):,}" if isinstance(views, (int, float)) else ""
    return f"🎬 {title}\n👤 الناشر: {uploader}\n⏱ المدة: {duration_text(info.get('duration'))}{views_text}\n\nاختار الجودة أو الصوت من الأزرار 👇"


def build_quality_keyboard(job_id: str, info: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for height in video_height_choices(info):
        size = format_bytes(estimate_video_size(info, height))
        if len(size) > 16:
            size = "حجم تقريبي غير معروف"
        row.append(InlineKeyboardButton(f"🎬 {height}p · {size}", callback_data=f"dl|{job_id}|h{height}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(f"🎵 صوت MP3 · {format_bytes(estimate_audio_size(info))}", callback_data=f"dl|{job_id}|audio")])
    rows.append([InlineKeyboardButton("✖️ إلغاء", callback_data=f"cancel|{job_id}")])
    return InlineKeyboardMarkup(rows)


def prune_jobs() -> None:
    now = time.time()
    for key, job in list(jobs.items()):
        if now - job.get("created", now) > MAX_JOB_AGE_SECONDS and not job.get("running"):
            jobs.pop(key, None)
    if len(jobs) > MAX_JOBS:
        old = sorted(jobs.items(), key=lambda kv: kv[1].get("created", 0))
        for key, job in old[: len(jobs) - MAX_JOBS]:
            if not job.get("running"):
                jobs.pop(key, None)


def download_media(url: str, out_dir: str, choice: str, progress: dict[str, Any]) -> str:
    validate_public_url(url)
    template = str(Path(out_dir) / "AHMX_%(title).80B_%(id)s.%(ext)s")
    options = ytdlp_options()
    options.update({
        "outtmpl": template,
        "paths": {"home": out_dir, "temp": out_dir},
        "noplaylist": True,
        "overwrites": True,
        "max_filesize": MAX_UPLOAD_BYTES,
        "merge_output_format": "mp4",
    })

    def hook(data: dict[str, Any]) -> None:
        if progress.get("cancel_requested"):
            raise DownloadCancelled("أوقفت التحميل بناءً على طلبك.")
        if data.get("status") == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            try:
                downloaded = int(downloaded)
                total = int(total)
            except (TypeError, ValueError):
                downloaded, total = 0, 0
            if downloaded > MAX_UPLOAD_BYTES:
                raise DownloadTooLarge(f"الملف تجاوز حد {MAX_UPLOAD_MB} MB أثناء التحميل.")
            progress["downloaded"] = downloaded
            progress["total"] = total
            progress["speed"] = str(data.get("_speed_str") or "")[:40]
            progress["phase"] = "تنزيل الفيديو"
        elif data.get("status") == "finished":
            progress["phase"] = "تجهيز الملف النهائي"

    options["progress_hooks"] = [hook]
    if choice == "audio":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
        })
    else:
        height = int(choice)
        options["format"] = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        )

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    candidates = [p for p in Path(out_dir).rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS and not p.name.endswith(".part")]
    if not candidates:
        raise RuntimeError("التحميل انتهى لكن ماقدرتش أحدد ملف الفيديو الناتج.")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    output = candidates[0]
    final_size = output.stat().st_size
    if final_size > MAX_UPLOAD_BYTES:
        raise DownloadTooLarge(f"الحجم النهائي {format_bytes(final_size)} أكبر من حد {MAX_UPLOAD_MB} MB.")
    progress["phase"] = "التحميل اكتمل"
    progress["downloaded"] = final_size
    progress["total"] = final_size
    return str(output)


async def authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS and user.id not in ADMIN_IDS:
        await message.reply_text("🔒 البوت متاح حاليًا للمستخدمين المصرح لهم فقط.")
        return False
    return True


# ── Telegram commands and handlers ─────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "أهلًا بيك في AHM X Downloader 🔴⚫\n\n"
        "ابعت رابط فيديو عام من موقع مدعوم، وهعرض لك العنوان والغلاف والجودات المتاحة وحجم كل اختيار بشكل تقريبي.\n\n"
        f"📦 حد إرسال الملف: {MAX_UPLOAD_MB} MB\n"
        "🎬 فيديو بجودة مختلفة • 🎵 استخراج صوت MP3\n"
        "⏳ فيه متابعة للتنزيل وإمكانية الإلغاء.\n\n"
        "استخدم البوت فقط للمحتوى الذي تملكه أو لديك إذن بتنزيله. بعض المنصات قد ترفض طلبات الاستضافة أو تشترط وصولًا إضافيًا."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            "📥 طريقة الاستخدام\n"
            "1) أرسل رابط الفيديو نفسه في الخاص.\n"
            "2) راجع العنوان والغلاف والمدة.\n"
            "3) اختر الجودة أو MP3.\n"
            "4) انتظر وصول الملف؛ تقدر تلغي أثناء التنزيل.\n\n"
            "ملاحظات مهمة:\n"
            f"• الحد الحالي للملف {MAX_UPLOAD_MB} MB.\n"
            "• الأحجام المعروضة تقديرية، وقد تختلف عن الحجم النهائي.\n"
            "• لا يدعم البوت الفيديوهات الخاصة أو تجاوز تسجيل الدخول أو اختبارات الحماية.\n"
            "• لو رابط معيّن فشل، جرّب رابطًا عامًا آخر أو جودة أقل.\n\n"
            "الأوامر: /start /help /cancel /id"
        )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and update.effective_message:
        await update.effective_message.reply_text(f"Telegram ID بتاعك: `{user.id}`", parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message:
        return
    job_id = active_job_by_user.get(user.id)
    job = jobs.get(job_id or "")
    if job and job.get("progress") is not None:
        job["progress"]["cancel_requested"] = True
        await update.effective_message.reply_text("🛑 وصلتني إشارة الإلغاء. هوقف التنزيل عند أقرب نقطة آمنة.")
    else:
        await update.effective_message.reply_text("مفيش تحميل شغال عندك حاليًا.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or user.id not in ADMIN_IDS:
        if message:
            await message.reply_text("الأمر ده للمشرفين فقط.")
        return
    await message.reply_text(
        "🔴⚫ حالة AHM X Downloader\n"
        f"• المستخدمون النشطون: {len(active_users)}\n"
        f"• طلبات التحليل/الاختيار المخزنة مؤقتًا: {len(jobs)}\n"
        f"• حد الإرسال: {MAX_UPLOAD_MB} MB\n"
        f"• التنزيلات المتزامنة: {MAX_ACTIVE_DOWNLOADS}\n"
        f"• الصيانة: {'مفعّلة' if maintenance_mode else 'متوقفة'}"
    )


async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global maintenance_mode
    user = update.effective_user
    message = update.effective_message
    if not user or not message or user.id not in ADMIN_IDS:
        if message:
            await message.reply_text("الأمر ده للمشرفين فقط.")
        return
    value = context.args[0].lower() if context.args else ""
    if value not in {"on", "off"}:
        await message.reply_text("استخدم: /maintenance on أو /maintenance off")
        return
    maintenance_mode = value == "on"
    await message.reply_text("✅ وضع الصيانة اتفعّل." if maintenance_mode else "✅ وضع الصيانة اتوقف.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    if not await authorized(update, context):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("📩 استخدم البوت في المحادثة الخاصة علشان الخصوصية.")
        return
    if maintenance_mode and user.id not in ADMIN_IDS:
        await message.reply_text("🛠 البوت في وضع الصيانة مؤقتًا. جرّب بعد شوية.")
        return
    url = extract_url(message.text)
    if not url:
        await message.reply_text("ابعت رابط فيديو يبدأ بـ http:// أو https:// 🙂")
        return
    if user.id in active_users:
        await message.reply_text("عندك عملية شغالة بالفعل. استخدم /cancel أو استنى لما تخلص. ⏳")
        return
    now = time.monotonic()
    if now - last_request_at.get(user.id, 0) < REQUEST_COOLDOWN_SECONDS:
        await message.reply_text("استنى ثواني بسيطة قبل ما تبعت رابط تاني 🙂")
        return
    last_request_at[user.id] = now
    active_users.add(user.id)
    status = await message.reply_text("🔎 بفحص الرابط وبجمع معلومات الفيديو والجودات المتاحة...")
    try:
        info = await asyncio.to_thread(extract_info, url)
        heights = video_height_choices(info)
        has_audio = any(f.get("acodec") not in (None, "none") for f in (info.get("formats") or [])) or bool(info.get("url"))
        if not heights and not has_audio:
            raise ValueError("مش لاقي مسارات فيديو أو صوت قابلة للتحميل في الرابط ده.")
        prune_jobs()
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {
            "created": time.time(),
            "user_id": user.id,
            "chat_id": message.chat_id,
            "url": url,
            "title": str(info.get("title") or "فيديو بدون عنوان")[:400],
            "uploader": str(info.get("uploader") or info.get("channel") or "غير معروف")[:120],
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "heights": heights,
            "running": False,
            "progress": None,
        }
        caption = caption_for(info)
        keyboard = build_quality_keyboard(job_id, info)
        thumbnail = info.get("thumbnail")
        try:
            if thumbnail and str(thumbnail).startswith(("https://", "http://")):
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=thumbnail,
                    caption=caption,
                    reply_markup=keyboard,
                    connect_timeout=20,
                    read_timeout=30,
                    write_timeout=30,
                )
            else:
                await context.bot.send_message(chat_id=message.chat_id, text=caption, reply_markup=keyboard)
        except (TelegramError, BadRequest, ValueError):
            await context.bot.send_message(chat_id=message.chat_id, text=caption, reply_markup=keyboard)
        try:
            await status.delete()
        except TelegramError:
            pass
    except Exception as exc:
        message_text = friendly_error(url, exc)
        if "Sign in to confirm" in str(exc) or "not a bot" in str(exc).lower():
            log.warning("Metadata extraction rejected by source (%s)", urlparse(url).hostname)
        else:
            log.warning("Metadata extraction failed (%s): %s", type(exc).__name__, str(exc)[:350])
        try:
            await status.edit_text(f"❌ مقدرتش أقرأ الفيديو.\n{message_text}")
        except TelegramError:
            await message.reply_text(f"❌ مقدرتش أقرأ الفيديو.\n{message_text}")
    finally:
        active_users.discard(user.id)


async def safe_edit(message: Any, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramError:
        pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return
    try:
        await query.answer()
    except TelegramError:
        pass
    parts = query.data.split("|")
    action = parts[0]
    if len(parts) < 2:
        return
    job_id = parts[1]
    job = jobs.get(job_id)
    if not job:
        await safe_edit(query.message, "انتهت صلاحية الطلب. ابعت الرابط مرة تانية.")
        return
    if job.get("user_id") != user.id:
        try:
            if query.message:
                await query.message.reply_text("الطلب ده مش خاص بيك.")
        except TelegramError:
            pass
        return

    if action == "cancel":
        if job.get("running") and job.get("progress"):
            job["progress"]["cancel_requested"] = True
            if query.message:
                try:
                    await query.message.reply_text("🛑 وصلتني إشارة إلغاء التحميل.")
                except TelegramError:
                    pass
        else:
            jobs.pop(job_id, None)
            try:
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.reply_text("تم إلغاء طلب التحميل.")
            except TelegramError:
                pass
        return

    if action == "canceljob":
        progress = job.get("progress")
        if progress is not None:
            progress["cancel_requested"] = True
            await safe_edit(query.message, "🛑 جاري إيقاف التحميل...")
        return

    if action != "dl" or len(parts) != 3:
        return
    if job.get("running"):
        await safe_edit(query.message, "الطلب ده بدأ بالفعل. استنى شوية.")
        return
    if user.id in active_users:
        await safe_edit(query.message, "عندك عملية تانية شغالة بالفعل.")
        return
    choice_token = parts[2]
    if choice_token == "audio":
        choice = "audio"
    elif choice_token.startswith("h") and choice_token[1:].isdigit():
        choice = choice_token[1:]
        if int(choice) not in job.get("heights", []):
            await safe_edit(query.message, "الجودة دي مش موجودة ضمن اختيارات الرابط. ابعت الرابط مرة تانية.")
            return
    else:
        return

    active_users.add(user.id)
    active_job_by_user[user.id] = job_id
    job["running"] = True
    progress: dict[str, Any] = {"phase": "في انتظار دور التنزيل", "downloaded": 0, "total": 0, "speed": "", "cancel_requested": False}
    job["progress"] = progress
    status_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إلغاء التنزيل", callback_data=f"canceljob|{job_id}")]])
    try:
        status = await query.message.reply_text("⏳ جاري تجهيز التنزيل...", reply_markup=status_markup)
    except TelegramError:
        job["running"] = False
        job["progress"] = None
        active_users.discard(user.id)
        active_job_by_user.pop(user.id, None)
        log.warning("Could not create download status message")
        return
    temp_dir = tempfile.mkdtemp(prefix="ahmx_dl_")
    file_path: str | None = None
    try:
        async with download_semaphore:
            if progress.get("cancel_requested"):
                raise DownloadCancelled("أوقفت العملية بناءً على طلبك.")
            progress["phase"] = "جاري تنزيل الملف"
            download_task = asyncio.create_task(asyncio.to_thread(download_media, job["url"], temp_dir, choice, progress))
            last_update = 0.0
            while not download_task.done():
                await asyncio.sleep(2)
                now = time.monotonic()
                if now - last_update >= 4:
                    downloaded = int(progress.get("downloaded") or 0)
                    total = int(progress.get("total") or 0)
                    phase = progress.get("phase") or "جاري التنزيل"
                    pct = f" ({min(100, int(downloaded * 100 / total))}%)" if total > 0 else ""
                    speed = f"\n🚀 السرعة: {progress.get('speed')}" if progress.get("speed") else ""
                    await safe_edit(status, f"⏳ {phase}{pct}\n📦 {format_bytes(downloaded)} / {format_bytes(total)}{speed}\n\nتقدر تلغي بزر الإلغاء.", status_markup)
                    last_update = now
            file_path = await download_task
        if progress.get("cancel_requested"):
            raise DownloadCancelled("أوقفت العملية بناءً على طلبك.")
        final_size = Path(file_path).stat().st_size
        if final_size > MAX_UPLOAD_BYTES:
            raise DownloadTooLarge(f"حجم الملف {format_bytes(final_size)} أكبر من حد {MAX_UPLOAD_MB} MB.")
        await safe_edit(status, f"📤 اكتمل التحميل ({format_bytes(final_size)}). جاري الإرسال إلى تيليجرام...", None)
        title = str(job.get("title") or "AHM X Video")[:650]
        caption = f"✅ {title}\n📦 {format_bytes(final_size)}\n🔴⚫ AHM X Downloader"
        ext = Path(file_path).suffix.lower()
        with open(file_path, "rb") as media:
            if choice == "audio" or ext in {".mp3", ".m4a", ".aac", ".opus", ".ogg"}:
                await context.bot.send_audio(
                    chat_id=user.id, audio=media, filename=Path(file_path).name,
                    caption=caption[:1024], connect_timeout=30, read_timeout=180, write_timeout=180,
                )
            elif ext == ".mp4":
                await context.bot.send_video(
                    chat_id=user.id, video=media, filename=Path(file_path).name,
                    caption=caption[:1024], supports_streaming=True,
                    connect_timeout=30, read_timeout=180, write_timeout=180,
                )
            else:
                await context.bot.send_document(
                    chat_id=user.id, document=media, filename=Path(file_path).name,
                    caption=caption[:1024], connect_timeout=30, read_timeout=180, write_timeout=180,
                )
        await safe_edit(status, "✅ تم إرسال الملف بنجاح. النسخة المؤقتة هتتمسح من السيرفر.", None)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        jobs.pop(job_id, None)
    except DownloadCancelled:
        await safe_edit(status, "🛑 تم إلغاء التنزيل وحذف الملفات المؤقتة.", None)
    except DownloadTooLarge as exc:
        await safe_edit(status, f"⚠️ {exc}\nجرّب جودة أقل أو فيديو أقصر.", None)
    except Exception as exc:
        log.warning("Download/upload failed (%s): %s", type(exc).__name__, str(exc)[:350])
        friendly = friendly_error(job["url"], exc)
        await safe_edit(status, f"❌ فشل التحميل أو الإرسال.\n{friendly}", None)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        active_users.discard(user.id)
        active_job_by_user.pop(user.id, None)
        jobs.pop(job_id, None)


# ── Render Web Service / Telegram webhook ───────────────────────────────────
@asynccontextmanager
async def lifespan(web_app: FastAPI):
    global bot_app, WEBHOOK_SECRET
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (or BOT_TOKEN) environment variable")
    if not PUBLIC_URL.startswith("https://"):
        raise RuntimeError("Set RENDER_EXTERNAL_URL or WEBHOOK_URL to the public HTTPS service URL")
    if not WEBHOOK_SECRET:
        import secrets
        WEBHOOK_SECRET = secrets.token_urlsafe(32)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        raise RuntimeError("WEBHOOK_SECRET may contain only letters, digits, underscores, and hyphens")

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30,
        read_timeout=180,
        write_timeout=180,
        pool_timeout=30,
    )
    bot_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(8)
        .build()
    )
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("cancel", cmd_cancel))
    bot_app.add_handler(CommandHandler("id", cmd_id))
    bot_app.add_handler(CommandHandler("status", cmd_status))
    bot_app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(
        url=f"{PUBLIC_URL}/telegram",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )
    try:
        await bot_app.bot.set_my_commands([
            BotCommand("start", "ابدأ استخدام البوت"),
            BotCommand("help", "طريقة الاستخدام"),
            BotCommand("cancel", "إلغاء التحميل الحالي"),
            BotCommand("id", "معرفة Telegram ID"),
        ])
    except TelegramError:
        log.warning("Could not register Telegram commands")
    log.info("Bot webhook registered: %s/telegram", PUBLIC_URL)
    try:
        yield
    finally:
        if bot_app is not None:
            try:
                await bot_app.stop()
                await bot_app.shutdown()
            except Exception:
                log.exception("Error shutting down Telegram application")
        bot_app = None


api = FastAPI(title="AHM X Downloader Bot", version="3.0.0", lifespan=lifespan)


@api.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception):
    log.exception("Unhandled HTTP error path=%s type=%s", request.url.path, type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error. Check Render Logs."})


@api.get("/")
async def root() -> dict[str, str]:
    return {"service": "AHM X Downloader Bot", "status": "ok", "docs": "Telegram bot only; send /start to the bot."}


def distribution_version(name: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "missing"


def runtime_version(command: str) -> str:
    import subprocess
    try:
        proc = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=3, check=False)
        if proc.returncode == 0:
            return (proc.stdout or proc.stderr).strip()[:80]
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return "unavailable"


@api.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "ahm-x-downloader-bot",
        "version": "3.0.0",
        "yt_dlp": str(getattr(getattr(yt_dlp, "version", None), "__version__", "unknown")),
        "yt_dlp_ejs": distribution_version("yt-dlp-ejs"),
        "node": runtime_version(NODE_PATH),
        "ffmpeg": runtime_version("ffmpeg"),
        "webhook_configured": "yes" if bool(PUBLIC_URL and WEBHOOK_SECRET) else "no",
    }


@api.post("/telegram")
async def telegram_webhook(request: Request):
    if not WEBHOOK_SECRET or not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    if bot_app is None:
        raise HTTPException(status_code=503, detail="Bot is starting")
    try:
        payload = await request.json()
        update = Update.de_json(payload, bot_app.bot)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc
    await bot_app.update_queue.put(update)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    uvicorn.run("app:api", host="0.0.0.0", port=PORT, proxy_headers=True)
