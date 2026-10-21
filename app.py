from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any
from urllib.parse import parse_qsl, urlparse

import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo
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

# ---------------------------- Configuration ----------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PUBLIC_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "49")) * 1024 * 1024
MAX_DIRECT_BYTES = int(os.getenv("MAX_DIRECT_DOWNLOAD_MB", "150")) * 1024 * 1024
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
WEB_DIR = Path(__file__).parent / "web"
MAX_VIDEO_HEIGHT = int(os.getenv("MAX_VIDEO_HEIGHT", "1080"))
MAX_ACTIVE_DOWNLOADS = int(os.getenv("MAX_ACTIVE_DOWNLOADS", "2"))
JOB_TTL_SECONDS = 20 * 60
MAX_CACHED_JOBS = 120
STANDARD_HEIGHTS = [144, 240, 360, 480, 540, 720, 1080]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ahm-x-downloader")

telegram_app: Application | None = None
active_users: set[int] = set()
active_downloads = asyncio.Semaphore(max(1, MAX_ACTIVE_DOWNLOADS))
# Short-lived cache. It is deliberately not a permanent record of URLs or media.
jobs: dict[str, dict[str, Any]] = {}
mini_inspections: dict[str, dict[str, Any]] = {}
mini_jobs: dict[str, dict[str, Any]] = {}
history_by_user: dict[int, list[dict[str, Any]]] = {}
seen_users: set[int] = set()
completed_downloads = 0
maintenance_mode = False
last_inspection_at: dict[int, float] = {}


# ------------------------------ Utilities ------------------------------

class DownloadTooLarge(Exception):
    pass


def clean_url_text(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).rstrip(".,!؟)]}>")


