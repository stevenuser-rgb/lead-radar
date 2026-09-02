import unittest
from unittest.mock import MagicMock, patch

import httpx

import scraper


def _setting(key, default=""):
    values = {
        "apify_api_token": "present-for-test",
        "apify_actor_id": "example~actor",
        "collection_days": "7",
    }
    return values.get(key, default)


class ApifyErrorMessageTests(unittest.TestCase):
    @patch("scraper.get_setting", side_effect=_setting)
    @patch("scraper.httpx.Client")
    def test_connect_timeout_uses_safe_chinese_message(self, client_class, _get_setting):
        client = MagicMock()
        client.post.side_effect = httpx.ConnectTimeout("technical detail")
        client_class.return_value.__enter__.return_value = client

        with self.assertRaisesRegex(scraper.ThreadsSearchError, "Apify API 連線逾時"):
            scraper.fetch_apify_posts("廠房")

    @patch("scraper.get_setting", side_effect=_setting)
    @patch("scraper.httpx.Client")
    def test_read_timeout_uses_safe_chinese_message(self, client_class, _get_setting):
        client = MagicMock()
        client.post.side_effect = httpx.ReadTimeout("technical detail")
        client_class.return_value.__enter__.return_value = client

        with self.assertRaisesRegex(scraper.ThreadsSearchError, "Apify API 回應逾時"):
            scraper.fetch_apify_posts("廠房")


if __name__ == "__main__":
    unittest.main()
