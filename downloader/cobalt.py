from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from config import COBALT_API_KEY, COBALT_API_URL
from downloader.common import DownloadCancelled, DownloadTooLarge, format_bytes, validate_public_url


def configured() -> bool:
    return bool(COBALT_API_URL)


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "AHM-X-Downloader/4.0"}
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    return headers


def fallback_metadata(url: str) -> dict[str, Any]:
    """Get public oEmbed metadata without login; fallback only creates a quality ladder."""
    import urllib.parse
    safe_url = url
    validate_public_url(safe_url)
    oembed = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": safe_url, "format": "json"})
    req = urllib.request.Request(oembed, headers={"User-Agent": "Mozilla/5.0 AHM-X-Downloader/4.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read(512_000).decode("utf-8"))
    except Exception:
        data = {}
    parsed = urlparse(url)
    video_id = ""
    if parsed.hostname and parsed.hostname.lower() in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        qs = urllib.parse.parse_qs(parsed.query)
        video_id = (qs.get("v") or [""])[0]
        if not video_id:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                video_id = parts[1]
    return {
        "id": video_id[:30],
        "title": str(data.get("title") or f"YouTube video {video_id or ''}").strip()[:400],
        "uploader": str(data.get("author_name") or "YouTube").strip()[:120],
        "thumbnail": data.get("thumbnail_url") or (f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else None),
        "duration": None,
        "view_count": None,
        "formats": [],
        "_fallback_provider": "cobalt",
        "_fallback_note": "Metadata fallback; quality size is not available until download.",
    }


def download_with_cobalt(url: str, out_dir: str, choice: str, settings: dict[str, Any], progress: dict[str, Any]) -> str:
    if not configured():
        raise RuntimeError("Fallback Cobalt غير مفعّل. اضبط COBALT_API_URL على نسخة Cobalt تملكها أو لديك تصريح باستخدامها.")
    validate_public_url(url)
    quality = str(max(144, min(4320, int(choice) if choice != "audio" else 1080))) if choice != "audio" else "1080"
    body = {"url": url, "downloadMode": "audio" if choice == "audio" else "auto", "audioFormat": "mp3", "audioBitrate": "128", "videoQuality": quality, "filenameStyle": "basic", "youtubeVideoContainer": "mp4"}
    endpoint = COBALT_API_URL.rstrip("/") + "/"
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=_headers(), method="POST")
    progress.update({"phase": "محاولة محرك بديل مصرح به (Cobalt)", "progress": 1.0})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", "replace")
        raise RuntimeError(f"Cobalt API HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"تعذر الاتصال بمزوّد Cobalt: {type(exc).__name__}: {exc}") from exc
    status = payload.get("status")
    if status not in {"tunnel", "redirect"} or not payload.get("url"):
        code = ((payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None) or status or "unknown"
        raise RuntimeError(f"Cobalt لم يرجع رابط ملف مباشر (status={code}). قد يكون الموقع أو الرابط غير مدعوم.")
    media_url = str(payload["url"])
    if urlparse(media_url).scheme != "https":
        raise RuntimeError("رابط الملف العائد من Cobalt لا يستخدم HTTPS وتم رفضه.")
    validate_public_url(media_url)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = str(payload.get("filename") or ("AHMX-audio.mp3" if choice == "audio" else "AHMX-video.mp4"))
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")[:160] or "AHMX-download.mp4"
    if "." not in name:
        name += ".mp3" if choice == "audio" else ".mp4"
    target = out / name
    max_bytes = int(settings.get("max_upload_mb", 47)) * 1024 * 1024
    request_file = urllib.request.Request(media_url, headers={"User-Agent": "AHM-X-Downloader/4.0"})
    try:
        with urllib.request.urlopen(request_file, timeout=60) as response, target.open("wb") as handle:
            length = int(response.headers.get("Content-Length") or 0)
            if length > max_bytes:
                raise DownloadTooLarge(f"حجم الملف المتوقع {format_bytes(length)} يتجاوز الحد.")
            written = 0
            while True:
                if progress.get("cancel_requested"):
                    raise DownloadCancelled("تم إلغاء التنزيل بناءً على طلب المستخدم.")
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise DownloadTooLarge(f"الملف تجاوز حد {settings.get('max_upload_mb',47)} MB أثناء التنزيل.")
                handle.write(chunk)
                progress.update({"downloaded_bytes": written, "total_bytes": length, "progress": min(99.0, written / length * 100 if length else 0.0), "phase": "تنزيل عبر Cobalt"})
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    if target.stat().st_size < 1024:
        target.unlink(missing_ok=True)
        raise RuntimeError("المزوّد رجّع ملفًا صغيرًا/غير مكتمل؛ تم رفضه.")
    size = target.stat().st_size
    progress.update({"phase": "اكتمل التنزيل عبر Cobalt", "progress": 100.0, "downloaded_bytes": size, "total_bytes": size})
    return str(target)
