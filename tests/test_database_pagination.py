import os
import tempfile
import unittest

import database


class SkippedLogPaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "radar.db")
        database.init_db()
        conn = database.get_db()
        conn.executemany(
            """
            INSERT INTO skipped_logs
                (post_id, keyword, author, content, post_url, ai_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (f"post-{index}", "廠房", "測試", f"內容 {index}", f"https://example.com/{index}", "略過")
                for index in range(23)
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_returns_requested_page_and_total(self):
        result = database.get_skipped_log_page(page=2, page_size=10)

        self.assertEqual(result["page"], 2)
        self.assertEqual(result["pages"], 3)
        self.assertEqual(result["total"], 23)
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["items"][0]["post_id"], "post-12")

    def test_clamps_page_past_last_page(self):
        result = database.get_skipped_log_page(page=99, page_size=10)

        self.assertEqual(result["page"], 3)
        self.assertEqual(len(result["items"]), 3)


if __name__ == "__main__":
    unittest.main()
