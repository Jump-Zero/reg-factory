import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from common import browser, browser_registry


class BrowserRegistryTests(unittest.TestCase):
    def test_register_and_unregister_are_cross_process_safe_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "active.json"
            with patch.object(browser_registry, "_path", return_value=path), \
                    patch.dict(os.environ, {"REG_FACTORY_RUN_ID": "test-run"}, clear=False):
                browser_registry.register("profile-1", name="chatgpt_test", api_base="http://127.0.0.1:54345")
                self.assertEqual(browser_registry.active_profiles(owner="test-run")[0]["name"], "chatgpt_test")
                browser_registry.unregister("profile-1")
                self.assertEqual(browser_registry.active_profiles(), [])

    def test_open_and_connect_deletes_profile_when_cdp_connect_fails(self):
        client = MagicMock()
        client.open_browser.return_value = {"ws": "ws://fixture"}
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(
                connect_over_cdp=AsyncMock(side_effect=RuntimeError("cdp failed"))
            )
        )
        with patch.object(browser, "BitBrowser", return_value=client), \
                patch.object(browser, "create_browser_with_retry", return_value="profile-1"):
            with self.assertRaisesRegex(RuntimeError, "cdp failed"):
                asyncio.run(browser.open_and_connect("fixture", p=playwright))
        client.close_browser.assert_called_once_with("profile-1")
        client.delete_browser.assert_called_once_with("profile-1")

    def test_open_and_connect_retries_transient_bitbrowser_opening_state(self):
        client = MagicMock()
        client.open_browser.side_effect = [
            Exception("浏览器正在打开中"),
            {"ws": "ws://fixture"},
        ]
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(
                connect_over_cdp=AsyncMock(side_effect=RuntimeError("cdp failed"))
            )
        )
        with patch.object(browser, "BitBrowser", return_value=client), patch.object(
            browser, "create_browser_with_retry", return_value="profile-1"
        ), patch.object(browser.asyncio, "sleep", AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "cdp failed"):
                asyncio.run(browser.open_and_connect("fixture", p=playwright))

        self.assertEqual(client.open_browser.call_count, 2)
        client.delete_browser.assert_called_once_with("profile-1")

    def test_teardown_deletes_profile_when_async_close_is_cancelled(self):
        client = MagicMock()
        client.close_browser_async = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(browser.teardown(client, "profile-2"))
        client.delete_browser.assert_called_once_with("profile-2")


if __name__ == "__main__":
    unittest.main()
