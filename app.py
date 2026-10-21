from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
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

import store
from config import (
    ADMIN_IDS, BOT_TOKEN, DATA_DIR, DEFAULT_SETTINGS, DOWNLOAD_DIR, OWNER_ID, PORT, PUBLIC_URL,
    SETTING_SPECS, WEBHOOK_SECRET as ENV_WEBHOOK_SECRET,
)
from diagnostics import capture_exception, capture_message, public_error
from downloader.common import DownloadCancelled, format_bytes
from downloader.registry import detect_platform, serializable_info
from security import WebAppAuthError, validate_init_data
from service import (
    active_progress,
    analyses,
    analyze_url,
    build_summary,
    file_paths,
    get_live_job,
    limiter,
    last_analysis_at,
    request_cancel,
    start_download,
    set_error_notifier,
    cleanup_loop,
)

LOG = logging.getLogger("ahmx.v4")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

bot_app: Application | None = None
WEBHOOK_SECRET = ENV_WEBHOOK_SECRET
last_maintenance_alert: dict[int, float] = {}


def _clean_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "on", "yes", "1"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "off", "no", "0"}:
        return False
    raise ValueError("القيمة لازم تكون true أو false.")


def validate_setting_patch(patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("ابعت إعدادات صالحة لتعديلها.")
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        spec = SETTING_SPECS.get(key)
        if not spec:
            raise ValueError(f"الإعداد {key} غير قابل للتعديل من اللوحة.")
        kind = spec["type"]
        if kind == "bool":
            cleaned[key] = _clean_bool(value)
        elif kind == "int":
            try:
                num = int(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"قيمة {spec['label']} لازم تكون رقمًا صحيحًا.") from exc
            if num < int(spec["min"]) or num > int(spec["max"]):
                raise ValueError(f"قيمة {spec['label']} لازم تكون بين {spec['min']} و{spec['max']}.")
            cleaned[key] = num
        elif kind == "str":
            valid = list(spec.get("choices") or [])
            if value not in valid:
                raise ValueError(f"القيمة المتاحة لـ {spec['label']}: {', '.join(valid)}")
            cleaned[key] = str(value)
    return cleaned


def is_maintaining() -> bool:
    return bool(store.get_settings().get("maintenance_mode", False))


def webapp_button(label: str = "🚀 فتح Mini App") -> InlineKeyboardMarkup | None:
    if PUBLIC_URL.startswith("https://"):
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, web_app=WebAppInfo(url=f"{PUBLIC_URL}/app"))]])
    return None


def user_can_download(user_id: int, chat_type: str | None = None) -> bool:
    settings = store.get_settings()
    if settings.get("allow_private_chats_only", True) and chat_type and chat_type != "private":
        return False
    if is_maintaining() and not store.is_admin(user_id):
        return False
    if not settings.get("downloads_enabled", True) and not store.is_admin(user_id):
        return False
    return True


async def notify_owner(text: str, *, user_id: int = 0, force: bool = False) -> None:
    settings = store.get_settings()
    if not settings.get("owner_alerts_enabled", True) or bot_app is None or bot_app.bot is None:
        return
    targets = {OWNER_ID} if OWNER_ID > 0 else set()
    if not targets:
        targets = set(ADMIN_IDS)
    for target in targets:
        try:
            await bot_app.bot.send_message(chat_id=target, text=text[:3800], disable_web_page_preview=True)
        except TelegramError:
            LOG.debug("Could not notify owner id=%s", target)


async def maintenance_notice(user_id: int, where: str) -> None:
    now = time.monotonic()
    # Notify/log at most once per user per minute to prevent spam during repeated polling.
    if now - last_maintenance_alert.get(user_id, 0) >= 60:
        last_maintenance_alert[user_id] = now
        store.add_event(user_id, "maintenance_interaction", where)
        await notify_owner(f"🛠 تفاعل أثناء الصيانة\nالمستخدم: {user_id}\nالمكان: {where}", user_id=user_id)


async def apply_settings_patch(patch: dict[str, Any], actor_id: int) -> dict[str, Any]:
    """Persist admin changes; maintenance mode also stops jobs for regular users."""
    before = store.get_settings()
    after = store.set_settings(patch)
    await limiter.notify_limit_change()
    if patch.get("maintenance_mode") is True and not before.get("maintenance_mode", False):
        cancelled = 0
        for job_id, job in list(active_progress.items()):
            job_user = int(job.get("user_id") or 0)
            if job_user and not store.is_admin(job_user) and job.get("status") in {"queued", "running"}:
                if request_cancel(job_id, job_user):
                    cancelled += 1
        store.add_event(actor_id, "maintenance_enabled", f"cancel_requested={cancelled}")
        await notify_owner(f"🛠 تم تفعيل وضع الصيانة. أُرسلت طلبات إلغاء لـ {cancelled} عملية غير إدارية.")
    elif patch.get("maintenance_mode") is False and before.get("maintenance_mode", False):
        store.add_event(actor_id, "maintenance_disabled", "")
    return after


def register_user(update: Update) -> int:
    user = update.effective_user
    if not user:
        return 0
    store.touch_user(user.id, user.username or "", user.first_name or "")
    return user.id


async def reject_blocked_user(update: Update, user_id: int) -> bool:
    if user_id and store.is_blocked(user_id) and not store.is_admin(user_id):
        message = update.effective_message
        if message:
            await message.reply_text("⛔ حسابك موقوف من استخدام البوت. تواصل مع مالك البوت للمراجعة.")
        return True
    return False


