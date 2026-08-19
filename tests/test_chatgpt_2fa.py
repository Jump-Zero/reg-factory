import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common import chatgpt_2fa


class ChatGPTTwoFactorTests(unittest.TestCase):
    def test_enable_totp_reauthenticates_enrolls_and_activates(self):
        calls = []

        async def evaluate(_script, payload=None):
            if payload is None:
                return "en-US"
            calls.append(payload)
            url = payload["url"]
            if url.startswith("/api/auth/csrf"):
                data = {"csrfToken": "csrf-token"}
            elif url.startswith("/api/auth/signin/openai"):
                data = {"url": "https://auth.openai.com/email-verification"}
            elif url == "/api/accounts/email-otp/validate":
                data = {"continue_url": "https://chatgpt.com/?action=enable&factor=totp"}
            elif url == "/backend-api/accounts/mfa/enroll":
                data = {"secret": "JBSWY3DPEHPK3PXP", "session_id": "mfa-session"}
            elif url == "/backend-api/accounts/mfa/user/activate_enrollment":
                data = {"success": True}
            else:
                raise AssertionError(f"unexpected request: {url}")
            return {"ok": True, "status": 200, "data": data, "text": ""}

        async def goto(url, **_kwargs):
            page.url = url

        page = MagicMock()
        page.url = "https://chatgpt.com/"
        page.evaluate = AsyncMock(side_effect=evaluate)
        page.goto = AsyncMock(side_effect=goto)
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[{
            "name": "oai-did",
            "value": "device-id",
        }])
        fetch_code = AsyncMock(return_value="123456")
        session = {"accessToken": "access-token", "user": {"email": "user@example.com"}}

        with (
            patch.object(chatgpt_2fa, "fetch_chatgpt_session", AsyncMock(return_value=session)),
            patch.object(chatgpt_2fa, "_totp_code", return_value="654321"),
            patch.object(chatgpt_2fa.asyncio, "sleep", AsyncMock()),
        ):
            secret, refreshed = asyncio.run(
                chatgpt_2fa.enable_chatgpt_totp(
                    page,
                    context,
                    "user@example.com",
                    fetch_code,
                )
            )

        self.assertEqual(secret, "JBSWY3DPEHPK3PXP")
        self.assertIs(refreshed, session)
        fetch_code.assert_awaited_once()
        self.assertTrue(fetch_code.await_args.kwargs["allow_browser_fallback"])
        self.assertEqual(
            [call["url"].split("?", 1)[0] for call in calls],
            [
                "/api/auth/csrf",
                "/api/auth/signin/openai",
                "/api/accounts/email-otp/validate",
                "/backend-api/accounts/mfa/enroll",
                "/backend-api/accounts/mfa/user/activate_enrollment",
            ],
        )
        self.assertIn('"code":"123456"', calls[2]["body"])
        self.assertIn('"code":"654321"', calls[4]["body"])

    def test_error_formatter_does_not_include_enrollment_secret(self):
        error = chatgpt_2fa._error_message(
            {
                "status": 400,
                "data": {"secret": "DO-NOT-PRINT", "message": "invalid request"},
            },
            "TOTP enrollment",
        )

        self.assertIn("invalid request", str(error))
        self.assertNotIn("DO-NOT-PRINT", str(error))


if __name__ == "__main__":
    unittest.main()
