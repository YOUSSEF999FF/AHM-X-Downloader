from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from config import ADMIN_IDS, DB_PATH, DEFAULT_SETTINGS, OWNER_ID

_lock = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_day_start() -> str:
    # Daily limits reset at midnight Cairo time, stored/queried as a UTC instant.
    local_now = datetime.now(ZoneInfo("Africa/Cairo"))
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, _connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0,
            daily_limit_override INTEGER
        );
        CREATE TABLE IF NOT EXISTS download_jobs (
            job_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            source_host TEXT NOT NULL DEFAULT '',
            choice TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            progress REAL NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'queued',
            downloaded_bytes INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            speed TEXT NOT NULL DEFAULT '',
            file_size INTEGER NOT NULL DEFAULT 0,
            file_name TEXT NOT NULL DEFAULT '',
            error_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON download_jobs(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON download_jobs(status, created_at DESC);
        CREATE TABLE IF NOT EXISTS errors (
            error_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT '',
            error_type TEXT NOT NULL,
            message TEXT NOT NULL,
            traceback_text TEXT NOT NULL,
            source_hint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """)
        # Safe, idempotent schema migrations for databases created by earlier V4 builds.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "blocked" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        if "daily_limit_override" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN daily_limit_override INTEGER")
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value), utc_now()),
            )
        # Seed ADMIN_IDS only on a fresh admin table; otherwise a removed admin
        # would be silently re-added on every redeploy. Later admin changes belong
        # to the owner-controlled panel/database.
        existing_admins = int(db.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"])
        if existing_admins == 0:
            for admin_id in ADMIN_IDS:
                db.execute(
                    "INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)",
                    (admin_id, OWNER_ID, utc_now()),
                )
        if OWNER_ID > 0:
            db.execute(
                "INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)",
                (OWNER_ID, OWNER_ID, utc_now()),
            )


def get_settings() -> dict[str, Any]:
    with _lock, _connect() as db:
        rows = db.execute("SELECT key,value_json FROM settings").fetchall()
    return {row["key"]: json.loads(row["value_json"]) for row in rows}


def set_settings(patch: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    with _lock, _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for key, value in patch.items():
            db.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now),
            )
        db.execute("COMMIT")
    return get_settings()


def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID and OWNER_ID > 0:
        return True
    with _lock, _connect() as db:
        return db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def is_owner(user_id: int) -> bool:
    return OWNER_ID > 0 and user_id == OWNER_ID


def list_admins() -> list[dict[str, Any]]:
    with _lock, _connect() as db:
        rows = db.execute("SELECT user_id,added_by,added_at FROM admins ORDER BY added_at").fetchall()
    return [dict(row) for row in rows]


def add_admin(user_id: int, added_by: int) -> None:
    with _lock, _connect() as db:
        db.execute("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (user_id, added_by, utc_now()))


def remove_admin(user_id: int) -> None:
    if user_id == OWNER_ID:
        raise ValueError("لا يمكن إزالة المالك من قائمة الصلاحيات.")
    with _lock, _connect() as db:
        db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))


def touch_user(user_id: int, username: str = "", first_name: str = "") -> None:
    now = utc_now()
    with _lock, _connect() as db:
        db.execute(
            "INSERT INTO users(user_id,username,first_name,first_seen,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_seen=excluded.last_seen",
            (user_id, username[:100], first_name[:150], now, now),
        )


def user_count() -> int:
    with _lock, _connect() as db:
        row = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return int(row["c"])


def create_download_job(job_id: str, user_id: int, platform: str, title: str, source_host: str, choice: str) -> dict[str, Any]:
    now = utc_now()
    with _lock, _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        settings_rows = db.execute("SELECT key,value_json FROM settings").fetchall()
        settings = {r["key"]: json.loads(r["value_json"]) for r in settings_rows}
        override_row = db.execute("SELECT daily_limit_override FROM users WHERE user_id=?", (user_id,)).fetchone()
        override = override_row["daily_limit_override"] if override_row else None
        limit = int(override if override is not None else settings.get("daily_limit", DEFAULT_SETTINGS["daily_limit"]))
        day_start = utc_day_start()
        consumed_row = db.execute(
            "SELECT COUNT(*) AS c FROM download_jobs WHERE user_id=? AND created_at>=? AND status IN ('queued','running','completed')",
            (user_id, day_start),
        ).fetchone()
        consumed = int(consumed_row["c"])
        if consumed >= limit:
            db.execute("ROLLBACK")
            raise ValueError(f"وصلت للحد اليومي ({limit} تنزيلات). جرّب بعد منتصف الليل بتوقيت القاهرة.")
        db.execute(
            "INSERT INTO download_jobs(job_id,user_id,platform,title,source_host,choice,status,progress,phase,created_at,updated_at) VALUES(?,?,?,?,?,?, 'queued',0,'في قائمة الانتظار',?,?)",
            (job_id, user_id, platform, title[:400], source_host[:160], choice, now, now),
        )
        db.execute("COMMIT")
    return get_job(job_id) or {}


def update_job(job_id: str, **fields: Any) -> None:
    allowed = {"status", "progress", "phase", "downloaded_bytes", "total_bytes", "speed", "file_size", "file_name", "error_id", "title"}
    patch = {k: v for k, v in fields.items() if k in allowed}
    if not patch:
        return
    patch["updated_at"] = utc_now()
    assignments = ",".join(f"{key}=?" for key in patch)
    values = list(patch.values()) + [job_id]
    with _lock, _connect() as db:
        db.execute(f"UPDATE download_jobs SET {assignments} WHERE job_id=?", values)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock, _connect() as db:
        row = db.execute("SELECT * FROM download_jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def recover_interrupted_jobs() -> int:
    """Mark in-flight jobs from a previous process as interrupted after a redeploy/restart."""
    now = utc_now()
    with _lock, _connect() as db:
        cursor = db.execute(
            "UPDATE download_jobs SET status='failed',phase=?,error_id=?,updated_at=? WHERE status IN ('queued','running')",
            ("الخدمة أُعيد تشغيلها قبل انتهاء التنزيل", "SERVICE_RESTARTED", now),
        )
        return int(cursor.rowcount or 0)


def get_history(user_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 100))
    safe_offset = max(0, int(offset))
    with _lock, _connect() as db:
        rows = db.execute(
            "SELECT job_id,platform,title,source_host,choice,status,progress,phase,file_size,error_id,created_at,updated_at FROM download_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, safe_limit, safe_offset),
        ).fetchall()
    return [dict(row) for row in rows]


def get_usage(user_id: int) -> dict[str, int]:
    day_start = utc_day_start()
    with _lock, _connect() as db:
        complete = db.execute(
            "SELECT COUNT(*) AS c FROM download_jobs WHERE user_id=? AND created_at>=? AND status='completed'",
            (user_id, day_start),
        ).fetchone()["c"]
        active = db.execute(
            "SELECT COUNT(*) AS c FROM download_jobs WHERE user_id=? AND created_at>=? AND status IN ('queued','running')",
            (user_id, day_start),
        ).fetchone()["c"]
        all_time = db.execute(
            "SELECT COUNT(*) AS c FROM download_jobs WHERE user_id=? AND status='completed'",
            (user_id,),
        ).fetchone()["c"]
    with _lock, _connect() as db:
        override_row = db.execute("SELECT daily_limit_override FROM users WHERE user_id=?", (user_id,)).fetchone()
    override = override_row["daily_limit_override"] if override_row else None
    daily_limit = int(override if override is not None else get_settings().get("daily_limit", DEFAULT_SETTINGS["daily_limit"]))
    used = int(complete) + int(active)
    return {"completed_today": int(complete), "active_today": int(active), "used_today": used, "remaining_today": max(0, daily_limit - used), "daily_limit": daily_limit, "completed_total": int(all_time)}


def add_error(error_id: str, user_id: int, source: str, platform: str, error_type: str, message: str, traceback_text: str, source_hint: str = "") -> None:
    with _lock, _connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO errors(error_id,user_id,source,platform,error_type,message,traceback_text,source_hint,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (error_id, user_id, source[:80], platform[:40], error_type[:120], message[:1600], traceback_text[-18000:], source_hint[:220], utc_now()),
        )


def get_errors(limit: int = 30) -> list[dict[str, Any]]:
    with _lock, _connect() as db:
        rows = db.execute(
            "SELECT error_id,user_id,source,platform,error_type,message,traceback_text,source_hint,created_at FROM errors ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    return [dict(row) for row in rows]


def add_event(user_id: int, event_type: str, details: str = "") -> None:
    with _lock, _connect() as db:
        db.execute("INSERT INTO events(user_id,event_type,details,created_at) VALUES(?,?,?,?)", (user_id, event_type[:80], details[:500], utc_now()))


def get_stats() -> dict[str, int]:
    today = utc_day_start()
    with _lock, _connect() as db:
        users = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        today_jobs = db.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE created_at>=?", (today,)).fetchone()["c"]
        completed = db.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status='completed'").fetchone()["c"]
        failed = db.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status='failed'").fetchone()["c"]
        active = db.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status IN ('queued','running')").fetchone()["c"]
        error_count = db.execute("SELECT COUNT(*) AS c FROM errors").fetchone()["c"]
    return {"users": int(users), "today_jobs": int(today_jobs), "completed": int(completed), "failed": int(failed), "active": int(active), "errors": int(error_count)}


def is_blocked(user_id: int) -> bool:
    with _lock, _connect() as db:
        row = db.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["blocked"])


def update_user_access(user_id: int, *, blocked: bool | None = None, daily_limit_override: int | None | object = ...) -> None:
    with _lock, _connect() as db:
        if db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
            now = utc_now()
            db.execute("INSERT INTO users(user_id,first_seen,last_seen) VALUES(?,?,?)", (user_id, now, now))
        if blocked is not None:
            db.execute("UPDATE users SET blocked=? WHERE user_id=?", (1 if blocked else 0, user_id))
        if daily_limit_override is not ...:
            if daily_limit_override is not None and not 1 <= int(daily_limit_override) <= 100:
                raise ValueError("حد المستخدم لازم يكون من 1 إلى 100 أو null للحد العام.")
            db.execute("UPDATE users SET daily_limit_override=? WHERE user_id=?", (daily_limit_override, user_id))


def list_users(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    day_start = utc_day_start()
    global_limit = int(get_settings().get("daily_limit", DEFAULT_SETTINGS["daily_limit"]))
    with _lock, _connect() as db:
        rows = db.execute(
            "SELECT u.user_id,u.username,u.first_name,u.first_seen,u.last_seen,u.blocked,u.daily_limit_override, "
            "(SELECT COUNT(*) FROM download_jobs j WHERE j.user_id=u.user_id AND j.status='completed') AS completed_total, "
            "(SELECT COUNT(*) FROM download_jobs j WHERE j.user_id=u.user_id AND j.status='failed') AS failed_total, "
            "(SELECT COUNT(*) FROM download_jobs j WHERE j.user_id=u.user_id AND j.created_at>=? AND j.status='completed') AS completed_today, "
            "(SELECT COUNT(*) FROM download_jobs j WHERE j.user_id=u.user_id AND j.created_at>=? AND j.status IN ('queued','running')) AS active_today "
            "FROM users u ORDER BY u.last_seen DESC LIMIT ? OFFSET ?",
            (day_start, day_start, max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        daily_limit = int(item["daily_limit_override"] if item["daily_limit_override"] is not None else global_limit)
        item["effective_daily_limit"] = daily_limit
        item["remaining_today"] = max(0, daily_limit - int(item["completed_today"]) - int(item["active_today"]))
        result.append(item)
    return result
