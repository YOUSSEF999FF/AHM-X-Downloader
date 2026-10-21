from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl
from typing import Any

from config import ALLOW_DEV_WEBAPP_AUTH, BOT_TOKEN, DEV_USER_ID, INIT_DATA_MAX_AGE_SECONDS


class WebAppAuthError(ValueError):
    pass


def validate_init_data(init_data: str) -> dict[str, Any]:
    if ALLOW_DEV_WEBAPP_AUTH and DEV_USER_ID > 0 and not init_data:
        return {"id": DEV_USER_ID, "first_name": "Development", "username": "dev"}
    if not BOT_TOKEN:
        raise WebAppAuthError("البوت غير مهيأ للتحقق من هوية Telegram.")
    if not init_data or len(init_data) > 8192:
        raise WebAppAuthError("بيانات جلسة Telegram غير موجودة أو غير صالحة.")
    values = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    supplied_hash = values.pop("hash", "")
    if not supplied_hash:
        raise WebAppAuthError("جلسة Telegram لا تحتوي على توقيع صالح.")
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise WebAppAuthError("توقيع جلسة Telegram غير صحيح.")
    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError as exc:
        raise WebAppAuthError("وقت جلسة Telegram غير صالح.") from exc
    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        raise WebAppAuthError("انتهت صلاحية جلسة Telegram. أغلق التطبيق وافتحه مرة أخرى.")
    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise WebAppAuthError("بيانات المستخدم من Telegram غير صالحة.") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise WebAppAuthError("تعذّر تحديد مستخدم Telegram.")
    return user
