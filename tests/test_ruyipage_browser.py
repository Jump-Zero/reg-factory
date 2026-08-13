import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common.ruyipage_browser import (
    RuyiContext,
    RuyiLocator,
    RuyiMouse,
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

    def test_fill_uses_native_keyboard_input_and_accepts_timeout(self):
        element = MagicMock()
        element.input = AsyncMock()
        locator = RuyiLocator(MagicMock(), "input")
        locator._one = AsyncMock(return_value=element)

        asyncio.run(locator.fill("user@outlook.com", timeout=15000))

        locator._one.assert_awaited_once_with(timeout=15)
        element.input.assert_awaited_once_with(
            "user@outlook.com", clear=True, by_js=False
        )

    def test_evaluate_accepts_playwright_timeout_keyword(self):
        element = MagicMock()
        element.run_js = AsyncMock(return_value={"invalid": False})
        locator = RuyiLocator(MagicMock(), "input")
        locator._one = AsyncMock(return_value=element)

        result = asyncio.run(
            locator.evaluate("el => ({invalid: !el.value})", timeout=1000)
        )

        self.assertEqual(result, {"invalid": False})
        locator._one.assert_awaited_once_with(timeout=1)

    def test_select_option_supports_value_label_and_index(self):
        element = MagicMock()
        element.run_js = AsyncMock(side_effect=["outlook.com", "outlook.jp", "US"])
        locator = RuyiLocator(MagicMock(), "select")
        locator._one = AsyncMock(return_value=element)

        async def exercise():
            by_value = await locator.select_option("outlook.com")
            by_label = await locator.select_option({"label": "@outlook.jp"})
            by_index = await locator.select_option(index=1)
            return by_value, by_label, by_index

        self.assertEqual(
            asyncio.run(exercise()),
            (["outlook.com"], ["outlook.jp"], ["US"]),
        )

    def test_child_locator_evaluate_all_queries_parent_element(self):
        element = MagicMock()
        element.run_js = AsyncMock(return_value=["outlook.com", "@outlook.com"])
        parent = RuyiLocator(MagicMock(), "select")
        parent._one = AsyncMock(return_value=element)

        result = asyncio.run(
            parent.locator("option").evaluate_all(
                "options => options.flatMap(option => [option.value, option.textContent])"
            )
        )

        self.assertEqual(result, ["outlook.com", "@outlook.com"])
        self.assertIn('querySelectorAll("option")', element.run_js.await_args.args[0])

    def test_page_refreshes_playwright_style_frames(self):
        async def exercise():
            frame = MagicMock()
            frame.get_url = AsyncMock(return_value="https://captcha.hsprotect.net/hold")
            raw = MagicMock()
            raw.get_url = AsyncMock(return_value="https://signup.live.com/")
            raw.get_frames = AsyncMock(return_value=[frame])
            raw.eles = AsyncMock(return_value=[])
            page = RuyiPageAdapter(raw)
            try:
                await page._refresh_frames(force=True)
                return page, page.frames[1]
            finally:
                await page._stop_watcher()

        page, frame = asyncio.run(exercise())

        self.assertIs(page.frames[0], page.main_frame)
        self.assertEqual(frame.url, "https://captcha.hsprotect.net/hold")
        self.assertIsInstance(frame.locator("#px-captcha"), RuyiLocator)

    def test_page_matches_frame_offset_by_url_instead_of_dom_order(self):
        def raw_element(src, x):
            element = MagicMock()
            element.attr = AsyncMock(return_value=src)
            element.get_location = AsyncMock(return_value={"x": x, "y": 100})
            element.get_size = AsyncMock(return_value={"width": 300, "height": 180})
            return element

        async def exercise():
            challenge = MagicMock()
            challenge.get_url = AsyncMock(
                return_value="https://captcha.hsprotect.net/challenge"
            )
            telemetry = MagicMock()
            telemetry.get_url = AsyncMock(
                return_value="https://telemetry.microsoft.com/frame"
            )
            raw = MagicMock()
            raw.get_url = AsyncMock(return_value="https://signup.live.com/")
            raw.get_frames = AsyncMock(return_value=[challenge, telemetry])
            raw.run_js = AsyncMock(return_value={"width": 1414, "height": 792})
            raw.eles = AsyncMock(return_value=[
                raw_element("https://telemetry.microsoft.com/frame", -9999),
                raw_element("https://captcha.hsprotect.net/challenge", 420),
            ])
            page = RuyiPageAdapter(raw)
            try:
                await page._refresh_frames(force=True)
                return page.frames[1]._frame_offset, page.frames[2]._frame_offset
            finally:
                await page._stop_watcher()

        challenge_offset, telemetry_offset = asyncio.run(exercise())

        self.assertEqual(challenge_offset, {"x": 420.0, "y": 100.0})
        self.assertEqual(telemetry_offset, {"x": -9999.0, "y": 100.0})

    def test_mouse_supports_move_hold_and_release(self):
        async def exercise():
            page = MagicMock()
            move_chain = MagicMock(perform=AsyncMock())
            hold_chain = MagicMock(perform=AsyncMock())
            release_chain = MagicMock(perform=AsyncMock())
            page._ruyi.actions.move_to = AsyncMock(return_value=move_chain)
            page._ruyi.actions.hold = AsyncMock(return_value=hold_chain)
            page._ruyi.actions.release = AsyncMock(return_value=release_chain)
            mouse = RuyiMouse(page)
            await mouse.move(120.7, 250.2, steps=4)
            await mouse.down()
            await mouse.up()
            return page, move_chain, hold_chain, release_chain

        page, move_chain, hold_chain, release_chain = asyncio.run(exercise())

        page._ruyi.actions.move_to.assert_awaited_once_with((120, 250), duration=48)
        page._ruyi.actions.hold.assert_awaited_once_with(button=0)
        page._ruyi.actions.release.assert_awaited_once_with(button=0)
        move_chain.perform.assert_awaited_once()
        hold_chain.perform.assert_awaited_once()
        release_chain.perform.assert_awaited_once()

    def test_screenshot_accepts_playwright_timeout(self):
        async def exercise(path):
            raw = MagicMock()
            raw.get_url = AsyncMock(return_value="about:blank")
            raw.get_frames = AsyncMock(return_value=[])
            raw.eles = AsyncMock(return_value=[])
            raw.screenshot = AsyncMock(return_value=True)
            page = RuyiPageAdapter(raw)
            try:
                result = await page.screenshot(path=path, timeout=5000)
                return result, raw
            finally:
                await page._stop_watcher()

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "error.png")
            result, raw = asyncio.run(exercise(path))

        self.assertTrue(result)
        raw.screenshot.assert_awaited_once_with(path=path, full_page=False)

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
