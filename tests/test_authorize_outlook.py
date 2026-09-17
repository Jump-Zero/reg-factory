import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tools.authorize_outlook as authorize_outlook


class AuthorizeOutlookTests(unittest.TestCase):
    def test_dedicated_proxy_urls_are_one_to_one(self):
        pool = [
            SimpleNamespace(url="http://one.test:8001"),
            SimpleNamespace(url="http://two.test:8002"),
            SimpleNamespace(url="http://two.test:8002"),
        ]
        with patch("common.direct_proxy.proxy_pool", return_value=pool):
            self.assertEqual(
                authorize_outlook.dedicated_proxy_urls(2),
                ["http://one.test:8001", "http://two.test:8002"],
            )

    def test_dedicated_proxy_urls_reject_an_insufficient_pool(self):
        with patch(
            "common.direct_proxy.proxy_pool",
            return_value=[SimpleNamespace(url="http://one.test:8001")],
        ):
            with self.assertRaisesRegex(ValueError, "requires at least 2"):
                authorize_outlook.dedicated_proxy_urls(2)

    def test_load_accounts_accepts_email_password_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.txt"
            path.write_text(
                "User@Outlook.com----secret\n"
                "user@outlook.com----duplicate\n"
                "# comment\n",
                encoding="utf-8",
            )
            self.assertEqual(
                authorize_outlook.load_accounts(str(path)),
                [("user@outlook.com", "secret")],
            )

    def test_load_accounts_rejects_missing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.txt"
            path.write_text("user@outlook.com\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "email----password"):
                authorize_outlook.load_accounts(str(path))

    def test_load_accounts_accepts_five_dash_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.txt"
            path.write_text("user@outlook.com-----secret\n", encoding="utf-8")
            self.assertEqual(authorize_outlook.load_accounts(str(path)), [("user@outlook.com", "secret")])

    @patch("tools.extract_graph_tokens.get_graph_token")
    def test_authorize_one_returns_only_safe_result_fields(self, get_graph_token):
        get_graph_token.return_value = {
            "email": "user@outlook.com",
            "password": "secret",
            "refresh_token": "refresh",
            "client_id": "client",
            "access_token": "access",
        }
        result = authorize_outlook.authorize_one(("user@outlook.com", "secret"), 1)
        self.assertEqual(result, {
            "email": "user@outlook.com",
            "password": "secret",
            "refresh_token": "refresh",
            "client_id": "client",
        })
        get_graph_token.assert_called_once_with("user@outlook.com", "secret", 1)

    @patch("tools.extract_graph_tokens.get_graph_token")
    def test_http_failure_records_account_without_starting_browser(self, get_graph_token):
        get_graph_token.return_value = None
        with tempfile.TemporaryDirectory() as directory, patch(
            "tools.authorize_outlook._authorize_browser"
        ) as browser_authorize:
            result = authorize_outlook.authorize_one(
                ("user@outlook.com", "secret"),
                2,
                "http://proxy.test:9000",
                method="http",
                environ={"REG_FACTORY_DATA_DIR": directory},
            )

            self.assertIsNone(result)
            pending = Path(directory) / "outlook_no_graph.txt"
            self.assertEqual(pending.read_text(encoding="utf-8"), "user@outlook.com----secret\n")
            browser_authorize.assert_not_called()
        get_graph_token.assert_called_once_with(
            "user@outlook.com", "secret", 2, proxy="http://proxy.test:9000"
        )

    @patch("tools.authorize_outlook._authorize_browser")
    @patch("tools.extract_graph_tokens.get_graph_token")
    def test_browser_method_is_explicit_and_does_not_call_http(
        self, get_graph_token, browser_authorize
    ):
        get_graph_token.return_value = {
            "refresh_token": "should-not-be-used",
            "client_id": "http-client",
        }
        browser_authorize.return_value = {
            "refresh_token": "browser-refresh",
            "client_id": "browser-client",
        }

        result = authorize_outlook.authorize_one(
            ("user@outlook.com", "secret"),
            3,
            method="browser",
        )

        self.assertEqual(result["refresh_token"], "browser-refresh")
        get_graph_token.assert_not_called()
        browser_authorize.assert_called_once_with(
            ("user@outlook.com", "secret"), 3, ""
        )


if __name__ == "__main__":
    unittest.main()
