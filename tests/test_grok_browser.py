import unittest
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import register_grok
import register_grok_http


class GrokBrowserTests(unittest.TestCase):
    def test_stealth_avoids_global_runtime_monkeypatches(self):
        script = register_grok.GROK_STEALTH_JS
        self.assertNotIn("Object.defineProperty =", script)
        self.assertNotIn("Error.prepareStackTrace", script)
        self.assertNotIn("HTMLIFrameElement.prototype", script)

    def test_browser_starts_at_xai_signup(self):
        self.assertEqual(
            register_grok.GROK_SIGNUP_URL,
            "https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F",
        )

    def test_polish_signup_and_cookie_labels_are_supported(self):
        self.assertIn(
            "Zarejestruj się za pomocą e-maila", register_grok.EMAIL_SIGNUP_BTN
        )
        self.assertIn("Odrzucenie wszystkich", register_grok.COOKIE_DISMISS)

    def test_spanish_signup_and_cookie_labels_are_supported(self):
        self.assertIn(
            "Regístrate con correo electrónico", register_grok.EMAIL_SIGNUP_BTN
        )
        self.assertIn("Rechazarlas todas", register_grok.COOKIE_DISMISS)

    def test_browser_uses_modern_native_fingerprint(self):
        fingerprint = register_grok.grok_browser_fingerprint()
        self.assertEqual(fingerprint["coreVersion"], "146")
        source = inspect.getsource(register_grok.register_one)
        self.assertNotIn("await inject_grok_stealth", source)
        self.assertIn("await arm_turnstile_hook", source)

    def test_yescaptcha_turnstile_solver_is_available(self):
        fake_solver = MagicMock()
        fake_solver.solve_turnstile.return_value = "token"
        with patch.object(register_grok, "YESCAPTCHA_API_KEY", "key"):
            with patch("xconsole_client.solver.YesCaptchaSolver", return_value=fake_solver):
                token = register_grok._solve_turnstile_yescaptcha(
                    "0x4-test", "https://accounts.x.ai/sign-up"
                )
        self.assertEqual(token, "token")
        self.assertTrue(fake_solver.solve_turnstile.call_args.kwargs["premium"])

    def test_protocol_flow_prefers_yescaptcha(self):
        fake_solver = MagicMock()
        fake_solver.solve_turnstile.return_value = "protocol-token"
        with patch.object(register_grok_http, "YESCAPTCHA_API_KEY", "key"):
            with patch("xconsole_client.solver.YesCaptchaSolver", return_value=fake_solver):
                token = register_grok_http.solve_turnstile(
                    "0x4-test", "https://accounts.x.ai/sign-up"
                )
        self.assertEqual(token, "protocol-token")
        self.assertTrue(fake_solver.solve_turnstile.call_args.kwargs["premium"])

    def test_current_compact_xai_code_format_is_accepted(self):
        import re

        self.assertEqual(re.search(register_grok.GROK_CODE_REGEX, "Code Q5137N").group(1), "Q5137N")
        self.assertEqual(re.search(register_grok.GROK_CODE_REGEX, "Code WIF-W23").group(1), "WIF-W23")


class GrokBrowserOAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_failed_import_persists_failed_authorization_status(self):
        with patch.object(register_grok, "IMPORT_SUB2API", True), patch(
            "common.session_export.save_grok_token", return_value=True
        ) as save, patch(
            "common.uploaders.upload_sub2api_grok",
            return_value=(False, "authorization denied"),
        ), patch.object(register_grok.email_pool, "mark_used") as mark_used:
            result = register_grok.save_and_import_grok(
                "sso-token", "failed@example.com", "password"
            )

        self.assertFalse(result)
        self.assertEqual(save.call_args_list[0].kwargs["authorization_status"], "pending")
        self.assertEqual(save.call_args_list[1].kwargs["authorization_status"], "failed")
        mark_used.assert_not_called()

    def test_successful_import_persists_authorized_status(self):
        with patch.object(register_grok, "IMPORT_SUB2API", True), patch(
            "common.session_export.save_grok_token", return_value=True
        ) as save, patch(
            "common.uploaders.upload_sub2api_grok",
            return_value=(True, "imported"),
        ), patch(
            "common.token_upload_state.mark_uploaded"
        ) as mark_uploaded, patch.object(
            register_grok.email_pool, "mark_used"
        ) as mark_used:
            result = register_grok.save_and_import_grok(
                "sso-token", "ok@example.com", "password"
            )

        self.assertTrue(result)
        self.assertEqual(save.call_args_list[1].kwargs["authorization_status"], "authorized")
        mark_uploaded.assert_called_once_with("grok", "sub2api", "ok@example.com")
        mark_used.assert_called_once()

    async def test_http_sso_recovery_uses_existing_account_without_signup(self):
        client = MagicMock()
        client.fetch_sso_token.return_value = "recovered-sso"
        with patch(
            "xconsole_client.XConsoleAuthClient", return_value=client
        ) as factory, patch.object(
            register_grok.proxy_switch,
            "effective_proxy_url",
            return_value="http://127.0.0.1:7897",
        ):
            result = await register_grok.recover_grok_sso_without_cookie(
                "grok@example.com", "password"
            )

        self.assertEqual(result, "recovered-sso")
        factory.assert_called_once()
        client.visit_home.assert_called_once()
        client.load_signup_page.assert_not_called()
        client.close.assert_called_once()

    async def test_webui_task_skips_browser_device_flow(self):
        with patch.object(register_grok, "IMPORT_SUB2API", True), patch.dict(
            register_grok.os.environ, {"REG_FACTORY_WEBUI_TASK": "1"}, clear=False
        ), patch("common.grok_oauth.start_grok_device_flow") as start:
            result = await register_grok.acquire_browser_grok_oauth(
                MagicMock(), "sso-token", "webui@example.com"
            )

        self.assertIsNone(result)
        start.assert_not_called()

    async def test_risk_denied_account_does_not_start_device_flow(self):
        state = {
            "denied": True,
            "bot_flag_details": "policy=deny,risk=1.00,event=$registration",
        }
        with patch.object(register_grok, "IMPORT_SUB2API", True), patch(
            "common.grok_oauth.inspect_grok_account_state", return_value=state
        ), patch("common.grok_oauth.start_grok_device_flow") as start:
            result = await register_grok.acquire_browser_grok_oauth(
                MagicMock(), "sso-token", "denied@example.com"
            )

        self.assertIsNone(result)
        start.assert_not_called()

    async def test_device_flow_returns_refreshable_credentials(self):
        page = MagicMock()
        page.url = "https://accounts.x.ai/oauth2/device"
        page.goto = AsyncMock()
        credentials = {
            "access_token": "access",
            "refresh_token": "refresh",
            "email": "new@example.com",
        }
        device = {
            "verification_url": "https://accounts.x.ai/oauth2/device?user_code=ABCD",
            "device_code": "device-code",
            "interval": 2,
        }
        with patch.object(register_grok, "IMPORT_SUB2API", True), patch.object(
            register_grok.proxy_switch,
            "effective_proxy_url",
            return_value="http://127.0.0.1:7897",
        ), patch(
            "common.grok_oauth.inspect_grok_account_state",
            return_value={"denied": False},
        ), patch(
            "common.grok_oauth.start_grok_device_flow", return_value=device
        ), patch(
            "common.grok_oauth.finish_grok_device_flow",
            return_value=(credentials, "new@example.com"),
        ), patch.object(
            register_grok, "click_any", new=AsyncMock(return_value="Allow")
        ):
            result = await register_grok.acquire_browser_grok_oauth(
                page, "sso-token", "new@example.com"
            )

        self.assertEqual(result, credentials)
        page.goto.assert_awaited_once_with(
            device["verification_url"], timeout=45000, wait_until="domcontentloaded"
        )


if __name__ == "__main__":
    unittest.main()
