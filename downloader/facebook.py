from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from downloader.common import download_ytdlp, extract_ytdlp_info


def can_handle(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host in {"fb.watch", "fb.com", "facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"} or host.endswith(".facebook.com")


def extract_info(url: str, settings: dict[str, Any]) -> dict[str, Any]:
    # Facebook public video/reel URLs are handled by yt-dlp's Facebook extractor.
    return extract_ytdlp_info(url, settings)


def download(url: str, out_dir: str, choice: str, settings: dict[str, Any], progress: dict[str, Any]) -> str:
    return download_ytdlp(url, out_dir, choice, settings, progress)
