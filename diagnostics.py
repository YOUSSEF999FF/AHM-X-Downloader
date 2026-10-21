from __future__ import annotations

import re
import traceback
import uuid
from urllib.parse import urlsplit, urlunsplit
from typing import Any

import store

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bot\d{5,}:[A-Za-z0-9_-]{20,})"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(token|cookie|password|secret)(\s*[:=]\s*)([^\s,;]+)"),
]


def redact(text: str) -> str:
    out = str(text or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 1:
            out = pattern.sub("[REDACTED]", out)
        else:
            out = pattern.sub(lambda m: (m.group(1) or "") + "[REDACTED]", out)
    return out[:18000]


def safe_source_hint(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path[:120], "[query-redacted]" if parsed.query else "", ""))[:220]
    except Exception:
        return "[unavailable]"


def capture_exception(
    exc: BaseException,
    *,
    source: str,
    user_id: int = 0,
    platform: str = "",
    url: str | None = None,
    include_traceback: bool = True,
) -> str:
    error_id = uuid.uuid4().hex[:10].upper()
    message = redact(str(exc) or repr(exc))
    tb = redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))) if include_traceback else ""
    store.add_error(
        error_id=error_id,
        user_id=user_id,
        source=source,
        platform=platform,
        error_type=type(exc).__name__,
        message=message,
        traceback_text=tb,
        source_hint=safe_source_hint(url),
    )
    return error_id


def capture_message(message: str, *, source: str, user_id: int = 0, platform: str = "", url: str | None = None) -> str:
    class LoggedMessageError(RuntimeError):
        pass
    return capture_exception(LoggedMessageError(message), source=source, user_id=user_id, platform=platform, url=url, include_traceback=False)


def public_error(error_id: str, exc: BaseException) -> dict[str, str]:
    raw = str(exc or "")
    low = raw.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        msg = "المنصة رفضت طلب الاستضافة وطلبت تحققًا إضافيًا. جرّب رابطًا عامًا آخر، أو فعّل مزوّد Cobalt خاصًا بك من إعدادات الخادم إن كان متاحًا. لا نستخدم بيانات دخول شخصية أو نتجاوز الحماية."
    elif "unsupported url" in low or "no suitable extractor" in low:
        msg = "الرابط أو الموقع غير مدعوم حاليًا. تأكد أنك أرسلت رابط الفيديو نفسه."
    elif "private" in low or "login required" in low or "members-only" in low or "sign in" in low:
        msg = "المحتوى يتطلب تسجيل دخول أو صلاحية غير متاحة للبوت. استخدم محتوى عامًا مسموحًا بتنزيله."
    elif "max-filesize" in low or "too large" in low or "file size" in low or "أكبر من الحد" in low or "تخطى حد" in low or "يتجاوز حد الإرسال" in low:
        msg = "الملف أكبر من الحد المضبوط. جرّب جودة أقل أو فيديو أقصر."
    elif "timed out" in low or "timeout" in low:
        msg = "انتهت مهلة الاتصال بالموقع. حاول مرة أخرى لاحقًا."
    elif "ffmpeg" in low:
        msg = "هناك مشكلة في FFmpeg أو دمج الصوت والفيديو. راجع فحص /health وسجل الأعطال للمالك."
    else:
        msg = "فشلت العملية. رقم التشخيص محفوظ ويمكن للمالك رؤية السبب التفصيلي من لوحة الإدارة."
    return {"error_id": error_id, "message": msg, "type": type(exc).__name__}