def validate_public_url(url: str) -> None:
    """Accept only public HTTP(S) hosts; reject local/private network targets."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("أرسل رابطًا يبدأ بـ http:// أو https://")
    if parsed.username or parsed.password:
        raise ValueError("الروابط التي تحتوي بيانات دخول غير مسموح بها.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("الرابط ده مش رابط عام صالح للتحميل.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
        except socket.gaierror as exc:
            raise ValueError("مش قادر أوصل للدومين الموجود في الرابط. راجع الرابط وجرب تاني.") from exc
    if not addresses or any(
        not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        for ip in addresses
    ):
        raise ValueError("الرابط لازم يكون من موقع عام على الإنترنت.")


def ytdlp_options(*, quiet: bool = True) -> dict[str, Any]:
    return {
        "quiet": quiet,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "ignoreerrors": False,
        "restrictfilenames": True,
        "cachedir": False,
        # GitHub projects and the official yt-dlp EJS guide recommend an explicit
        # supported runtime. Docker supplies Node.js 22+ and requirements install
        # yt-dlp-ejs. This improves normal JS challenge compatibility; it does not
        # authenticate the request or bypass YouTube's sign-in / anti-bot decisions.
        "js_runtimes": {"node": {"path": os.getenv("YTDLP_NODE_PATH", shutil.which("node") or "node")}},
        "logger": None,
    }


def extract_info(url: str) -> dict[str, Any]:
    validate_public_url(url)
    options = ytdlp_options()
    options.update({"skip_download": True, "extract_flat": False})
    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(url, download=False)
    if not result or not isinstance(result, dict):
        raise ValueError("الموقع ما رجّعش معلومات فيديو صالحة.")
    # Prevent accidental playlist/group results from being displayed as one video.
    if result.get("_type") in {"playlist", "multi_video"}:
        entries = result.get("entries") or []
        result = next((entry for entry in entries if entry), None)
        if not result:
            raise ValueError("الرابط ده مفيهوش فيديو متاح.")
    return result


def friendly_extraction_error(url: str, exc: Exception) -> str:
    """Convert frequent extractor failures into safe, actionable user messages."""
    raw = str(exc or "")
    lowered = raw.lower()
    host = (urlparse(url).hostname or "").lower()
    is_youtube = host == "youtu.be" or host.endswith(("youtube.com", "youtube-nocookie.com"))

    if is_youtube and any(marker in lowered for marker in (
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "confirm you're not a bot",
        "confirm you’re not a bot",
    )):
        return (
            "YouTube رفض الطلب من عنوان الاستضافة وطلب تحققًا بشريًا. "
            "البوت لا يتجاوز تسجيل الدخول أو حماية المنصة؛ جرّب رابطًا عامًا آخر "
            "مسموحًا بتنزيله، أو استخدم خيارات YouTube الرسمية المتاحة للمحتوى."
        )

    if is_youtube and ("po token" in lowered or "proof of origin" in lowered):
        return (
            "YouTube لم يوفّر صيغًا قابلة للقراءة لهذا الطلب من الاستضافة الحالية. "
            "قد تتغير متطلبات المنصة أو تتطلب تحققًا إضافيًا؛ جرّب لاحقًا أو استخدم "
            "وسيلة التنزيل الرسمية المتاحة."
        )

    if any(marker in lowered for marker in ("private video", "video is private", "members-only", "login required", "sign in to confirm")):
        return "الفيديو يحتاج تسجيل دخول أو صلاحية غير متوفرة للبوت. استخدم رابطًا عامًا لديك إذن بتنزيله."

    if not raw or len(raw) > 500:
        return "الموقع رفض الطلب أو رابط الفيديو غير متاح حاليًا. جرّب رابطًا عامًا آخر بعد قليل."
    return raw


def parse_size(fmt: dict[str, Any]) -> int | None:
    value = fmt.get("filesize") or fmt.get("filesize_approx")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def approx_size_for_height(info: dict[str, Any], height: int) -> int | None:
    formats = info.get("formats") or []
    videos = [
        f for f in formats
        if f.get("height") and f.get("vcodec") not in (None, "none") and int(f.get("height") or 0) <= height
    ]
    if not videos:
        return None
    videos.sort(key=lambda f: (int(f.get("height") or 0), float(f.get("tbr") or 0)), reverse=True)
    video_fmt = videos[0]
    video_size = parse_size(video_fmt)
    # A muxed format already contains audio, while a video-only format needs an audio track.
    if video_fmt.get("acodec") not in (None, "none"):
        if video_size:
            return video_size
        tbr = float(video_fmt.get("tbr") or 0)
        duration = float(info.get("duration") or 0)
        return int(tbr * 1000 * duration / 8) if tbr and duration else None

    audio_formats = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    audio_formats.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 0), reverse=True)
    audio_fmt = audio_formats[0] if audio_formats else None
    audio_size = parse_size(audio_fmt) if audio_fmt else None
    if video_size and audio_size:
        return video_size + audio_size
    duration = float(info.get("duration") or 0)
    video_bitrate = float(video_fmt.get("vbr") or video_fmt.get("tbr") or 0)
    audio_bitrate = float((audio_fmt or {}).get("abr") or 0)
    if duration and (video_bitrate or audio_bitrate):
        estimated = int((video_bitrate + audio_bitrate) * 1000 * duration / 8)
        return estimated if estimated > 0 else None
    return None


def approx_audio_size(info: dict[str, Any]) -> int | None:
    formats = info.get("formats") or []
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 0), reverse=True)
    if audio:
        size = parse_size(audio[0])
        if size:
            return size
        abr = float(audio[0].get("abr") or audio[0].get("tbr") or 0)
        duration = float(info.get("duration") or 0)
        if abr and duration:
            # MP3 re-encoding may differ; this is intentionally presented as an estimate.
            return int(abr * 1000 * duration / 8)
    duration = float(info.get("duration") or 0)
    return int(192_000 * duration / 8) if duration else None


def format_bytes(size: int | None) -> str:
    if not size or size <= 0:
        return "الحجم غير متاح"
    mb = size / (1024 * 1024)
    return f"≈ {mb:.1f} MB" if mb < 1024 else f"≈ {mb / 1024:.2f} GB"


def duration_text(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        return "غير معروف"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def output_heights(info: dict[str, Any]) -> list[int]:
    formats = info.get("formats") or []
    heights = sorted({
        int(f["height"]) for f in formats
        if f.get("height") and f.get("vcodec") not in (None, "none") and 0 < int(f["height"]) <= MAX_VIDEO_HEIGHT
    })
    if not heights:
        return []
    choices: list[int] = []
    max_height = max(heights)
    for ceiling in STANDARD_HEIGHTS:
        if ceiling > MAX_VIDEO_HEIGHT:
            continue
        candidates = [h for h in heights if h <= ceiling]
        if candidates:
            actual = max(candidates)
            if actual not in choices:
                choices.append(actual)
    if max_height not in choices:
        choices.append(max_height)
    return sorted(set(choices))


def metadata_caption(job: dict[str, Any]) -> str:
    title = str(job.get("title") or "فيديو بدون عنوان").strip()
    uploader = str(job.get("uploader") or job.get("channel") or "غير معروف").strip()
    duration = duration_text(job.get("duration"))
    views = job.get("view_count")
    views_text = f"\n👁 المشاهدات: {int(views):,}" if isinstance(views, (int, float)) else ""
    # Telegram photo captions are limited; leave room for metadata.
    if len(title) > 450:
        title = title[:447] + "..."
    if len(uploader) > 100:
        uploader = uploader[:97] + "..."
    return f"🎬 {title}\n👤 الناشر: {uploader}\n⏱ المدة: {duration}{views_text}\n\nاختار الجودة اللي تناسبك من الأزرار 👇"


def prune_jobs() -> None:
    now = time.time()
    expired = [key for key, item in jobs.items() if now - item.get("created", now) > JOB_TTL_SECONDS]
    for key in expired:
        jobs.pop(key, None)
    if len(jobs) > MAX_CACHED_JOBS:
        oldest = sorted(jobs.items(), key=lambda pair: pair[1].get("created", 0))
        for key, _ in oldest[: len(jobs) - MAX_CACHED_JOBS]:
            jobs.pop(key, None)


def build_keyboard(job_id: str, info: dict[str, Any]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    heights = output_heights(info)
    for height in heights:
        size = format_bytes(approx_size_for_height(info, height))
        label = f"{height}p • {size}" if len(size) < 15 else f"{height}p • حجم غير معروف"
        row.append(InlineKeyboardButton(label, callback_data=f"dl|{job_id}|h{height}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(
            f"🎵 صوت MP3 • {format_bytes(approx_audio_size(info))}",
            callback_data=f"dl|{job_id}|audio",
        )
    ])
    buttons.append([InlineKeyboardButton("إلغاء", callback_data=f"cancel|{job_id}")])
    return InlineKeyboardMarkup(buttons)


def download_media(url: str, output_dir: str, choice: str, progress_hook=None, max_bytes: int | None = None) -> str:
    outtmpl = str(Path(output_dir) / "AHMX_%(title).80B_%(id)s.%(ext)s")
    options = ytdlp_options()
    size_limit = max_bytes or MAX_UPLOAD_BYTES
    options.update({
        "outtmpl": outtmpl,
        "paths": {"home": output_dir, "temp": output_dir},
        "noplaylist": True,
        "merge_output_format": "mp4",
        "max_filesize": size_limit,
        "overwrites": True,
    })

    def enforce_size_limit(data: dict[str, Any]) -> None:
        # Stop a download as soon as yt-dlp reports that the byte limit was crossed.
        # The final post-processed file is checked again below because containers and
        # audio conversion can change the final size.
        if data.get("status") == "downloading":
            downloaded = data.get("downloaded_bytes")
            try:
                if downloaded is not None and int(downloaded) > size_limit:
                    raise DownloadTooLarge(
                        f"حجم الملف تجاوز الحد المسموح به وهو {format_bytes(size_limit)}."
                    )
            except (TypeError, ValueError):
                pass
        if progress_hook:
            progress_hook(data)

    options["progress_hooks"] = [enforce_size_limit]
    if choice == "audio":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        height = int(choice)
        options["format"] = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]"
        )
    validate_public_url(url)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    candidates = [
        p for p in Path(output_dir).rglob("*")
        if p.is_file() and p.suffix.lower() in {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".opus", ".aac"}
    ]
    if not candidates:
        raise RuntimeError("التحميل خلص من غير ما أقدر أحدد ملف الفيديو الناتج.")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    path = candidates[0]
    size = path.stat().st_size
    if size > size_limit:
        raise DownloadTooLarge(f"حجم الملف {format_bytes(size)} أكبر من الحد المسموح به للعملية.")
    return str(path)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    text = (
        "أهلًا بيك في AHM X Downloader 🔴⚫\n\n"
        "افتح الـ Mini App علشان تستخدم واجهة التحميل الكاملة: معاينة الفيديو، الجودات والأحجام التقديرية، MP3، وسجل التحميلات.\n\n"
        f"• إرسال داخل تيليجرام: حتى {MAX_UPLOAD_BYTES // (1024 * 1024)} MB تقريبًا.\n"
        f"• تنزيل مباشر مؤقت: حتى {MAX_DIRECT_BYTES // (1024 * 1024)} MB حسب الاستضافة.\n\n"
        "نزّل فقط المحتوى المسموح لك بتنزيله. لا يتجاوز البوت حماية المحتوى أو تسجيل الدخول.\n"
        "أرسل /help للمساعدة."
    )
    markup = None
    if PUBLIC_URL.startswith("https://"):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 افتح AHM X Downloader", web_app=WebAppInfo(url=PUBLIC_URL + "/"))]])
    await message.reply_text(text, reply_markup=markup)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "طريقة الاستخدام:\n"
            "1) انسخ رابط الفيديو العام وأرسله هنا.\n"
            "2) راجع العنوان والصورة والمدة وحجم كل اختيار تقريبيًا.\n"
            "3) اضغط على الجودة أو MP3 وانتظر تجهيز الملف.\n\n"
            "ملاحظات:\n"
            "• بعض المواقع قد لا تكون مدعومة أو قد تغيّر طريقة عملها.\n"
            "• تقدير الحجم ليس ضمانًا؛ الحجم النهائي يعتمد على المصدر.\n"
            "• الملفات الأكبر من حد تيليجرام لن تُرسل، واختَر جودة أقل.\n"
            "• لا يدعم الفيديوهات الخاصة أو المحتوى المحمي أو تخطي تسجيل الدخول.\n"
            "• الملفات المؤقتة تُحذف بعد انتهاء العملية."
        )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("علشان الخصوصية، استخدم البوت في المحادثة الخاصة معاه 📩")
        return
    url = clean_url_text(message.text)
    if not url:
        await message.reply_text("ابعت رابط فيديو يبدأ بـ https:// أو http:// 🙂")
        return
    if user.id in active_users:
        await message.reply_text("عندك عملية شغّالة بالفعل؛ استنى لما تخلص وبعدين ابعت رابط تاني. ⏳")
        return

    active_users.add(user.id)
    status = await message.reply_text("🔎 بفحص الرابط وبجمع معلومات الفيديو والجودات المتاحة...")
    try:
        info = await asyncio.to_thread(extract_info, url)
        formats = info.get("formats") or []
        heights = output_heights(info)
        if not heights and not any(f.get("acodec") not in (None, "none") for f in formats):
            raise ValueError("مش لاقي مسارات فيديو أو صوت قابلة للتحميل في الرابط ده.")

        prune_jobs()
        job_id = uuid.uuid4().hex[:8]
        job = {
            "created": time.time(),
            "user_id": user.id,
            "url": url,
            "title": info.get("title") or "فيديو بدون عنوان",
            "uploader": info.get("uploader"),
            "channel": info.get("channel"),
            "duration": info.get("duration"),
            "view_count": info.get("view_count"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or url,
        }
        jobs[job_id] = job
        caption = metadata_caption(job)
        keyboard = build_keyboard(job_id, info)
        thumbnail_url = job.get("thumbnail")
        sent_preview = False
        if thumbnail_url and str(thumbnail_url).startswith(("https://", "http://")):
            try:
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=thumbnail_url,
                    caption=caption,
                    reply_markup=keyboard,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=20,
                )
                sent_preview = True
            except (TelegramError, BadRequest, ValueError):
                logger.info("Thumbnail URL could not be sent for job %s", job_id)
        if not sent_preview:
            await message.reply_text(caption, reply_markup=keyboard)
        await status.delete()
    except Exception as exc:
        logger.warning("Metadata extraction failed (%s): %s", type(exc).__name__, str(exc)[:500])
        user_message = friendly_extraction_error(url, exc)
        try:
            await status.edit_text(f"❌ مقدرتش أقرأ الفيديو.\n{user_message}\n\nتأكد إن الرابط عام وإن الموقع مدعوم.")
        except TelegramError:
            await message.reply_text(f"❌ مقدرتش أقرأ الفيديو.\n{user_message}")
    finally:
        active_users.discard(user.id)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return
    parts = query.data.split("|")
    if len(parts) < 2:
        await query.answer()
        return
    action, job_id = parts[0], parts[1]
    job = jobs.get(job_id)
    if action == "cancel":
        if not job or job.get("user_id") != user.id:
            await query.answer("الاختيار انتهت صلاحيته.", show_alert=True)
            return
        await query.answer()
        jobs.pop(job_id, None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        if query.message:
            await query.message.reply_text("تم إلغاء الاختيار ✅")
        return

    if action != "dl" or len(parts) != 3:
        await query.answer()
        return
    if not job or job.get("user_id") != user.id:
        await query.answer("الرابط انتهت صلاحيته؛ ابعته من جديد.", show_alert=True)
        return
    choice = parts[2]
    if choice != "audio":
        match = re.fullmatch(r"h(\d{2,4})", choice)
        if not match:
            await query.answer("اختيار جودة غير صالح.", show_alert=True)
            return
        height = int(match.group(1))
        if height <= 0 or height > MAX_VIDEO_HEIGHT:
            await query.answer("الجودة المطلوبة خارج الحدود المتاحة.", show_alert=True)
            return
        choice = str(height)

    if user.id in active_users:
        await query.answer("فيه عملية تانية شغالة عندك بالفعل.", show_alert=True)
        return
    await query.answer()
    active_users.add(user.id)

    if query.message:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
    status = await context.bot.send_message(
        chat_id=user.id,
        text="⏬ جاري تجهيز التحميل...\nهتظهر نسبة التقدم هنا لما الموقع يوفّر معلومات التحميل.",
    )
    temp_dir = tempfile.mkdtemp(prefix="ahmx_dl_")
    loop = asyncio.get_running_loop()
    progress_queue: asyncio.Queue[int] = asyncio.Queue()
    last_progress = {"value": -1}

    def hook(data: dict[str, Any]) -> None:
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            got = data.get("downloaded_bytes")
            if total and got:
                pct = max(0, min(100, int(int(got) * 100 / int(total))))
                if pct >= last_progress["value"] + 5 or pct == 100:
                    last_progress["value"] = pct
                    try:
                        loop.call_soon_threadsafe(progress_queue.put_nowait, pct)
                    except RuntimeError:
                        pass

    try:
        async with active_downloads:
            download_task = asyncio.create_task(asyncio.to_thread(download_media, job["url"], temp_dir, choice, hook))
            while not download_task.done():
                try:
                    pct = await asyncio.wait_for(progress_queue.get(), timeout=4.0)
                    try:
                        await status.edit_text(f"⏬ جاري تحميل الفيديو... {pct}%")
                    except TelegramError:
                        pass
                except asyncio.TimeoutError:
                    # This also signals progress during slow remote extraction.
                    pass
            file_path = await download_task

        file_size = os.path.getsize(file_path)
        if file_size > MAX_UPLOAD_BYTES:
            raise DownloadTooLarge(f"الملف النهائي حجمه {format_bytes(file_size)}، وده أكبر من الحد المتاح حاليًا.")
        title = str(job.get("title") or "AHM X Video")
        caption = f"✅ {title[:700]}\n📦 الحجم: {format_bytes(file_size)}\n🔴⚫ AHM X Downloader"
        try:
            await status.edit_text("📤 التحميل اكتمل؛ جاري إرسال الملف إلى تيليجرام...")
        except TelegramError:
            pass

        suffix = Path(file_path).suffix.lower()
        with open(file_path, "rb") as media_file:
            if choice == "audio" or suffix in {".mp3", ".m4a", ".aac", ".opus"}:
                await context.bot.send_audio(
                    chat_id=user.id,
                    audio=media_file,
                    filename=Path(file_path).name,
                    caption=caption[:1024],
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
            elif suffix == ".mp4":
                await context.bot.send_video(
                    chat_id=user.id,
                    video=media_file,
                    filename=Path(file_path).name,
                    caption=caption[:1024],
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
            else:
                await context.bot.send_document(
                    chat_id=user.id,
                    document=media_file,
                    filename=Path(file_path).name,
                    caption=caption[:1024],
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
        try:
            await status.edit_text("✅ تم إرسال الملف بنجاح. حذفت النسخة المؤقتة من السيرفر.")
        except TelegramError:
            pass
        jobs.pop(job_id, None)
    except DownloadTooLarge as exc:
        try:
            await status.edit_text(f"⚠️ {exc}\n\nارجع لاختيار الجودة وجرّب دقة أقل؛ الأحجام الظاهرة تقديرية.")
        except TelegramError:
            pass
    except Exception as exc:
        logger.exception("Download/upload failed for job %s", job_id)
        details = str(exc)
        if "max-filesize" in details.lower() or "larger than" in details.lower() or "file is larger" in details.lower():
            details = "الحجم أكبر من الحد الأقصى للملف المسموح به. اختَر جودة أقل."
        elif len(details) > 300:
            details = "الموقع رفض التحميل أو الملف غير متاح بهذه الجودة. جرّب جودة أخرى أو رابطًا عامًا آخر."
        try:
            await status.edit_text(f"❌ فشل التحميل أو الإرسال.\n{details}")
        except TelegramError:
            pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        active_users.discard(user.id)


# ----------------------------- Webhook API -----------------------------

@asynccontextmanager
async def lifespan(api: FastAPI):
    global telegram_app, WEBHOOK_SECRET
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    if not PUBLIC_URL.startswith("https://"):
        raise RuntimeError("Set RENDER_EXTERNAL_URL or WEBHOOK_URL to the public HTTPS service URL")
    if not WEBHOOK_SECRET:
        WEBHOOK_SECRET = secrets.token_urlsafe(32).replace("-", "A").replace("_", "B")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        raise RuntimeError("WEBHOOK_SECRET may contain only letters, digits, underscores, and hyphens")

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=180,
        write_timeout=180,
        pool_timeout=30,
    )
    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(4)
        .build()
    )
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("app", cmd_start))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
    telegram_app.add_handler(CallbackQueryHandler(callback_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=f"{PUBLIC_URL}/telegram",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )
    try:
        await telegram_app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="AHM X", web_app=WebAppInfo(url=PUBLIC_URL + "/")))
    except TelegramError:
        logger.info("Could not set Telegram menu button; inline Mini App button remains available.")
    try:
        await telegram_app.bot.set_my_commands([
            BotCommand("start", "فتح AHM X Downloader"),
            BotCommand("app", "فتح واجهة التحميل"),
            BotCommand("help", "طريقة الاستخدام"),
        ])
    except Exception:
        logger.info("Could not set bot commands.")
    logger.info("Telegram webhook registered on %s/telegram", PUBLIC_URL)
    try:
        yield
    finally:
        try:
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception:
            logger.exception("Error shutting down Telegram application")
        telegram_app = None


api = FastAPI(title="AHM X Downloader V2", version="2.0.2", lifespan=lifespan)


@api.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Keep stack traces in Render logs, but never expose internals or secrets to users.
    logger.exception("Unhandled server exception path=%s type=%s", request.url.path, type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"detail": "حصل خطأ داخلي في الخدمة. سجّل وقت الخطأ وراجع Render Logs للتشخيص."},
    )


api.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@api.get("/")
async def root():
    index_path = WEB_DIR / "index.html"
    if not index_path.is_file():
        logger.error("Mini App entry file is missing: %s; WEB_DIR=%s", index_path, WEB_DIR)
        return HTMLResponse(
            status_code=503,
            content=(
                "<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>AHM X — إعداد غير مكتمل</title>"
                "<body style='background:#0c090b;color:#fff;font:16px sans-serif;padding:28px'>"
                "<h2 style='color:#ff515d'>AHM X Downloader</h2>"
                "<p>ملفات واجهة Mini App غير موجودة على الخادم.</p>"
                "<p>تأكد أن مجلد <code>web</code> بملفاته الثلاثة (index.html وstyle.css وapp.js) موجود في مستودع GitHub، ثم أعد النشر.</p>"
                "</body></html>"
            ),
        )
    return FileResponse(index_path)


def installed_version(distribution: str) -> str:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return "missing"


def node_runtime_status() -> dict[str, str]:
    node_path = os.getenv("YTDLP_NODE_PATH") or shutil.which("node")
    if not node_path:
        return {"node": "missing", "node_path": ""}
    try:
        result = subprocess.run([node_path, "--version"], capture_output=True, text=True, timeout=3, check=False)
        node_version = (result.stdout or result.stderr).strip()[:40] if result.returncode == 0 else "unavailable"
    except (OSError, subprocess.SubprocessError):
        node_version = "unavailable"
    return {"node": node_version, "node_path": node_path}


@api.get("/health")
async def health() -> dict[str, str]:
    runtime = node_runtime_status()
    ejs_version = installed_version("yt-dlp-ejs")
    ytdlp_version = str(getattr(getattr(yt_dlp, "version", None), "__version__", "unknown"))
    # Keep health HTTP 200 so Render can report service availability; runtime fields
    # make it obvious in the response if YouTube's normal JS prerequisites are absent.
    return {
        "status": "ok",
        "service": "ahm-x-downloader-v2",
        "version": "2.0.2",
        "yt_dlp": ytdlp_version,
        "yt_dlp_ejs": ejs_version,
        **runtime,
    }


@api.post("/telegram")
async def telegram_webhook(request: Request):
    if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Bot is starting")
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid update payload") from exc
    await telegram_app.update_queue.put(update)
    return JSONResponse({"ok": True})



# --------------------------- Mini App API ---------------------------

def validate_init_data(init_data: str) -> dict[str, Any]:
    """Validate Telegram Mini App initData using the official Bot-token HMAC scheme."""
    if not BOT_TOKEN or not init_data or len(init_data) > 12_000:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        # Duplicate keys make signature interpretation ambiguous; reject them outright.
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("Duplicate initData keys")
        values = dict(pairs)
        received_hash = values.pop("hash", "")
        if not received_hash or len(received_hash) != 64:
            raise ValueError("Missing hash")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            raise ValueError("Bad signature")
        auth_date = int(values.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 24 * 60 * 60:
            raise ValueError("Expired initData")
        user_json = values.get("user")
        if not user_json:
            raise ValueError("Missing user")
        user = json.loads(user_json)
        if not isinstance(user, dict) or not user.get("id"):
            raise ValueError("Bad user")
        return user
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("Rejected invalid Telegram initData: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="Unauthorized") from exc


def payload_user(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("initData")
    if not isinstance(raw, str):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return validate_init_data(raw)


def prune_mini_cache() -> None:
    now = time.time()
    expired_inspections = [key for key, item in mini_inspections.items() if now - item.get("created", now) > JOB_TTL_SECONDS]
    for key in expired_inspections:
        mini_inspections.pop(key, None)
    for key, item in list(mini_jobs.items()):
        if item.get("status") in {"queued", "downloading", "sending"}:
            continue
        expired = now > item.get("expires_at", item.get("created", now) + JOB_TTL_SECONDS)
        if expired:
            temp_dir = item.pop("temp_dir", None)
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            item.pop("file_path", None)
            item.pop("download_token", None)
            item.pop("download_url", None)
            if item.get("status") == "completed" and item.get("delivery") == "browser":
                item["message"] = "انتهت صلاحية رابط التنزيل المؤقت."
        if now - item.get("created", now) > JOB_TTL_SECONDS * 3:
            mini_jobs.pop(key, None)


def choice_rows(info: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    heights = output_heights(info)
    for height in heights:
        size = approx_size_for_height(info, height)
        rows.append({
            "id": f"h{height}",
            "height": height,
            "label": f"{height}p",
            "size_bytes": size,
            "size_label": format_bytes(size),
            "recommended": height == max(heights) if heights else False,
        })
    formats = info.get("formats") or []
    if any(f.get("acodec") not in (None, "none") for f in formats):
        audio_size = approx_audio_size(info)
        rows.append({
            "id": "audio",
            "height": 0,
            "label": "MP3",
            "size_bytes": audio_size,
            "size_label": format_bytes(audio_size),
            "recommended": False,
        })
    return rows


def add_history(user_id: int, item: dict[str, Any]) -> None:
    entries = history_by_user.setdefault(user_id, [])
    entries.insert(0, item)
    del entries[40:]


def update_history(user_id: int, job_id: str, **changes: Any) -> None:
    for item in history_by_user.get(user_id, []):
        if item.get("job_id") == job_id:
            item.update(changes)
            break


async def mini_api_error(request: Request) -> dict[str, Any]:
    # Kept for explicit API consumers; normal handlers perform authentication individually.
    return {}


@api.get("/api/public-config")
async def public_config() -> dict[str, Any]:
    return {
        "app_name": "AHM X Downloader",
        "bot_username": BOT_USERNAME,
        "telegram_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "direct_download_mb": MAX_DIRECT_BYTES // (1024 * 1024),
    }


@api.post("/api/session")
async def mini_session(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user = payload_user(payload)
    user_id = int(user["id"])
    seen_users.add(user_id)
    return {
        "user": {"id": user_id, "first_name": str(user.get("first_name") or "Creator"), "username": user.get("username")},
        "is_admin": user_id in ADMIN_IDS,
        "maintenance": maintenance_mode,
        "limits": {"telegram_mb": MAX_UPLOAD_BYTES // (1024 * 1024), "direct_mb": MAX_DIRECT_BYTES // (1024 * 1024)},
    }


@api.post("/api/inspect")
async def mini_inspect(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user = payload_user(payload)
    user_id = int(user["id"])
    seen_users.add(user_id)
    if maintenance_mode and user_id not in ADMIN_IDS:
        raise HTTPException(status_code=503, detail="maintenance")
    now = time.time()
    if now - last_inspection_at.get(user_id, 0) < 1.5:
        raise HTTPException(status_code=429, detail="Please wait a moment before analyzing another link.")
    last_inspection_at[user_id] = now
    url = payload.get("url")
    if not isinstance(url, str) or len(url.strip()) > 2500:
        raise HTTPException(status_code=400, detail="أرسل رابط فيديو صالحًا.")
    url = url.strip()
    try:
        validate_public_url(url)
        info = await asyncio.to_thread(extract_info, url)
        choices = choice_rows(info)
        if not choices:
            raise ValueError("لم يتم العثور على صيغ فيديو أو صوت قابلة للتنزيل لهذا الرابط.")
        prune_mini_cache()
        inspect_id = uuid.uuid4().hex[:16]
        mini_inspections[inspect_id] = {"created": time.time(), "user_id": user_id, "url": url, "info": info, "choices": choices}
        duration = info.get("duration")
        return {
            "inspect_id": inspect_id,
            "title": str(info.get("title") or "فيديو بدون عنوان")[:350],
            "uploader": str(info.get("uploader") or info.get("channel") or "غير معروف")[:150],
            "duration": duration,
            "view_count": info.get("view_count"),
            "thumbnail": info.get("thumbnail") if str(info.get("thumbnail") or "").startswith(("https://", "http://")) else None,
            "webpage_url": info.get("webpage_url") or url,
            "platform": str(info.get("extractor_key") or info.get("extractor") or "VIDEO")[:60],
            "choices": choices,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Mini App inspect failed for user=%s (%s): %s", user_id, type(exc).__name__, str(exc)[:500])
        text = friendly_extraction_error(url, exc)
        raise HTTPException(status_code=400, detail=text or "تعذر تحليل الرابط.") from exc


async def perform_mini_download(job_id: str) -> None:
    global completed_downloads
    job = mini_jobs.get(job_id)
    if not job:
        return
    user_id = int(job["user_id"])
    temp_dir = tempfile.mkdtemp(prefix="ahmx_mini_")
    job["temp_dir"] = temp_dir
    loop = asyncio.get_running_loop()
    last_progress = {"value": -1}

    def progress_hook(data: dict[str, Any]) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        got = data.get("downloaded_bytes")
        if not total or got is None:
            return
        pct = max(0, min(100, int(int(got) * 100 / int(total))))
        if pct >= last_progress["value"] + 2 or pct == 100:
            last_progress["value"] = pct
            def set_progress() -> None:
                current = mini_jobs.get(job_id)
                if current:
                    current["progress"] = pct
                    current["message"] = f"جاري تنزيل الملف… {pct}%"
            try:
                loop.call_soon_threadsafe(set_progress)
            except RuntimeError:
                pass

    try:
        job["status"] = "downloading"
        job["message"] = "جاري تنزيل الملف…"
        max_bytes = MAX_UPLOAD_BYTES if job["delivery"] == "telegram" else MAX_DIRECT_BYTES
        async with active_downloads:
            file_path = await asyncio.to_thread(download_media, job["url"], temp_dir, job["choice"], progress_hook, max_bytes)
        size = os.path.getsize(file_path)
        job["progress"] = 100
        job["file_size"] = size
        job["filename"] = Path(file_path).name
        if job["delivery"] == "telegram":
            if size > MAX_UPLOAD_BYTES:
                raise DownloadTooLarge(f"حجم الملف {format_bytes(size)} أكبر من حد الإرسال إلى تيليجرام.")
            if telegram_app is None:
                raise RuntimeError("البوت غير متصل حاليًا؛ جرّب مرة أخرى لاحقًا.")
            job["status"] = "sending"
            job["message"] = "الملف جاهز، جاري إرساله إلى تيليجرام…"
            caption = f"✅ {str(job.get('title') or 'AHM X Video')[:650]}\\n📦 الحجم: {format_bytes(size)}\\n🔴⚫ AHM X Downloader"
            suffix = Path(file_path).suffix.lower()
            with open(file_path, "rb") as media_file:
                if job["choice"] == "audio" or suffix in {".mp3", ".m4a", ".aac", ".opus"}:
                    await telegram_app.bot.send_audio(chat_id=user_id, audio=media_file, filename=Path(file_path).name, caption=caption[:1024], read_timeout=180, write_timeout=180, connect_timeout=30)
                elif suffix == ".mp4":
                    await telegram_app.bot.send_video(chat_id=user_id, video=media_file, filename=Path(file_path).name, caption=caption[:1024], supports_streaming=True, read_timeout=180, write_timeout=180, connect_timeout=30)
                else:
                    await telegram_app.bot.send_document(chat_id=user_id, document=media_file, filename=Path(file_path).name, caption=caption[:1024], read_timeout=180, write_timeout=180, connect_timeout=30)
            job["status"] = "completed"
            job["message"] = "تم إرسال الملف بنجاح إلى تيليجرام."
            job["download_url"] = None
            job["expires_at"] = time.time() + JOB_TTL_SECONDS
            shutil.rmtree(temp_dir, ignore_errors=True)
            job.pop("temp_dir", None)
        else:
            token = secrets.token_urlsafe(28)
            job["download_token"] = token
            job["file_path"] = file_path
            job["download_url"] = f"{PUBLIC_URL}/api/file/{job_id}?token={token}"
            job["status"] = "completed"
            job["message"] = "الملف جاهز للتنزيل على جهازك."
            job["expires_at"] = time.time() + JOB_TTL_SECONDS
            # File remains temporary only for the short-lived signed download URL.
        completed_downloads += 1
        update_history(user_id, job_id, status="completed", size_bytes=size, quality_label=job.get("quality_label"))
    except Exception as exc:
        logger.info("Mini download failed job=%s type=%s", job_id, type(exc).__name__)
        message = str(exc)
        if "max-filesize" in message.lower() or "larger than" in message.lower() or isinstance(exc, DownloadTooLarge):
            message = "حجم الملف أكبر من الحد المسموح به. جرّب دقة أقل أو اختر طريقة تنزيل مناسبة."
        elif len(message) > 260:
            message = "الموقع رفض التحميل أو الجودة غير متاحة حاليًا. جرّب اختيارًا آخر."
        job["status"] = "failed"
        job["error"] = message
        job["message"] = "فشل التحميل."
        job["expires_at"] = time.time() + JOB_TTL_SECONDS
        shutil.rmtree(temp_dir, ignore_errors=True)
        job.pop("temp_dir", None)
        update_history(user_id, job_id, status="failed", error=message)
    finally:
        active_users.discard(user_id)


@api.post("/api/download")
async def mini_download(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user = payload_user(payload)
    user_id = int(user["id"])
    seen_users.add(user_id)
    if maintenance_mode and user_id not in ADMIN_IDS:
        raise HTTPException(status_code=503, detail="maintenance")
    inspect_id = payload.get("inspect_id")
    inspected = mini_inspections.get(str(inspect_id))
    if not inspected or inspected.get("user_id") != user_id or time.time() - inspected.get("created", 0) > JOB_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="انتهت صلاحية المعاينة. حلّل الرابط مرة أخرى.")
    quality = str(payload.get("quality") or "")
    choice_row = next((row for row in inspected["choices"] if row["id"] == quality), None)
    if not choice_row:
        raise HTTPException(status_code=400, detail="اختيار الجودة غير صالح.")
    delivery = payload.get("delivery")
    if delivery not in {"telegram", "browser"}:
        raise HTTPException(status_code=400, detail="طريقة الاستلام غير صالحة.")
    if delivery == "telegram" and choice_row.get("size_bytes") and choice_row["size_bytes"] > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="الحجم المتوقع أكبر من حد تيليجرام؛ اختر التنزيل على الجهاز أو جودة أقل.")
    if user_id in active_users:
        raise HTTPException(status_code=409, detail="عندك عملية تحميل شغّالة بالفعل.")
    choice = "audio" if quality == "audio" else str(int(quality[1:])) if re.fullmatch(r"h\d{2,4}", quality) else ""
    if not choice:
        raise HTTPException(status_code=400, detail="اختيار الجودة غير صالح.")
    job_id = uuid.uuid4().hex[:14]
    label = choice_row.get("label") or quality
    job = {
        "job_id": job_id,
        "created": time.time(),
        "user_id": user_id,
        "inspect_id": str(inspect_id),
        "url": inspected["url"],
        "title": str(inspected["info"].get("title") or "AHM X Video")[:350],
        "thumbnail": inspected["info"].get("thumbnail"),
        "choice": choice,
        "quality": quality,
        "quality_label": "MP3" if quality == "audio" else label,
        "delivery": delivery,
        "status": "queued",
        "progress": 0,
        "message": "تم وضع طلبك في قائمة التنفيذ…",
    }
    mini_jobs[job_id] = job
    active_users.add(user_id)
    add_history(user_id, {"job_id": job_id, "created": job["created"], "title": job["title"], "quality": quality, "quality_label": job["quality_label"], "delivery": delivery, "status": "queued"})
    asyncio.create_task(perform_mini_download(job_id))
    return {"job_id": job_id, "status": "queued"}


@api.post("/api/job")
async def mini_job_status(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user = payload_user(payload)
    job_id = str(payload.get("job_id") or "")
    job = mini_jobs.get(job_id)
    if not job or job.get("user_id") != int(user["id"]):
        raise HTTPException(status_code=404, detail="العملية غير موجودة أو انتهت صلاحيتها.")
    return {"job_id": job_id, "status": job.get("status"), "progress": job.get("progress", 0), "message": job.get("message", ""), "error": job.get("error"), "delivery": job.get("delivery"), "download_url": job.get("download_url"), "file_size": job.get("file_size")}


def cleanup_browser_download(job_id: str) -> None:
    job = mini_jobs.get(job_id)
    if not job:
        return
    temp_dir = job.pop("temp_dir", None)
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
    job.pop("file_path", None)
    job.pop("download_token", None)
    job.pop("download_url", None)
    job["expires_at"] = time.time()


@api.get("/api/file/{job_id}")
async def mini_file(job_id: str, token: str):
    job = mini_jobs.get(job_id)
    if not job or job.get("status") != "completed" or job.get("delivery") != "browser":
        raise HTTPException(status_code=404, detail="رابط التنزيل غير متاح.")
    expected = str(job.get("download_token") or "")
    if not expected or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="رابط التنزيل غير صالح.")
    if time.time() > job.get("expires_at", 0):
        cleanup_browser_download(job_id)
        raise HTTPException(status_code=410, detail="انتهت صلاحية رابط التنزيل.")
    path = job.get("file_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="الملف المؤقت لم يعد موجودًا.")
    filename = str(job.get("filename") or Path(path).name)
    suffix = Path(filename).suffix.lower()
    media_type = "audio/mpeg" if suffix == ".mp3" else "video/mp4" if suffix == ".mp4" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename, background=BackgroundTask(cleanup_browser_download, job_id))


@api.post("/api/history")
async def mini_history(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user = payload_user(payload)
    items = history_by_user.get(int(user["id"]), [])[:30]
    return {"items": [{k: v for k, v in item.items() if k not in {"url", "inspect_id", "user_id"}} for item in items]}


@api.post("/api/admin/overview")
async def mini_admin_overview(request: Request) -> dict[str, Any]:
    payload = await request.json()
    user = payload_user(payload)
    if int(user["id"]) not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="غير مسموح لك بفتح لوحة المشرف.")
    running = sum(1 for item in mini_jobs.values() if item.get("status") in {"queued", "downloading", "sending"})
    recent = sorted(mini_jobs.values(), key=lambda item: item.get("created", 0), reverse=True)[:12]
    return {
        "users": len(seen_users),
        "completed": completed_downloads,
        "running": running,
        "maintenance": maintenance_mode,
        "recent": [{"title": item.get("title"), "quality": item.get("quality"), "quality_label": item.get("quality_label"), "status": item.get("status"), "created": item.get("created")} for item in recent],
    }


@api.post("/api/admin/maintenance")
async def mini_admin_maintenance(request: Request) -> dict[str, Any]:
    global maintenance_mode
    payload = await request.json()
    user = payload_user(payload)
    if int(user["id"]) not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="غير مسموح لك بتغيير إعدادات الإدارة.")
    maintenance_mode = bool(payload.get("enabled"))
    return {"maintenance": maintenance_mode}

if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level=os.getenv("LOG_LEVEL", "info").lower())