def format_settings(settings: dict[str, Any]) -> str:
    def flag(key: str) -> str:
        return "✅" if settings.get(key) else "❌"
    return (
        "🔴⚫ **AHM X · لوحة المالك**\n\n"
        f"الصيانة: {flag('maintenance_mode')}\n"
        f"التنزيلات: {flag('downloads_enabled')}\n"
        f"YouTube: {flag('youtube_enabled')} · Facebook: {flag('facebook_enabled')} · Instagram: {flag('instagram_enabled')}\n"
        f"المنصات الأخرى: {flag('generic_enabled')} · الصوت: {flag('audio_enabled')}\n\n"
        f"التنزيلات المتزامنة: `{settings.get('max_concurrent')}`\n"
        f"الحد اليومي/مستخدم: `{settings.get('daily_limit')}`\n"
        f"أقصى حجم: `{settings.get('max_upload_mb')} MB`\n"
        f"أقصى جودة: `{settings.get('max_video_height')}p`\n"
        f"المهلة: `{settings.get('cooldown_seconds')}s`"
    )


def admin_keyboard(settings: dict[str, Any]) -> InlineKeyboardMarkup:
    items = [
        ("maintenance_mode", "🛠 الصيانة"),
        ("downloads_enabled", "📥 التنزيلات"),
        ("youtube_enabled", "▶️ YouTube"),
        ("facebook_enabled", "📘 Facebook"),
        ("instagram_enabled", "📸 Instagram"),
        ("generic_enabled", "🌐 منصات أخرى"),
        ("audio_enabled", "🎵 الصوت"),
        ("owner_alerts_enabled", "🔔 تنبيهات المالك"),
    ]
    rows = []
    for key, label in items:
        rows.append([InlineKeyboardButton(f"{'✅' if settings.get(key) else '❌'} {label}", callback_data=f"admin_toggle|{key}")])
    rows.append([InlineKeyboardButton("📊 الإحصاءات", callback_data="admin_stats"), InlineKeyboardButton("🧾 الأخطاء", callback_data="admin_errors")])
    if PUBLIC_URL.startswith("https://"):
        rows.append([InlineKeyboardButton("🖥 فتح لوحة Mini App", web_app=WebAppInfo(url=f"{PUBLIC_URL}/app"))])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg or not user_id:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/start")
        await msg.reply_text("🛠 **AHM X Downloader تحت الصيانة**\nالبوت مش هيعالج روابط حاليًا. جرب تاني بعد انتهاء الصيانة.", parse_mode="Markdown")
        return
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        await msg.reply_text("📩 افتح محادثة البوت الخاصة واضغط /start علشان تفتح Mini App بأمان.")
        return
    settings = store.get_settings()
    usage = store.get_usage(user_id)
    text = (
        "أهلًا بيك في **AHM X Downloader V4** 🔴⚫\n\n"
        "📥 ابعت رابط فيديو عام من منصة مدعومة أو افتح Mini App للتحكم في الجودة وسجل التنزيلات.\n"
        "🎬 اختيار الجودة وحجم تقريبي · 🎵 MP3 · 📈 متابعة التقدم\n"
        f"📦 أقصى حجم للإرسال: {settings.get('max_upload_mb')} MB\n"
        f"📊 المتبقي اليوم: {usage['remaining_today']} من {usage['daily_limit']} تنزيلات\n\n"
        "استخدم البوت للمحتوى الذي تملكه أو لديك إذن بتنزيله. بعض المنصات قد ترفض طلبات الاستضافة أو تحتاج وصولًا غير متاح."
    )
    markup = webapp_button()
    if store.is_admin(user_id):
        if markup:
            rows = markup.inline_keyboard + [[InlineKeyboardButton("🛠 لوحة الإدارة", callback_data="admin_panel")]]
            markup = InlineKeyboardMarkup(rows)
        else:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛠 لوحة الإدارة", callback_data="admin_panel")]])
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=markup, disable_web_page_preview=True)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/help")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    await msg.reply_text(
        "📥 **طريقة الاستخدام**\n1. ابعت رابط فيديو عام في الخاص.\n2. اختار الجودة أو MP3.\n3. تابع التقدم أو ألغِ العملية.\n\n"
        "**الأوامر**\n/start — البداية\n/help — المساعدة\n/id — رقم حسابك\n/cancel — إلغاء التنزيل\n/history — آخر التنزيلات\n/panel — لوحة الإدارة (للمصرح لهم)\n"
        "الأحجام تقديرية وبعض الروابط قد لا تعمل بسبب قيود المنصة أو تغييراتها.",
        parse_mode="Markdown",
        reply_markup=webapp_button() if not update.effective_chat or update.effective_chat.type == ChatType.PRIVATE else None,
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    if update.effective_message:
        if await reject_blocked_user(update, user_id):
            return
        if is_maintaining() and not store.is_admin(user_id):
            await maintenance_notice(user_id, "/id")
            await update.effective_message.reply_text("🛠 البوت تحت الصيانة حاليًا. جرّب بعد انتهائها.")
            return
        await update.effective_message.reply_text(f"🆔 Telegram ID: `{user_id}`", parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/cancel")
    cancelled = False
    for job_id, job in list(active_progress.items()):
        if int(job.get("user_id") or 0) == user_id and job.get("status") in {"queued", "running"}:
            cancelled = request_cancel(job_id, user_id) or cancelled
    await msg.reply_text("🛑 وصلت إشارة الإلغاء. هيتوقف التنزيل عند أقرب نقطة ممكنة." if cancelled else "مفيش تنزيل شغال عندك حاليًا.")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/history")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. جرّب تاني بعد انتهائها.")
        return
    rows = store.get_history(user_id, 8)
    if not rows:
        await msg.reply_text("لسه مفيش تنزيلات في السجل.", reply_markup=webapp_button() if not update.effective_chat or update.effective_chat.type == ChatType.PRIVATE else None)
        return
    lines = ["🧾 آخر التنزيلات"]
    for row in rows:
        mark = {"completed": "✅", "failed": "❌", "queued": "⏳", "running": "⬇️", "cancelled": "🚫"}.get(row["status"], "•")
        # Titles come from external websites; keep them plain text to avoid Markdown parse errors.
        lines.append(f"{mark} {str(row['title'])[:65]} — الحالة: {row['status']}")
    await msg.reply_text("\n".join(lines), reply_markup=webapp_button() if not update.effective_chat or update.effective_chat.type == ChatType.PRIVATE else None)


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/panel")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    if not store.is_admin(user_id):
        await msg.reply_text("⛔ لوحة الإدارة للمالك والمشرفين المعتمدين فقط.")
        return
    await msg.reply_text(format_settings(store.get_settings()), parse_mode="Markdown", reply_markup=admin_keyboard(store.get_settings()))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/status")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    if not store.is_admin(user_id):
        await msg.reply_text("الأمر ده للإدارة فقط.")
        return
    stats = store.get_stats()
    await msg.reply_text(
        "📊 **إحصاءات البوت**\n"
        f"المستخدمون: {stats['users']}\nطلبات اليوم: {stats['today_jobs']}\n"
        f"اكتملت: {stats['completed']} · فشلت: {stats['failed']} · نشطة: {stats['active']}\n"
        f"أخطاء محفوظة: {stats['errors']}\nعمليات متزامنة الآن: {limiter.active}",
        parse_mode="Markdown",
    )


