from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from downloader import facebook, generic, instagram, youtube
from downloader.common import available_heights, estimate_size, validate_public_url

PLUGINS = [
    ("youtube", youtube),
    ("facebook", facebook),
    ("instagram", instagram),
]


def detect_platform(url: str) -> str:
    for name, plugin in PLUGINS:
        if plugin.can_handle(url):
            return name
    return "generic"


def plugin_for(platform: str):
    return {"youtube": youtube, "facebook": facebook, "instagram": instagram}.get(platform, generic)


def assert_enabled(platform: str, settings: dict[str, Any]) -> None:
    key = f"{platform}_enabled" if platform in {"youtube", "facebook", "instagram"} else "generic_enabled"
    if not bool(settings.get(key, True)):
        raise ValueError(f"المعالجة متوقفة حاليًا لمنصة {platform} من إعدادات الإدارة.")


def analyze(url: str, settings: dict[str, Any]) -> dict[str, Any]:
    validate_public_url(url)
    platform = detect_platform(url)
    assert_enabled(platform, settings)
    plugin = plugin_for(platform)
    info = plugin.extract_info(url, settings)
    info["_ahmx_platform"] = platform
    if info.get("_fallback_provider") == "cobalt":
        info["_ahmx_platform"] = platform
    return info


def serializable_info(info: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    platform = str(info.get("_ahmx_platform") or "generic")
    max_height = int(settings.get("max_video_height", 1080))
    heights = available_heights(info, max_height)
    if not heights and info.get("_fallback_provider") == "cobalt":
        heights = [x for x in [360, 480, 720, 1080] if x <= max_height]
    formats = []
    for height in heights:
        size = estimate_size(info, str(height))
        formats.append({"choice": str(height), "label": f"{height}p", "size": size, "size_text": _format_size(size)})
    if bool(settings.get("audio_enabled", True)):
        a_size = estimate_size(info, "audio")
        formats.append({"choice": "audio", "label": "MP3", "size": a_size, "size_text": _format_size(a_size)})
    return {
        "analysis_id": str(info.get("_ahmx_analysis_id") or ""),
        "platform": platform,
        "title": str(info.get("title") or "فيديو بدون عنوان")[:400],
        "uploader": str(info.get("uploader") or info.get("channel") or info.get("artist") or "غير معروف")[:120],
        "duration": _duration(info.get("duration")),
        "duration_seconds": info.get("duration"),
        "views": int(info["view_count"]) if isinstance(info.get("view_count"), (int, float)) else None,
        "thumbnail": str(info.get("thumbnail") or "")[:1600],
        "formats": formats,
        "fallback_provider": info.get("_fallback_provider") or "yt-dlp",
        "note": info.get("_fallback_note", ""),
    }


def _format_size(size: int | None) -> str:
    from downloader.common import format_bytes
    return format_bytes(size)


def _duration(value: Any) -> str:
    from downloader.common import duration_text
    return duration_text(value)
