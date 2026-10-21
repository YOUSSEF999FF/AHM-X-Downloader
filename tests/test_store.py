from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = store.DB_PATH
        store.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        store.init_db()
        store.touch_user(12345, "tester", "Test User")

    def tearDown(self) -> None:
        store.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_daily_limit_and_usage(self) -> None:
        store.set_settings({"daily_limit": 2})
        store.create_download_job("j1", 12345, "youtube", "one", "youtube.com", "720")
        store.create_download_job("j2", 12345, "facebook", "two", "facebook.com", "480")
        usage = store.get_usage(12345)
        self.assertEqual(usage["active_today"], 2)
        self.assertEqual(usage["remaining_today"], 0)
        with self.assertRaises(ValueError):
            store.create_download_job("j3", 12345, "youtube", "three", "youtube.com", "360")

    def test_paginated_history(self) -> None:
        store.create_download_job("h1", 12345, "youtube", "one", "youtube.com", "720")
        store.create_download_job("h2", 12345, "youtube", "two", "youtube.com", "480")
        newest = store.get_history(12345, limit=1, offset=0)
        next_page = store.get_history(12345, limit=1, offset=1)
        self.assertEqual(len(newest), 1)
        self.assertEqual(len(next_page), 1)
        self.assertNotEqual(newest[0]["job_id"], next_page[0]["job_id"])

    def test_recover_interrupted_jobs(self) -> None:
        store.create_download_job("recover1", 12345, "youtube", "interrupted", "youtube.com", "720")
        store.update_job("recover1", status="running", phase="downloading")
        count = store.recover_interrupted_jobs()
        self.assertEqual(count, 1)
        job = store.get_job("recover1")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_id"], "SERVICE_RESTARTED")

    def test_paginated_user_management(self) -> None:
        store.touch_user(22222, "second", "Second User")
        newest = store.list_users(limit=1, offset=0)
        next_page = store.list_users(limit=1, offset=1)
        self.assertEqual(len(newest), 1)
        self.assertEqual(len(next_page), 1)
        self.assertNotEqual(newest[0]["user_id"], next_page[0]["user_id"])

    def test_block_and_individual_quota(self) -> None:
        store.update_user_access(12345, blocked=True, daily_limit_override=3)
        self.assertTrue(store.is_blocked(12345))
        self.assertEqual(store.get_usage(12345)["daily_limit"], 3)
        store.update_user_access(12345, blocked=False, daily_limit_override=None)
        self.assertFalse(store.is_blocked(12345))
        self.assertEqual(store.get_usage(12345)["daily_limit"], store.get_settings()["daily_limit"])


if __name__ == "__main__":
    unittest.main()
