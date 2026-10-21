from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from downloader import cobalt
from downloader.common import download_ytdlp, extract_ytdlp_info


def can_handle(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"} or host.endswith(".youtube.com")


def extract_info(url: str, settings: dict[str, Any]) -> dict[str, Any]:
    try:
        return extract_ytdlp_info(url, settings)
    except Exception as first_error:
        raw = str(first_error).lower()
        blocked = any(term in raw for term in ("sign in to confirm", "confirm you're not a bot", "confirm you’re not a bot", "login_required", "not a bot"))
        if blocked and cobalt.configured():
            info = cobalt.fallback_metadata(url)
            info["_primary_error"] = str(first_error)[:900]
            return info
        raise


def download(url: str, out_dir: str, choice: str, settings: dict[str, Any], progress: dict[str, Any]) -> str:
    if progress.get("provider") == "cobalt" or progress.get("metadata_provider") == "cobalt":
        return cobalt.download_with_cobalt(url, out_dir, choice, settings, progress)
    try:
        return download_ytdlp(url, out_dir, choice, settings, progress)
    except Exception as first_error:
        raw = str(first_error).lower()
        blocked = any(term in raw for term in ("sign in to confirm", "confirm you're not a bot", "confirm you’re not a bot", "login_required", "not a bot"))
        if blocked and cobalt.configured() and not progress.get("cancel_requested"):
            progress["provider"] = "cobalt"
            return cobalt.download_with_cobalt(url, out_dir, choice, settings, progress)
        raise
