import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common.ruyipage_browser import (
    RuyiContext,
    RuyiLocator,
    RuyiPageAdapter,
    RuyiPageBrowser,
    _selector_parts,
)


class RuyiPageBrowserTests(unittest.TestCase):
    def test_playwright_selector_extensions_are_normalized(self):
        selector, texts, visible = _selector_parts(
            'button:has(span:has-text("@")):visible'
        )

        self.assertEqual(selector, "button:has(span)")
        self.assertEqual(texts, ["@"])
        self.assertTrue(visible)

    def test_page_evaluate_executes_arrow_function(self):
        async def exercise():
            raw = MagicMock()
            raw.get_url = AsyncMock(return_value="about:blank")
            raw.run_js = AsyncMock(return_value="ok")
            page = RuyiPageAdapter(raw)
            try:
                result = await page.evaluate("() => document.title")
            finally:
                await page._stop_watcher()
            return result, raw

        result, raw = asyncio.run(exercise())

        self.assertEqual(result, "ok")
        raw.run_js.assert_awaited_once_with(
            "() => document.title", as_expr=False
        )

    def test_element_evaluate_passes_element_as_first_argument(self):
        element = MagicMock()
        element.run_js = AsyncMock(return_value="outer")
        locator = RuyiLocator(MagicMock(), "button")
        locator._one = AsyncMock(return_value=element)

        result = asyncio.run(locator.evaluate("node => node.outerHTML"))

        self.assertEqual(result, "outer")
        wrapper = element.run_js.await_args.args[0]
        self.assertIn("(node => node.outerHTML)(this", wrapper)

    def test_context_normalizes_playwright_cookie_fields_for_bidi(self):
        async def exercise():
            root = MagicMock()
            root._ruyi.set_cookies = AsyncMock()
            context = RuyiContext(root)
            await context.add_cookies(
                [{
                    "name": "session",
                    "value": "value",
                    "domain": ".example.com",
                    "sameSite": "Lax",
                    "expires": 1234.5,
                }]
            )
            return root

        root = asyncio.run(exercise())
        cookie = root._ruyi.set_cookies.await_args.args[0][0]
        self.assertEqual(cookie["sameSite"], "lax")
        self.assertEqual(cookie["expiry"], 1234)
        self.assertNotIn("expires", cookie)

    def test_goto_enforces_hard_timeout_and_stops_loading(self):
        async def exercise():
            raw = MagicMock()
            raw.get_url = AsyncMock(return_value="https://example.com/loading")
            async def hang(*args, **kwargs):
                await asyncio.sleep(60)

            raw.get = AsyncMock(side_effect=hang)
            raw.stop_loading = AsyncMock()
            page = RuyiPageAdapter(raw)
            try:
                with self.assertRaises(TimeoutError):
                    await page.goto("https://example.com", timeout=1)
                raw.stop_loading.assert_awaited_once()
            finally:
                await page._stop_watcher()

        asyncio.run(exercise())

    def test_explicit_noproxy_profile_does_not_inherit_global_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"REG_FACTORY_DATA_DIR": directory}, clear=False
            ):
                browser = RuyiPageBrowser()
                profile_id = browser.create_browser(
                    "direct", proxyType="noproxy"
                )

                self.assertEqual(browser.profiles[profile_id]["proxy"], "")
                browser.delete_browser(profile_id)


if __name__ == "__main__":
    unittest.main()
