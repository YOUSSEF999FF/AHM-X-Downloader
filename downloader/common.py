from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp

from config import NODE_PATH

HEIGHT_STEPS = [144, 240, 360, 480, 540, 720, 1080, 1440, 2160]
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav"}


class DownloadTooLarge(RuntimeError):
    pass


class DownloadCancelled(RuntimeError):
    pass


def validate_public_url(url: str) -> str:
    """Validates URL basics and blocks private/local destinations to reduce SSRF risk."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("الرابط لازم يبدأ بـ http:// أو https://")
    if parsed.username or parsed.password:
        raise ValueError("الروابط اللي تحتوي بيانات دخول داخل العنوان غير مسموحة.")
    if len(url) > 4096:
        raise ValueError("الرابط طويل جدًا.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("الرابط لازم يكون لموقع عام.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            records = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(row[4][0]) for row in records}
        except (socket.gaierror, OSError) as exc:
            raise ValueError("تعذر الوصول إلى اسم الموقع. راجع الرابط وحاول مرة أخرى.") from exc
    if not addresses or any(not ip.is_global for ip in addresses):
        raise ValueError("الرابط لا يشير إلى موقع عام مسموح به.")
    return host


def base_options(settings: dict[str, Any]) -> dict[str, Any]:
    node = os.getenv("YTDLP_NODE_PATH", NODE_PATH or shutil.which("node") or "node")
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "cachedir": False,
        "restrictfilenames": True,
        "js_runtimes": {"node": {"path": node}},
    }
    # The installed yt-dlp-ejs package provides the JS challenge components. The
    # option below allows yt-dlp to retrieve its official remote EJS component if needed.
    options["remote_components"] = {"ejs:github"}
    return options


def extract_ytdlp_info(url: str, settings: dict[str, Any]) -> dict[str, Any]:
    validate_public_url(url)
    options = base_options(settings)
    options.update({"skip_download": True, "extract_flat": False})
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError("الموقع لم يرجع بيانات فيديو صالحة.")
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = info.get("entries") or []
        info = next((entry for entry in entries if entry), None)
        if not isinstance(info, dict):
            raise ValueError("الرابط لا يحتوي فيديو متاحًا.")
    return info


def filesize(fmt: dict[str, Any] | None) -> int | None:
    if not fmt:
        return None
    val = fmt.get("filesize") or fmt.get("filesize_approx")
    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None


def format_bytes(value: int | None) -> str:
    if not value or value < 1:
        return "الحجم غير معروف"
    mib = value / 1024 / 1024
    return f"≈ {mib:.1f} MB" if mib < 1024 else f"≈ {mib / 1024:.2f} GB"


def duration_text(value: Any) -> str:
    try:
        seconds = max(0, int(value or 0))
    except (ValueError, TypeError):
        return "غير معروف"
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}:{mins:02}:{secs:02}" if hours else f"{mins}:{secs:02}"


def available_heights(info: dict[str, Any], max_height: int = 1080) -> list[int]:
    found: set[int] = set()
    for fmt in info.get("formats") or []:
        try:
            height = int(fmt.get("height") or 0)
        except (ValueError, TypeError):
            continue
        if 0 < height <= max_height and fmt.get("vcodec") not in (None, "none"):
            found.add(height)
    if not found:
        try:
            height = int(info.get("height") or 0)
            if 0 < height <= max_height:
                found.add(height)
        except (ValueError, TypeError):
            pass
    if not found and info.get("_fallback_provider") == "cobalt":
        found = {h for h in HEIGHT_STEPS if h <= max_height}
    choices = []
    for ceiling in HEIGHT_STEPS:
        if ceiling > max_height:
            continue
        candidates = [h for h in found if h <= ceiling]
        if candidates:
            exact = max(candidates)
            if exact not in choices:
                choices.append(exact)
    if found:
        highest = max(found)
        if highest not in choices:
            choices.append(highest)
    return sorted(set(choices))


def estimate_size(info: dict[str, Any], choice: str) -> int | None:
    if choice == "audio":
        try:
            dur = float(info.get("duration") or 0)
            return int(128_000 / 8 * dur * 1.08) if dur > 0 else None
        except (ValueError, TypeError):
            return None
    try:
        target = int(choice)
    except (ValueError, TypeError):
        return filesize(info)
    formats = info.get("formats") or []
    candidates = []
    for f in formats:
        try:
            h = int(f.get("height") or 0)
        except (ValueError, TypeError):
            continue
        if 0 < h <= target and f.get("vcodec") not in (None, "none"):
            candidates.append(f)
    candidates.sort(key=lambda f: (int(f.get("height") or 0), float(f.get("tbr") or 0)), reverse=True)
    video = candidates[0] if candidates else info
    v_size = filesize(video)
    if video.get("acodec") not in (None, "none") and v_size:
        return v_size
    audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio_formats.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 0), reverse=True)
    a_size = filesize(audio_formats[0]) if audio_formats else None
    if v_size and a_size:
        return v_size + a_size
    try:
        duration = float(info.get("duration") or 0)
        vbr = float(video.get("vbr") or video.get("tbr") or 0)
        abr = float((audio_formats[0] if audio_formats else {}).get("abr") or 0)
        estimate = int((vbr + abr) * 1000 * duration / 8)
        return estimate if estimate > 0 else v_size
    except (ValueError, TypeError):
        return v_size


def download_ytdlp(
    url: str,
    out_dir: str,
    choice: str,
    settings: dict[str, Any],
    progress: dict[str, Any],
) -> str:
    validate_public_url(url)
    limit_bytes = int(settings.get("max_upload_mb", 47)) * 1024 * 1024
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    template = str(out_path / "AHMX_%(title).75B_%(id)s.%(ext)s")
    options = base_options(settings)
    options.update({
        "outtmpl": template,
        "paths": {"home": str(out_path), "temp": str(out_path)},
        "noplaylist": True,
        "overwrites": True,
        "max_filesize": limit_bytes,
        "merge_output_format": "mp4",
    })

    def progress_hook(data: dict[str, Any]) -> None:
        if progress.get("cancel_requested"):
            raise DownloadCancelled("تم إلغاء التنزيل بناءً على طلب المستخدم.")
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            if downloaded > limit_bytes:
                raise DownloadTooLarge(f"الملف تخطى حد {settings.get('max_upload_mb',47)} MB أثناء التنزيل.")
            progress.update({"downloaded_bytes": downloaded, "total_bytes": total, "speed": str(data.get("_speed_str") or "")[:40], "phase": "تنزيل الفيديو"})
            progress["progress"] = min(99.0, (downloaded / total * 100) if total else 0.0)
        elif status == "finished":
            progress.update({"phase": "دمج الصوت والفيديو", "progress": 98.0})

    options["progress_hooks"] = [progress_hook]
    if choice == "audio":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
        })
    else:
        height = int(choice)
        options["format"] = (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        )

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    candidates = [p for p in out_path.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS and not p.name.endswith(".part")]
    if not candidates:
        raise RuntimeError("اكتمل طلب التنزيل لكن لم يتم العثور على ملف صالح.")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    result = candidates[0]
    size = result.stat().st_size
    if size > limit_bytes:
        raise DownloadTooLarge(f"الحجم النهائي {format_bytes(size)} أكبر من حد الإرسال.")
    progress.update({"phase": "اكتمل التنزيل", "progress": 100.0, "downloaded_bytes": size, "total_bytes": size})
    return str(result)
