from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from config import DATA_DIR, DOWNLOAD_DIR, OWNER_ID
from diagnostics import capture_exception, capture_message, public_error
import store
from downloader import registry
from downloader.common import DownloadCancelled, DownloadTooLarge, available_heights, estimate_size, format_bytes

analyses: dict[str, dict[str, Any]] = {}
active_progress: dict[str, dict[str, Any]] = {}
file_paths: dict[str, str] = {}
last_analysis_at: dict[int, float] = {}
_error_notifier: Callable[[str], Awaitable[Any]] | None = None


def set_error_notifier(callback: Callable[[str], Awaitable[Any]] | None) -> None:
    global _error_notifier
    _error_notifier = callback


class DynamicLimiter:
    """A live adjustable limiter: admin changes take effect without restarting."""
    def __init__(self) -> None:
        self.active = 0
        self.condition = asyncio.Condition()

    async def enter(self, max_active: int | Callable[[], int], progress: dict[str, Any] | None = None) -> None:
        def current_limit() -> int:
            try:
                value = max_active() if callable(max_active) else max_active
                return max(1, int(value))
            except Exception:
                return 1

        async with self.condition:
            # Re-read the value after every admin notification, so live limit changes apply
            # to jobs already waiting in the queue, not only jobs created afterward.
            await self.condition.wait_for(lambda: self.active < current_limit() or bool(progress and progress.get("cancel_requested")))
            if progress and progress.get("cancel_requested"):
                raise DownloadCancelled("تم إلغاء التنزيل أثناء الانتظار.")
            self.active += 1

    async def leave(self) -> None:
        async with self.condition:
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    async def notify_limit_change(self) -> None:
        async with self.condition:
            self.condition.notify_all()


limiter = DynamicLimiter()


def _prune() -> None:
    now = time.time()
    for key, item in list(analyses.items()):
        if now - item.get("created", now) > 1800:
            analyses.pop(key, None)
    for key, item in list(active_progress.items()):
        if now - item.get("created", now) > 3600 and item.get("status") in {"completed", "failed", "cancelled"}:
            active_progress.pop(key, None)
    for key, path in list(file_paths.items()):
        job = store.get_job(key)
        expiry = 0
        try:
            expiry = time.time() - Path(path).stat().st_mtime
        except OSError:
            expiry = 999999
        if expiry > 1800:
            try:
                shutil.rmtree(Path(path).parent, ignore_errors=True)
            except OSError:
                pass
            file_paths.pop(key, None)


