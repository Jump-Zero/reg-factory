import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common import traffic_saver


class _Context:
    def __init__(self):
        self.pattern = None
        self.handler = None

    async def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


class _Route:
    def __init__(self, url, resource_type, headers=None):
        self.request = SimpleNamespace(
            url=url,
            resource_type=resource_type,
            headers=headers or {},
        )
        self.action = None

    async def abort(self):
        self.action = "abort"

    async def continue_(self):
        self.action = "continue"


class TrafficSaverTests(unittest.TestCase):
    def test_mode_only_applies_to_residential_egress(self):
        self.assertEqual(
            traffic_saver.configured_mode({
                "PROXY_MODE": "residential",
                "REG_FACTORY_PROXY": "http://proxy.test:9000",
            }),
            "balanced",
        )
        self.assertEqual(
            traffic_saver.configured_mode({"PROXY_MODE": "clash_fixed"}),
            "off",
        )
        self.assertEqual(
            traffic_saver.configured_mode({
                "PROXY_MODE": "residential",
                "REG_FACTORY_PROXY": "http://proxy.test:9000",
                "REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE": "extreme",
            }),
            "extreme",
        )

    def test_balanced_blocks_heavy_assets_but_keeps_scripts_and_challenges(self):
        self.assertTrue(traffic_saver.should_block("https://cdn.example/app.webp", "image", "balanced"))
        self.assertTrue(traffic_saver.should_block("https://cdn.example/font.woff2", "font", "balanced"))
        self.assertFalse(traffic_saver.should_block("https://cdn.example/app.js", "script", "balanced"))
        self.assertFalse(traffic_saver.should_block("https://newassets.hcaptcha.com/captcha.png", "image", "balanced"))
        self.assertFalse(traffic_saver.should_block("https://client-api.arkoselabs.com/image", "image", "balanced"))
        self.assertFalse(traffic_saver.should_block("https://fpt.live.com/pixel.png", "image", "balanced"))

    def test_aggressive_adds_stylesheets_and_optional_telemetry(self):
        self.assertFalse(traffic_saver.should_block("https://cdn.example/app.css", "stylesheet", "balanced"))
        self.assertTrue(traffic_saver.should_block("https://cdn.example/app.css", "stylesheet", "aggressive"))
        self.assertTrue(traffic_saver.should_block("https://www.google-analytics.com/collect", "fetch", "aggressive"))

    def test_aggressive_keeps_microsoft_authorization_styles(self):
        self.assertFalse(traffic_saver.should_block(
            "https://login.microsoftonline.com/common/oauth.css",
            "stylesheet",
            "aggressive",
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://signup.live.com/client/signup.css",
            "stylesheet",
            "aggressive",
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://logincdn.msauth.net/shared/oauth.css",
            "stylesheet",
            "aggressive",
        ))
        self.assertTrue(traffic_saver.should_block(
            "https://signup.live.com/client/hero.webp",
            "image",
            "aggressive",
        ))

    def test_extreme_blocks_optional_requests_but_keeps_challenges_and_auth_css(self):
        self.assertTrue(traffic_saver.should_block(
            "https://cdn.example/app.webmanifest", "manifest", "extreme"
        ))
        self.assertTrue(traffic_saver.should_block(
            "https://cdn.example/app.js.map", "fetch", "extreme"
        ))
        self.assertTrue(traffic_saver.should_block(
            "https://cdn.example/next", "fetch", "extreme", {"Sec-Purpose": "prefetch"}
        ))
        self.assertTrue(traffic_saver.should_block(
            "https://api.mixpanel.com/track", "fetch", "extreme"
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://newassets.hcaptcha.com/captcha.png", "image", "extreme"
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://login.microsoftonline.com/common/oauth.css", "stylesheet", "extreme"
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://claude.ai/_next/static/app.css", "stylesheet", "extreme"
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://auth.openai.com/assets/login.css", "stylesheet", "extreme"
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://accounts.x.ai/_next/static/signup.css", "stylesheet", "extreme"
        ))
        self.assertFalse(traffic_saver.should_block(
            "https://github.githubassets.com/assets/signup.css", "stylesheet", "extreme"
        ))
        self.assertTrue(traffic_saver.should_block(
            "https://claude.ai/_next/static/hero.webp", "image", "extreme"
        ))

    def test_extreme_bitbrowser_settings_only_apply_to_residential_mode(self):
        residential = {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "http://proxy.test:9000",
            "REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE": "extreme",
        }
        self.assertEqual(
            traffic_saver.bitbrowser_profile_defaults(residential),
            {},
        )
        payload = traffic_saver.bitbrowser_open_payload("profile-1", residential)
        self.assertEqual(payload["id"], "profile-1")
        self.assertIn("--disable-background-networking", payload["args"])
        self.assertEqual(
            traffic_saver.bitbrowser_open_payload(
                "profile-1", {**residential, "PROXY_MODE": "clash_fixed"}
            ),
            {"id": "profile-1"},
        )

    def test_install_routes_requests_using_configured_mode(self):
        context = _Context()
        mode = asyncio.run(traffic_saver.install(context, {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "http://proxy.test:9000",
            "REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE": "balanced",
        }))
        self.assertEqual(mode, "balanced")
        self.assertEqual(context.pattern, "**/*")

        image = _Route("https://cdn.example/hero.jpg", "image")
        script = _Route("https://cdn.example/app.js", "script")
        asyncio.run(context.handler(image))
        asyncio.run(context.handler(script))
        self.assertEqual(image.action, "abort")
        self.assertEqual(script.action, "continue")
        self.assertEqual(traffic_saver.stats(context), {"image": 1})

        self.assertTrue(traffic_saver.set_bypass(context))
        bypassed_image = _Route("https://cdn.example/second.jpg", "image")
        asyncio.run(context.handler(bypassed_image))
        self.assertEqual(bypassed_image.action, "continue")
        self.assertEqual(traffic_saver.stats(context), {"image": 1})

    def test_summary_identifies_platform_mode_even_when_nothing_was_blocked(self):
        context = _Context()
        environment = {
            "REG_FACTORY_PLATFORM": "grok",
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "http://proxy.test:9000",
            "REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE": "extreme",
        }
        with patch("builtins.print") as output:
            asyncio.run(traffic_saver.install(context, environment))
            traffic_saver.log_summary(context)

        messages = [str(call.args[0]) for call in output.call_args_list]
        self.assertTrue(any("platform=grok" in message for message in messages))
        self.assertTrue(any("mode=extreme blocked=0" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