async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/maintenance")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    if not store.is_admin(user_id):
        await msg.reply_text("الأمر ده للإدارة فقط.")
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await msg.reply_text("استخدم: /maintenance on أو /maintenance off")
        return
    value = context.args[0].lower() == "on"
    await apply_settings_patch({"maintenance_mode": value}, user_id)
    await msg.reply_text("🛠 تم تفعيل وضع الصيانة." if value else "✅ تم إيقاف وضع الصيانة.")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/set")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    if not store.is_admin(user_id):
        await msg.reply_text("الأمر ده للإدارة فقط.")
        return
    if len(context.args) < 2:
        keys = ", ".join(SETTING_SPECS)
        await msg.reply_text(f"استخدم /set key value\nالمفاتيح: {keys}\nمثال: /set daily_limit 12")
        return
    key, raw_value = context.args[0], " ".join(context.args[1:])
    try:
        patch = validate_setting_patch({key: raw_value})
        await apply_settings_patch(patch, user_id)
        await msg.reply_text(f"✅ تم تعديل {SETTING_SPECS[key]['label']} إلى `{patch[key]}`", parse_mode="Markdown")
    except Exception as exc:
        await msg.reply_text(f"❌ {exc}")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/admin")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    if not store.is_owner(user_id):
        await msg.reply_text("إدارة صلاحيات المشرفين للمالك فقط.")
        return
    if len(context.args) != 2 or context.args[0].lower() not in {"add", "remove"} or not context.args[1].isdigit():
        await msg.reply_text("استخدم: /admin add USER_ID أو /admin remove USER_ID\nاستخدم /id لمعرفة رقم المستخدم.")
        return
    target = int(context.args[1])
    if context.args[0].lower() == "add":
        store.add_admin(target, user_id)
        await msg.reply_text(f"✅ تمت إضافة {target} كمشرف.")
    else:
        try:
            store.remove_admin(target)
            await msg.reply_text(f"✅ تمت إزالة صلاحيات {target}.")
        except ValueError as exc:
            await msg.reply_text(str(exc))


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "/errors")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    if not store.is_admin(user_id):
        await msg.reply_text("سجل الأخطاء للمالك والمشرفين فقط.")
        return
    errors = store.get_errors(5)
    if not errors:
        await msg.reply_text("✅ مفيش أخطاء مسجلة.")
        return
    lines = ["🧩 آخر الأعطال"]
    for err in errors:
        # Exception text may include underscores/brackets supplied by an upstream site.
        lines.append(f"{err['error_id']} · {err['source']} · {err['error_type']}\n{err['message'][:150]}")
    await msg.reply_text("\n\n".join(lines))


async def analyse_for_user(url: str, user_id: int) -> tuple[str, dict[str, Any]]:
    settings = store.get_settings()
    if not store.is_admin(user_id):
        now = time.monotonic()
        cooldown = int(settings.get("cooldown_seconds", 4))
        previous = last_analysis_at.get(user_id, 0.0)
        if now - previous < cooldown:
            wait_for = max(1, int(cooldown - (now - previous) + 0.99))
            raise ValueError(f"استنى {wait_for} ثانية قبل تحليل رابط جديد.")
        last_analysis_at[user_id] = now
    analysis_id, info = await asyncio.to_thread(analyze_url, url, settings)
    analyses[analysis_id]["user_id"] = user_id
    return analysis_id, info


def info_caption(info: dict[str, Any]) -> str:
    title = str(info.get("title") or "فيديو بدون عنوان")[:300]
    uploader = str(info.get("uploader") or info.get("channel") or "غير معروف")[:100]
    return f"🎬 {title}\n👤 {uploader}\n⏱ {info.get('duration_string') or 'المدة غير متاحة'}\n\nاختار الجودة المناسبة:"


