from __future__ import annotations

import os
from pathlib import Path


def _ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            result.add(int(item))
    return result


BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
PUBLIC_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
NODE_PATH = os.getenv("YTDLP_NODE_PATH", "/usr/local/bin/node")
DB_PATH = Path(os.getenv("DB_PATH", "./data/ahmx_v4.sqlite3"))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/ahmx-v4"))
COBALT_API_URL = (os.getenv("COBALT_API_URL", "").strip().rstrip("/"))
COBALT_API_KEY = os.getenv("COBALT_API_KEY", "").strip()
INIT_DATA_MAX_AGE_SECONDS = int(os.getenv("INIT_DATA_MAX_AGE_SECONDS", "86400"))
ALLOW_DEV_WEBAPP_AUTH = os.getenv("ALLOW_DEV_WEBAPP_AUTH", "0") == "1"
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "0") or 0)

DEFAULT_SETTINGS: dict[str, object] = {
    "maintenance_mode": False,
    "downloads_enabled": True,
    "max_concurrent": 1,
    "daily_limit": 8,
    "max_upload_mb": max(5, min(int(os.getenv("MAX_UPLOAD_MB", "47")), 47)),
    "max_video_height": 1080,
    "cooldown_seconds": 4,
    "youtube_enabled": True,
    "facebook_enabled": True,
    "instagram_enabled": True,
    "generic_enabled": True,
    "audio_enabled": True,
    "owner_alerts_enabled": True,
    "allow_private_chats_only": True,
}

SETTING_SPECS: dict[str, dict[str, object]] = {
    "maintenance_mode": {"type": "bool", "label": "وضع الصيانة"},
    "downloads_enabled": {"type": "bool", "label": "السماح بالتنزيل"},
    "max_concurrent": {"type": "int", "min": 1, "max": 4, "label": "عدد التنزيلات المتزامنة"},
    "daily_limit": {"type": "int", "min": 1, "max": 100, "label": "حد التحميل اليومي لكل مستخدم"},
    "max_upload_mb": {"type": "int", "min": 5, "max": 47, "label": "أقصى حجم للإرسال MB"},
    "max_video_height": {"type": "int", "min": 144, "max": 2160, "label": "أعلى جودة فيديو"},
    "cooldown_seconds": {"type": "int", "min": 0, "max": 120, "label": "مهلة بين الطلبات بالثواني"},
    "youtube_enabled": {"type": "bool", "label": "معالجة YouTube"},
    "facebook_enabled": {"type": "bool", "label": "معالجة Facebook"},
    "instagram_enabled": {"type": "bool", "label": "معالجة Instagram"},
    "generic_enabled": {"type": "bool", "label": "معالجة المنصات الأخرى المدعومة"},
    "audio_enabled": {"type": "bool", "label": "استخراج الصوت"},
    "owner_alerts_enabled": {"type": "bool", "label": "تنبيهات المالك"},
    "allow_private_chats_only": {"type": "bool", "label": "العمل في الخاص فقط"},
}