def analyze_url(url: str, settings: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    _prune()
    info = registry.analyze(url, settings)
    analysis_id = uuid.uuid4().hex[:14]
    info["_ahmx_analysis_id"] = analysis_id
    analyses[analysis_id] = {"created": time.time(), "url": url, "info": info, "user_id": 0}
    return analysis_id, info


def build_summary(info: dict[str, Any], settings: dict[str, Any], analysis_id: str = "") -> dict[str, Any]:
    serial = registry.serializable_info(info, settings)
    serial["analysis_id"] = analysis_id or serial.get("analysis_id") or ""
    return serial


def _set_progress(job_id: str, progress: dict[str, Any]) -> None:
    current = active_progress.get(job_id)
    if current is not None:
        current.update({k: v for k, v in progress.items() if k not in {"cancel_requested", "created"}})
        store.update_job(job_id, **{k: current[k] for k in ("status", "progress", "phase", "downloaded_bytes", "total_bytes", "speed") if k in current})


def _download_sync(job_id: str, url: str, platform: str, info: dict[str, Any], choice: str, settings: dict[str, Any], progress: dict[str, Any]) -> str:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = tempfile.mkdtemp(prefix=f"ahmx_{job_id}_", dir=str(DOWNLOAD_DIR))
    progress["phase"] = "بدء محرك التنزيل"
    plugin = registry.plugin_for(platform)
    if info.get("_fallback_provider") == "cobalt":
        progress["metadata_provider"] = "cobalt"
    try:
        return plugin.download(url, out_dir, choice, settings, progress)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


async def start_download(job_id: str, analysis_id: str, user_id: int, choice: str, settings: dict[str, Any]) -> dict[str, Any]:
    analysis = analyses.get(analysis_id)
    if not analysis or time.time() - analysis.get("created", 0) > 1800:
        raise ValueError("انتهت صلاحية تحليل الرابط. حلله مرة أخرى.")
    if analysis.get("user_id") not in {0, user_id}:
        raise PermissionError("تحليل الرابط ده مش خاص بيك.")
    info = analysis["info"]
    platform = str(info.get("_ahmx_platform") or "generic")
    if choice != "audio" and not str(choice).isdigit():
        raise ValueError("اختيار الجودة غير صالح.")
    if choice == "audio" and not settings.get("audio_enabled", True):
        raise ValueError("استخراج الصوت متوقف من الإدارة.")
    allowed = {str(x["choice"]) for x in build_summary(info, settings, analysis_id).get("formats", [])}
    if choice not in allowed:
        raise ValueError("الجودة غير موجودة ضمن الاختيارات المتاحة لهذا الرابط.")
    if settings.get("maintenance_mode") and not store.is_admin(user_id):
        raise PermissionError("البوت في وضع الصيانة حاليًا.")
    if not settings.get("downloads_enabled") and not store.is_admin(user_id):
        raise PermissionError("التنزيلات متوقفة مؤقتًا من الإدارة.")
    parsed = urlparse(str(analysis["url"]))
    title = str(info.get("title") or "فيديو بدون عنوان")[:400]
    store.create_download_job(job_id, user_id, platform, title, parsed.hostname or "unknown", choice)
    progress = {
        "created": time.time(), "user_id": user_id, "job_id": job_id, "analysis_id": analysis_id,
        "status": "queued", "progress": 0.0, "phase": "في قائمة الانتظار", "downloaded_bytes": 0,
        "total_bytes": int(estimate_size(info, choice) or 0), "speed": "", "cancel_requested": False,
        "title": title, "platform": platform, "choice": choice, "info": info, "url": analysis["url"],
    }
    active_progress[job_id] = progress
    asyncio.create_task(_run_job(job_id, analysis["url"], platform, info, choice, settings, progress))
    return {"job_id": job_id, "status": "queued"}


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            _prune()
        except Exception:
            # Cleanup must never crash the Web Service.
            pass


async def _run_job(job_id: str, url: str, platform: str, info: dict[str, Any], choice: str, settings: dict[str, Any], progress: dict[str, Any]) -> None:
    path: str | None = None
    entered = False
    try:
        await limiter.enter(lambda: int(store.get_settings().get("max_concurrent", settings.get("max_concurrent", 1))), progress)
        entered = True
        if progress.get("cancel_requested"):
            raise DownloadCancelled("تم إلغاء التنزيل.")
        progress["status"] = "running"
        progress["phase"] = "بدء استخراج الملف"
        store.update_job(job_id, status="running", phase=progress["phase"])
        download_task = asyncio.create_task(asyncio.to_thread(_download_sync, job_id, url, platform, info, choice, store.get_settings(), progress))
        while not download_task.done():
            await asyncio.sleep(1.2)
            if progress.get("cancel_requested"):
                # yt-dlp checks cancellation through its progress hook.
                pass
            _set_progress(job_id, progress)
        path = await download_task
        if progress.get("cancel_requested"):
            raise DownloadCancelled("تم إلغاء التنزيل.")
        p = Path(path)
        if not p.exists() or p.stat().st_size < 512:
            raise RuntimeError("ملف التنزيل غير موجود أو غير مكتمل.")
        size = p.stat().st_size
        max_bytes = int(store.get_settings().get("max_upload_mb", 47)) * 1024 * 1024
        if size > max_bytes:
            raise DownloadTooLarge(f"الحجم النهائي {format_bytes(size)} يتجاوز حد الإرسال.")
        progress.update({"status": "completed", "phase": "اكتمل التنزيل — جاهز للحفظ", "progress": 100.0, "downloaded_bytes": size, "total_bytes": size, "file_size": size})
        file_paths[job_id] = path
        store.update_job(job_id, status="completed", phase=progress["phase"], progress=100.0, downloaded_bytes=size, total_bytes=size, file_size=size, file_name=p.name)
    except DownloadCancelled as exc:
        progress.update({"status": "cancelled", "phase": "تم الإلغاء"})
        store.update_job(job_id, status="cancelled", phase=progress["phase"])
        if path:
            Path(path).unlink(missing_ok=True)
    except Exception as exc:
        error_id = capture_exception(exc, source="download_job", user_id=int(progress.get("user_id") or 0), platform=platform, url=url)
        progress.update({"status": "failed", "phase": "فشل التنزيل", "error_id": error_id, "error_message": str(exc)[:600]})
        store.update_job(job_id, status="failed", phase=progress["phase"], error_id=error_id)
        if path:
            Path(path).unlink(missing_ok=True)
        if _error_notifier is not None:
            try:
                await _error_notifier(f"🚨 فشل تنزيل داخل AHM X Downloader V4\nرقم التشخيص: {error_id}\nالمنصة: {platform}\nالنوع: {type(exc).__name__}\n{str(exc)[:420]}")
            except Exception:
                pass
    finally:
        # If job failed early, remove any partial output directory.
        if path and progress.get("status") != "completed":
            try:
                shutil.rmtree(Path(path).parent, ignore_errors=True)
            except OSError:
                pass
        if entered:
            await limiter.leave()
        else:
            await limiter.notify_limit_change()


def request_cancel(job_id: str, user_id: int) -> bool:
    job = active_progress.get(job_id)
    if not job or job.get("user_id") != user_id or job.get("status") not in {"queued", "running"}:
        return False
    job["cancel_requested"] = True
    try:
        asyncio.get_running_loop().create_task(limiter.notify_limit_change())
    except RuntimeError:
        pass
    return True


def get_live_job(job_id: str, user_id: int) -> dict[str, Any] | None:
    job = store.get_job(job_id)
    if not job or int(job["user_id"]) != user_id:
        return None
    live = active_progress.get(job_id, {})
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": float(live.get("progress", job.get("progress", 0)) or 0),
        "phase": str(live.get("phase", job.get("phase", ""))),
        "downloaded_bytes": int(live.get("downloaded_bytes", job.get("downloaded_bytes", 0)) or 0),
        "total_bytes": int(live.get("total_bytes", job.get("total_bytes", 0)) or 0),
        "speed": str(live.get("speed", job.get("speed", "")) or ""),
        "title": str(live.get("title", job.get("title", "")) or ""),
        "platform": str(job.get("platform", "")),
        "file_size": int(job.get("file_size", 0) or 0),
        "error_id": str(job.get("error_id", "") or ""),
        "error_message": public_error(job.get("error_id", ""), RuntimeError(str(live.get("error_message", "")))).get("message", "") if job["status"] == "failed" else "",
    }