def telegram_quality_keyboard(analysis_id: str, summary: dict[str, Any]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row = []
    for fmt in summary.get("formats", []):
        label = f"🎵 MP3 · {fmt['size_text']}" if fmt["choice"] == "audio" else f"🎬 {fmt['label']} · {fmt['size_text']}"
        row.append(InlineKeyboardButton(label[:60], callback_data=f"dl|{analysis_id}|{fmt['choice']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ إلغاء", callback_data=f"analysis_cancel|{analysis_id}")])
    return InlineKeyboardMarkup(rows)


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "unknown_command")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. حاول لاحقًا.")
        return
    await msg.reply_text("الأمر ده مش معروف. استخدم /help لعرض الأوامر المتاحة.")


async def handle_other_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = register_user(update)
    msg = update.effective_message
    if not msg:
        return
    if await reject_blocked_user(update, user_id):
        return
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "non_text_message")
        await msg.reply_text("🛠 البوت تحت الصيانة حاليًا. مش هقدر أعالج الرسائل لحد ما الصيانة تنتهي.")
        return
    if store.get_settings().get("allow_private_chats_only", True) and update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        return
    await msg.reply_text("ابعت رابط فيديو عام كنص، أو افتح Mini App من /start لاختيار الجودة ومتابعة السجل.", reply_markup=webapp_button())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return
    user_id = register_user(update)
    if await reject_blocked_user(update, user_id):
        return
    settings = store.get_settings()
    if settings.get("maintenance_mode") and not store.is_admin(user_id):
        await maintenance_notice(user_id, "message")
        await message.reply_text("🛠 البوت في وضع الصيانة حاليًا. مش هقدر أعالج الرسالة أو الرابط لحد ما الصيانة تنتهي.")
        return
    if settings.get("allow_private_chats_only", True) and update.effective_chat and update.effective_chat.type != ChatType.PRIVATE:
        return
    url_match = re.search(r"https?://[^\s<>]+", message.text, flags=re.IGNORECASE)
    if not url_match:
        await message.reply_text("ابعت رابط فيديو عام، أو افتح Mini App لاختيار الجودة ومتابعة السجل.", reply_markup=webapp_button())
        return
    url = url_match.group(0).rstrip(".,!؟)]}>")
    if not user_can_download(user_id, update.effective_chat.type if update.effective_chat else None):
        await message.reply_text("التنزيلات متوقفة مؤقتًا.")
        return
    status = await message.reply_text("🔎 بفحص الرابط وبجمع معلومات الفيديو...")
    platform = detect_platform(url)
    try:
        analysis_id, info = await analyse_for_user(url, user_id)
        summary = build_summary(info, store.get_settings(), analysis_id)
        if not summary.get("formats"):
            raise ValueError("لم يتم العثور على صيغة فيديو أو صوت قابلة للتنزيل.")
        caption = info_caption(info)
        markup = telegram_quality_keyboard(analysis_id, summary)
        thumbnail = info.get("thumbnail")
        if thumbnail and str(thumbnail).startswith("https://"):
            try:
                await context.bot.send_photo(chat_id=message.chat_id, photo=thumbnail, caption=caption[:1024], reply_markup=markup)
            except TelegramError:
                await context.bot.send_message(chat_id=message.chat_id, text=caption, reply_markup=markup)
        else:
            await context.bot.send_message(chat_id=message.chat_id, text=caption, reply_markup=markup)
        await status.delete()
    except Exception as exc:
        error_id = capture_exception(exc, source="telegram_analyze", user_id=user_id, platform=platform, url=url)
        friendly = public_error(error_id, exc)
        LOG.exception("Telegram analysis failed id=%s platform=%s", error_id, platform)
        await status.edit_text(f"❌ مقدرتش أقرأ الفيديو.\n{friendly['message']}\n\nرقم التشخيص: `{error_id}`", parse_mode="Markdown")
        await notify_owner(f"⚠️ فشل تحليل رابط\nالخطأ: {error_id}\nالمنصة: {platform}\nالسبب: {type(exc).__name__}: {str(exc)[:350]}", user_id=user_id)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return
    register_user(update)
    if store.is_blocked(user.id) and not store.is_admin(user.id):
        try:
            await query.answer("حسابك موقوف من الإدارة.", show_alert=True)
        except TelegramError:
            pass
        return
    if is_maintaining() and not store.is_admin(user.id):
        await maintenance_notice(user.id, "callback")
        try:
            await query.answer("البوت تحت الصيانة حاليًا.", show_alert=True)
        except TelegramError:
            pass
        return
    try:
        await query.answer()
    except TelegramError:
        pass
    parts = query.data.split("|")
    action = parts[0]
    if action == "admin_panel":
        if not store.is_admin(user.id):
            await query.message.reply_text("⛔ لوحة الإدارة غير متاحة لحسابك.")
            return
        await query.message.reply_text(format_settings(store.get_settings()), parse_mode="Markdown", reply_markup=admin_keyboard(store.get_settings()))
        return
    if action == "admin_toggle" and len(parts) == 2:
        if not store.is_admin(user.id):
            return
        key = parts[1]
        if key not in SETTING_SPECS or SETTING_SPECS[key]["type"] != "bool":
            return
        settings = store.get_settings()
        patch = {key: not bool(settings.get(key))}
        await apply_settings_patch(patch, user.id)
        try:
            await query.message.edit_text(format_settings(store.get_settings()), parse_mode="Markdown", reply_markup=admin_keyboard(store.get_settings()))
        except TelegramError:
            await query.message.reply_text(format_settings(store.get_settings()), parse_mode="Markdown", reply_markup=admin_keyboard(store.get_settings()))
        return
    if action == "admin_stats":
        if not store.is_admin(user.id):
            return
        stats = store.get_stats()
        await query.message.reply_text(f"📊 مستخدمون: {stats['users']}\nطلبات اليوم: {stats['today_jobs']}\nنجاح: {stats['completed']}\nفشل: {stats['failed']}\nنشط: {stats['active']}\nأخطاء: {stats['errors']}")
        return
    if action == "admin_errors":
        if not store.is_admin(user.id):
            return
        errors = store.get_errors(5)
        if not errors:
            await query.message.reply_text("✅ لا توجد أخطاء محفوظة.")
        else:
            await query.message.reply_text("\n\n".join(f"{e['error_id']} · {e['source']} · {e['error_type']}\n{e['message'][:220]}" for e in errors))
        return
    if len(parts) < 2:
        return
    analysis_id = parts[1]
    analysis = analyses.get(analysis_id)
    if action == "analysis_cancel":
        if analysis and analysis.get("user_id") == user.id:
            analyses.pop(analysis_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("تم إلغاء اختيار الجودة.")
        return
    if action != "dl" or len(parts) != 3:
        return
    if not analysis or analysis.get("user_id") != user.id:
        await query.message.reply_text("الطلب انتهت صلاحيته أو مش تابع لحسابك. ابعت الرابط مرة تانية.")
        return
    settings = store.get_settings()
    if not user_can_download(user.id, update.effective_chat.type if update.effective_chat else None):
        await query.message.reply_text("التنزيلات متوقفة مؤقتًا.")
        return
    choice = parts[2]
    job_id = uuid.uuid4().hex[:14]
    try:
        await start_download(job_id, analysis_id, user.id, choice, settings)
    except Exception as exc:
        await query.message.reply_text(f"❌ {exc}")
        return
    progress_message = await query.message.reply_text("⏳ تم وضع التنزيل في قائمة الانتظار...")
    while True:
        await asyncio.sleep(1.5)
        state = get_live_job(job_id, user.id)
        if not state:
            await progress_message.edit_text("❌ انتهى سجل العملية قبل اكتمالها.")
            return
        if state["status"] in {"completed", "failed", "cancelled"}:
            break
        pct = int(state.get("progress") or 0)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        try:
            await progress_message.edit_text(f"⏳ {state['phase']}\n[{bar}] {pct}%\n📦 {format_bytes(state['downloaded_bytes'])} / {format_bytes(state['total_bytes'])}")
        except TelegramError:
            pass
    if state["status"] != "completed":
        if state["status"] == "cancelled":
            await progress_message.edit_text("🛑 تم إلغاء التنزيل.")
        else:
            await progress_message.edit_text(f"❌ فشل التنزيل.\n{state.get('error_message') or state.get('phase')}\nرقم التشخيص: {state.get('error_id') or 'غير متاح'}")
        return
    path = file_paths.get(job_id)
    if not path or not Path(path).is_file():
        await progress_message.edit_text("❌ الملف المؤقت غير متاح. افتح Mini App وحاول مرة أخرى.")
        return
    file_path = Path(path)
    caption = f"✅ {state['title'][:500]}\n📦 {format_bytes(file_path.stat().st_size)}\n🔴⚫ AHM X Downloader"
    try:
        with file_path.open("rb") as media:
            if file_path.suffix.lower() == ".mp4":
                await context.bot.send_video(chat_id=user.id, video=media, filename=file_path.name, caption=caption[:1024], supports_streaming=True, read_timeout=180, write_timeout=180)
            elif file_path.suffix.lower() in {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}:
                await context.bot.send_audio(chat_id=user.id, audio=media, filename=file_path.name, caption=caption[:1024], read_timeout=180, write_timeout=180)
            else:
                await context.bot.send_document(chat_id=user.id, document=media, filename=file_path.name, caption=caption[:1024], read_timeout=180, write_timeout=180)
        await progress_message.edit_text("✅ تم إرسال الملف بنجاح. اتأكد تحفظه على جهازك.")
    except TelegramError as exc:
        error_id = capture_exception(exc, source="telegram_upload", user_id=user.id, platform=state.get("platform", ""))
        await progress_message.edit_text(f"⚠️ التنزيل اكتمل لكن إرسال الملف لتيليجرام فشل. رقم التشخيص: {error_id}")
    finally:
        try:
            file_path.unlink(missing_ok=True)
            shutil.rmtree(file_path.parent, ignore_errors=True)
        except OSError:
            pass
        file_paths.pop(job_id, None)


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error or RuntimeError("Unknown Telegram application error")
    user_id = 0
    platform = ""
    url = None
    if isinstance(update, Update) and update.effective_user:
        user_id = update.effective_user.id
    error_id = capture_exception(exc, source="telegram_update", user_id=user_id, platform=platform, url=url)
    LOG.exception("Unhandled Telegram update error id=%s", error_id, exc_info=exc)
    await notify_owner(f"🚨 خطأ في معالجة Telegram\nرقم التشخيص: {error_id}\nالنوع: {type(exc).__name__}\n{str(exc)[:500]}", user_id=user_id)


# ── WebApp auth and API ─────────────────────────────────────────────────────
def get_web_user(init_data: str) -> dict[str, Any]:
    try:
        user = validate_init_data(init_data)
    except WebAppAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user_id = int(user["id"])
    store.touch_user(user_id, str(user.get("username") or ""), str(user.get("first_name") or ""))
    if store.is_blocked(user_id) and not store.is_admin(user_id):
        raise HTTPException(status_code=403, detail="تم إيقاف حسابك من الإدارة. تواصل مع مالك البوت إذا كنت تعتقد أن هذا خطأ.")
    return user


async def auth_from_header(x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data")) -> dict[str, Any]:
    return get_web_user(x_telegram_init_data or "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app, WEBHOOK_SECRET
    store.init_db()
    interrupted = store.recover_interrupted_jobs()
    if interrupted:
        LOG.warning("Marked %s queued/running jobs as interrupted after process restart", interrupted)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    if OWNER_ID < 1:
        raise RuntimeError("Set OWNER_ID to your numeric Telegram user ID")
    if not PUBLIC_URL.startswith("https://"):
        raise RuntimeError("Set RENDER_EXTERNAL_URL or WEBAPP_URL to the public HTTPS URL")
    if not WEBHOOK_SECRET:
        WEBHOOK_SECRET = secrets.token_urlsafe(32)
        LOG.warning("WEBHOOK_SECRET was not configured; generated for this process. Set a stable value in Render Environment Variables.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        raise RuntimeError("WEBHOOK_SECRET can contain only letters, digits, underscores and hyphens")
    request = HTTPXRequest(connection_pool_size=24, connect_timeout=25, read_timeout=180, write_timeout=180, pool_timeout=30)
    bot_app = Application.builder().token(BOT_TOKEN).request(request).concurrent_updates(8).build()
    set_error_notifier(notify_owner)
    cleaner_task = asyncio.create_task(cleanup_loop(), name="ahmx-cleanup")
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("id", cmd_id))
    bot_app.add_handler(CommandHandler("cancel", cmd_cancel))
    bot_app.add_handler(CommandHandler("history", cmd_history))
    bot_app.add_handler(CommandHandler("panel", cmd_panel))
    bot_app.add_handler(CommandHandler("status", cmd_status))
    bot_app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    bot_app.add_handler(CommandHandler("set", cmd_set))
    bot_app.add_handler(CommandHandler("admin", cmd_admin))
    bot_app.add_handler(CommandHandler("errors", cmd_errors))
    bot_app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    bot_app.add_handler(CallbackQueryHandler(callback_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    bot_app.add_handler(MessageHandler(filters.ALL, handle_other_message))
    bot_app.add_error_handler(telegram_error_handler)
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(url=f"{PUBLIC_URL}/telegram", secret_token=WEBHOOK_SECRET, allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    try:
        await bot_app.bot.set_my_commands([
            BotCommand("start", "ابدأ استخدام البوت"), BotCommand("help", "شرح الاستخدام"),
            BotCommand("history", "سجل التنزيلات"), BotCommand("cancel", "إلغاء التنزيل"),
            BotCommand("id", "معرفة Telegram ID"), BotCommand("panel", "لوحة الإدارة"),
        ])
    except TelegramError:
        LOG.exception("Could not register bot commands")
    if PUBLIC_URL.startswith("https://"):
        try:
            await bot_app.bot.set_chat_menu_button(menu_button=None)
        except TelegramError:
            pass
    LOG.info("AHM X Downloader V4 is running at %s", PUBLIC_URL)
    try:
        yield
    finally:
        cleaner_task.cancel()
        try:
            await cleaner_task
        except asyncio.CancelledError:
            pass
        if bot_app:
            try:
                await bot_app.bot.delete_webhook(drop_pending_updates=False)
                await bot_app.stop()
                await bot_app.shutdown()
            except Exception:
                LOG.exception("Error while shutting down Telegram bot")
        bot_app = None


api = FastAPI(title="AHM X Downloader V4", version="4.0.0", lifespan=lifespan)
web_dir = Path(__file__).parent / "web"
api.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@api.exception_handler(Exception)
async def unhandled_http_error(request: Request, exc: Exception):
    error_id = capture_exception(exc, source=f"http:{request.method}:{request.url.path}")
    LOG.exception("Unhandled HTTP error id=%s path=%s", error_id, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Internal Server Error (diagnostic: {error_id})", "error_id": error_id})


@api.get("/")
async def root():
    return RedirectResponse(url="/app")


@api.get("/app")
async def mini_app():
    return FileResponse(str(web_dir / "index.html"))


@api.get("/health")
async def health():
    import subprocess
    def ver(cmd: str) -> str:
        try:
            out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=3, check=False)
            return (out.stdout or out.stderr).strip().splitlines()[0][:100] if out.returncode == 0 else "unavailable"
        except Exception:
            return "unavailable"
    try:
        from importlib.metadata import version, PackageNotFoundError
        try: ejs = version("yt-dlp-ejs")
        except PackageNotFoundError: ejs = "missing"
    except Exception:
        ejs = "unknown"
    checks = {"database": "ok", "temp_storage": "ok"}
    try:
        store.get_stats()
    except Exception:
        checks["database"] = "error"
    try:
        from config import DOWNLOAD_DIR
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        probe = DOWNLOAD_DIR / f".health-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception:
        checks["temp_storage"] = "error"
    settings = store.get_settings()
    degraded = any(value != "ok" for value in checks.values()) or ejs == "missing"
    return {
        "status": "degraded" if degraded else "ok", "service": "ahm-x-downloader-v4", "version": "4.0.0",
        "checks": checks,
        "yt_dlp": getattr(getattr(yt_dlp, "version", None), "__version__", "unknown"),
        "yt_dlp_ejs": ejs, "node": ver(os.getenv("YTDLP_NODE_PATH", "/usr/local/bin/node")),
        "ffmpeg": ver("ffmpeg"), "cobalt_fallback": "configured" if os.getenv("COBALT_API_URL") else "off",
        "active_jobs": sum(1 for item in active_progress.values() if item.get("status") in {"queued", "running"}),
        "max_concurrent": settings.get("max_concurrent"), "mini_app": "ok",
    }


@api.post("/telegram")
async def telegram_webhook(request: Request):
    if not WEBHOOK_SECRET or not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    if bot_app is None:
        raise HTTPException(status_code=503, detail="Bot is starting")
    try:
        payload = await request.json()
        update = Update.de_json(payload, bot_app.bot)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc
    await bot_app.update_queue.put(update)
    return {"ok": True}


@api.get("/api/bootstrap")
async def api_bootstrap(user: dict[str, Any] = Depends(auth_from_header)):
    user_id = int(user["id"])
    settings = store.get_settings()
    if settings.get("maintenance_mode") and not store.is_admin(user_id):
        await maintenance_notice(user_id, "miniapp_open")
    usage = store.get_usage(user_id)
    return {
        "user": {"id": user_id, "name": str(user.get("first_name") or "User"), "username": str(user.get("username") or "")},
        "is_admin": store.is_admin(user_id), "is_owner": store.is_owner(user_id),
        "maintenance": bool(settings.get("maintenance_mode")), "downloads_enabled": bool(settings.get("downloads_enabled")),
        "usage": usage,
        "settings": {key: settings.get(key, default) for key, default in DEFAULT_SETTINGS.items() if key not in {"owner_alerts_enabled", "allow_private_chats_only"}},
        "history": store.get_history(user_id, 10),
    }


@api.post("/api/analyze")
async def api_analyze(request: Request, user: dict[str, Any] = Depends(auth_from_header)):
    user_id = int(user["id"])
    settings = store.get_settings()
    if settings.get("maintenance_mode") and not store.is_admin(user_id):
        await maintenance_notice(user_id, "miniapp_analyze")
        raise HTTPException(status_code=503, detail="البوت في وضع الصيانة حاليًا.")
    if not settings.get("downloads_enabled") and not store.is_admin(user_id):
        raise HTTPException(status_code=503, detail="التنزيلات متوقفة مؤقتًا.")
    try:
        data = await request.json()
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError("الصق رابط فيديو أولًا.")
        platform = detect_platform(url)
        analysis_id, info = await analyse_for_user(url, user_id)
        summary = build_summary(info, store.get_settings(), analysis_id)
        if not summary.get("formats"):
            raise ValueError("لم يتم العثور على صيغ فيديو أو صوت مناسبة.")
        summary["platform"] = platform
        return {"ok": True, "video": summary}
    except HTTPException:
        raise
    except Exception as exc:
        if isinstance(exc, ValueError) and "استنى" in str(exc):
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        error_id = capture_exception(exc, source="miniapp_analyze", user_id=user_id, platform=locals().get("platform", ""), url=locals().get("url"))
        LOG.exception("Mini App analyze failed error_id=%s", error_id)
        err = public_error(error_id, exc)
        raise HTTPException(status_code=422, detail={"message": err["message"], "error_id": error_id}) from exc


@api.post("/api/download")
async def api_download(request: Request, user: dict[str, Any] = Depends(auth_from_header)):
    user_id = int(user["id"])
    settings = store.get_settings()
    if settings.get("maintenance_mode") and not store.is_admin(user_id):
        await maintenance_notice(user_id, "miniapp_download")
        raise HTTPException(status_code=503, detail="البوت في وضع الصيانة حاليًا.")
    if not settings.get("downloads_enabled") and not store.is_admin(user_id):
        raise HTTPException(status_code=503, detail="التنزيلات متوقفة مؤقتًا.")
    try:
        body = await request.json()
        analysis_id = str(body.get("analysis_id") or "")
        choice = str(body.get("choice") or "")
        job_id = uuid.uuid4().hex[:14]
        result = await start_download(job_id, analysis_id, user_id, choice, settings)
        await limiter.notify_limit_change()
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        if isinstance(exc, ValueError) and "الحد اليومي" in str(exc):
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.get("/api/jobs/{job_id}")
async def api_job(job_id: str, user: dict[str, Any] = Depends(auth_from_header)):
    job = get_live_job(job_id, int(user["id"]))
    if not job:
        raise HTTPException(status_code=404, detail="العملية غير موجودة أو لا تخص حسابك.")
    return job


@api.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str, user: dict[str, Any] = Depends(auth_from_header)):
    if not request_cancel(job_id, int(user["id"])):
        raise HTTPException(status_code=404, detail="لا توجد عملية قابلة للإلغاء بهذا الرقم.")
    return {"ok": True}


@api.get("/api/jobs/{job_id}/file")
async def api_job_file(job_id: str, user: dict[str, Any] = Depends(auth_from_header)):
    job = store.get_job(job_id)
    path = file_paths.get(job_id)
    if not job or int(job["user_id"]) != int(user["id"]):
        raise HTTPException(status_code=404, detail="الملف غير موجود أو لا يخص حسابك.")
    if job["status"] != "completed" or not path or not Path(path).is_file():
        raise HTTPException(status_code=409, detail="الملف لم يجهز بعد أو انتهت صلاحيته.")
    return FileResponse(path, filename=Path(path).name, media_type="application/octet-stream")


@api.get("/api/history")
async def api_history(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    user: dict[str, Any] = Depends(auth_from_header),
):
    user_id = int(user["id"])
    if is_maintaining() and not store.is_admin(user_id):
        await maintenance_notice(user_id, "miniapp_history")
    items = store.get_history(user_id, limit, offset)
    return {"items": items, "usage": store.get_usage(user_id), "has_more": len(items) == limit, "next_offset": offset + len(items)}


@api.get("/api/admin/settings")
async def api_admin_settings(user: dict[str, Any] = Depends(auth_from_header)):
    if not store.is_admin(int(user["id"])):
        raise HTTPException(status_code=403, detail="لوحة الإدارة للمصرح لهم فقط.")
    return {"settings": store.get_settings(), "specs": SETTING_SPECS, "stats": store.get_stats(), "admins": store.list_admins() if store.is_owner(int(user["id"])) else []}


@api.patch("/api/admin/settings")
async def api_admin_update_settings(request: Request, user: dict[str, Any] = Depends(auth_from_header)):
    user_id = int(user["id"])
    if not store.is_admin(user_id):
        raise HTTPException(status_code=403, detail="تعديل الإعدادات للمصرح لهم فقط.")
    try:
        body = await request.json()
        patch = validate_setting_patch(body)
        settings = await apply_settings_patch(patch, user_id)
        store.add_event(user_id, "settings_changed", ",".join(patch.keys()))
        return {"ok": True, "settings": settings}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.get("/api/admin/errors")
async def api_admin_errors(user: dict[str, Any] = Depends(auth_from_header)):
    if not store.is_admin(int(user["id"])):
        raise HTTPException(status_code=403, detail="سجل الأخطاء للمصرح لهم فقط.")
    return {"items": store.get_errors(50)}


@api.get("/api/admin/users")
async def api_admin_list_users(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    user: dict[str, Any] = Depends(auth_from_header),
):
    if not store.is_admin(int(user["id"])):
        raise HTTPException(status_code=403, detail="إدارة المستخدمين للمصرح لهم فقط.")
    items = store.list_users(limit, offset)
    for item in items:
        item["is_admin"] = store.is_admin(int(item["user_id"]))
        item["is_owner"] = store.is_owner(int(item["user_id"]))
    return {"items": items, "has_more": len(items) == limit, "next_offset": offset + len(items)}


@api.patch("/api/admin/users/{target_id}")
async def api_admin_update_user(target_id: int, request: Request, user: dict[str, Any] = Depends(auth_from_header)):
    actor_id = int(user["id"])
    if not store.is_admin(actor_id):
        raise HTTPException(status_code=403, detail="تعديل المستخدمين للمصرح لهم فقط.")
    if target_id <= 0:
        raise HTTPException(status_code=422, detail="رقم المستخدم غير صالح.")
    if store.is_owner(target_id) or (store.is_admin(target_id) and target_id != actor_id):
        raise HTTPException(status_code=403, detail="لا يمكن تعديل مالك البوت أو حساب مشرف آخر من هنا.")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON غير صالح.") from exc
    if not isinstance(body, dict) or not body or not set(body).issubset({"blocked", "daily_limit_override"}):
        raise HTTPException(status_code=422, detail="الحقول المسموحة: blocked أو daily_limit_override.")
    if target_id == actor_id and body.get("blocked") is True:
        raise HTTPException(status_code=422, detail="لا يمكنك إيقاف حسابك الإداري بنفسك.")
    blocked = body.get("blocked")
    if "blocked" in body and not isinstance(blocked, bool):
        raise HTTPException(status_code=422, detail="blocked لازم تكون true أو false.")
    override: int | None | object = ...
    if "daily_limit_override" in body:
        raw = body.get("daily_limit_override")
        if raw is None or raw == "":
            override = None
        else:
            try:
                override = int(raw)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail="الحد اليومي الفردي لازم يكون رقمًا أو null.") from exc
            if not 1 <= override <= 100:
                raise HTTPException(status_code=422, detail="الحد اليومي الفردي لازم يكون من 1 إلى 100.")
    try:
        store.update_user_access(target_id, blocked=blocked if "blocked" in body else None, daily_limit_override=override)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.add_event(actor_id, "user_access_changed", f"target={target_id};fields={','.join(body.keys())}")
    if body.get("blocked") is True:
        await notify_owner(f"⛔ تم إيقاف مستخدم من الإدارة\nالمستخدم: {target_id}\nبواسطة: {actor_id}")
    item = next((x for x in store.list_users(100) if int(x["user_id"]) == target_id), None)
    if item:
        item.update({"is_admin": store.is_admin(target_id), "is_owner": store.is_owner(target_id)})
    return {"ok": True, "user": item}


@api.get("/api/admin/admins")
async def api_admin_list_admins(user: dict[str, Any] = Depends(auth_from_header)):
    if not store.is_owner(int(user["id"])):
        raise HTTPException(status_code=403, detail="إدارة المشرفين للمالك فقط.")
    return {"items": store.list_admins()}


@api.post("/api/admin/admins")
async def api_admin_add_admin(request: Request, user: dict[str, Any] = Depends(auth_from_header)):
    user_id = int(user["id"])
    if not store.is_owner(user_id):
        raise HTTPException(status_code=403, detail="إضافة المشرفين للمالك فقط.")
    body = await request.json()
    try:
        target = int(body.get("user_id"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="أرسل Telegram user ID رقميًا.") from exc
    if target <= 0:
        raise HTTPException(status_code=422, detail="رقم المستخدم غير صالح.")
    store.add_admin(target, user_id)
    return {"ok": True, "admins": store.list_admins()}


@api.delete("/api/admin/admins/{target_id}")
async def api_admin_remove_admin(target_id: int, user: dict[str, Any] = Depends(auth_from_header)):
    user_id = int(user["id"])
    if not store.is_owner(user_id):
        raise HTTPException(status_code=403, detail="إزالة المشرفين للمالك فقط.")
    try:
        store.remove_admin(target_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "admins": store.list_admins()}


if __name__ == "__main__":
    uvicorn.run("app:api", host="0.0.0.0", port=PORT, proxy_headers=True, workers=1)
