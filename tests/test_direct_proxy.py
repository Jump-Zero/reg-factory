import os
import tempfile
import unittest
from unittest.mock import patch

from bitbrowser import BitBrowser
from common.bundled_browser import BundledBrowser
from common import direct_proxy
from common import proxy_switch
import register_outlook_standalone


class DirectProxyTests(unittest.TestCase):
    def test_custom_chrome_uses_local_browser_adapter(self):
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "custom"}, clear=False):
            browser = BitBrowser()
        self.assertIsInstance(browser, BundledBrowser)

    def test_legacy_cdp_flow_falls_back_to_local_chromium_for_ruyipage(self):
        with patch.dict(os.environ, {"FINGERPRINT_BROWSER": "ruyipage"}, clear=False):
            browser = BitBrowser()
        self.assertIsInstance(browser, BundledBrowser)

    def test_custom_chrome_adapter_supports_legacy_outlook_api(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "FINGERPRINT_BROWSER": "custom",
                "REG_FACTORY_DATA_DIR": directory,
            }
            with patch.dict(os.environ, env, clear=False):
                browser = register_outlook_standalone.BitBrowserClient()
                created = browser._post("/browser/update", {"name": "outlook-custom"})
                profile_id = created["data"]["id"]
                listed = browser._post("/browser/list", {"page": 0, "pageSize": 10})
                browser._post("/browser/update", {"id": profile_id, "name": "updated"})
                deleted = browser._post("/browser/delete", {"id": profile_id})

        self.assertIsInstance(browser, BundledBrowser)
        self.assertEqual(listed["data"]["list"][0]["id"], profile_id)
        self.assertTrue(deleted["success"])

    def test_parse_proxy_preserves_authenticated_url(self):
        proxy = direct_proxy.parse_proxy("socks5://user:pass@proxy.test:1080")
        self.assertEqual(proxy.server, "socks5://proxy.test:1080")
        self.assertEqual(proxy.url, "socks5://user:pass@proxy.test:1080")

    def test_pool_rotation_persists_active_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "REG_FACTORY_PROXY_POOL": "http://one.test:8001,http://two.test:8002",
                "REG_FACTORY_PROXY_STATE_FILE": os.path.join(directory, "index.txt"),
            }
            self.assertEqual(direct_proxy.configured_proxy(environ=env).host, "one.test")
            self.assertEqual(direct_proxy.rotate_proxy_pool(environ=env).host, "two.test")
            self.assertEqual(direct_proxy.configured_proxy(environ=env).host, "two.test")
            self.assertEqual(direct_proxy.rotate_proxy_pool(environ=env).host, "one.test")

    def test_pool_takes_precedence_over_single_proxy(self):
        env = {
            "REG_FACTORY_PROXY": "http://single.test:8080",
            "REG_FACTORY_PROXY_POOL": "http://pool.test:8081",
        }
        self.assertEqual(direct_proxy.configured_proxy(environ=env).host, "pool.test")

    def test_bitbrowser_create_payload_receives_residential_proxy(self):
        env = {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "socks5://user:pass@proxy.test:1080",
            "FINGERPRINT_BROWSER": "bitbrowser",
        }
        with patch.dict(os.environ, env, clear=True):
            browser = BitBrowser(api_base="http://127.0.0.1:54345")
            with patch.object(browser, "_post", return_value={"data": {"id": "profile-1"}}) as post:
                profile_id = browser.create_browser(
                    name="residential-test",
                    **proxy_switch.browser_proxy_fields(),
                )
        self.assertEqual(profile_id, "profile-1")
        payload = post.call_args.args[1]
        self.assertEqual(payload["proxyType"], "socks5")
        self.assertEqual(payload["host"], "proxy.test")
        self.assertEqual(payload["port"], "1080")
        self.assertEqual(payload["proxyUserName"], "user")
        self.assertEqual(payload["proxyPassword"], "pass")

    def test_bitbrowser_partially_updates_fingerprint(self):
        browser = BitBrowser(api_base="http://127.0.0.1:54345")
        with patch.object(browser, "_post", return_value={"success": True}) as post:
            browser.update_browser_fingerprint("profile-1", coreVersion="130")

        post.assert_called_once_with(
            "/browser/update/partial",
            {
                "ids": ["profile-1"],
                "browserFingerPrint": {"coreVersion": "130"},
            },
        )


if __name__ == "__main__":
    unittest.main()
