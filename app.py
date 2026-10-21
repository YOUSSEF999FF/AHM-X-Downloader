from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
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


def download_media(url: str, output_dir: str, choice: str, progress_hook=None) -> str:
    outtmpl = str(Path(output_dir) / "AHMX_%(title).80B_%(id)s.%(ext)s")
    options = ytdlp_options()
    options.update({
        "outtmpl": outtmpl,
        "paths": {"home": output_dir, "temp": output_dir},
        "noplaylist": True,
        "merge_output_format": "mp4",
        "max_filesize": MAX_UPLOAD_BYTES,
        "overwrites": True,
    })
    if progress_hook:
        options["progress_hooks"] = [progress_hook]
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
    if size > MAX_UPLOAD_BYTES:
        raise DownloadTooLarge(f"حجم الملف {format_bytes(size)} أكبر من الحد المسموح به للبوت.")
    return str(path)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "أهلًا بيك في AHM X Video Downloader 🔴⚫\n\n"
        "📥 ابعت رابط فيديو عام من موقع مدعوم، وهعرض لك العنوان والصورة المصغّرة والمدة والأحجام التقريبية، وبعدها تختار الجودة.\n\n"
        "• فيديو: حتى 1080p حسب الجودات المتاحة.\n"
        "• صوت: MP3.\n"
        f"• الحد الأقصى للملف المرسل: {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.\n\n"
        "استخدمه للمحتوى اللي تملكه أو عندك إذن بتنزيله، والبوت لا يتجاوز الحماية أو تسجيل الدخول.\n"
        "أرسل /help للمساعدة."
    )


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
        logger.warning("Metadata extraction failed: %s", exc)
        user_message = str(exc)
        if len(user_message) > 650:
            user_message = "الموقع رفض الطلب أو حصل خطأ أثناء قراءة الرابط. جرّب رابطًا عامًا آخر بعد شوية."
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


api = FastAPI(title="AHM X Video Downloader Bot", version="1.0.0", lifespan=lifespan)


@api.get("/")
async def root() -> dict[str, str]:
    return {"service": "AHM X Video Downloader Bot", "status": "running"}


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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


if __name__ == "__main__":
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level=os.getenv("LOG_LEVEL", "info").lower())
