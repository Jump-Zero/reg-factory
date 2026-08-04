import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common.oauth_codex import _icloud_existing_codes, handle_add_phone


class OAuthCodexTests(unittest.TestCase):
    def test_existing_icloud_codes_collects_extracted_and_body_codes(self):
        messages = [
            {"extracted": {"codes": ["123456"]}, "subject": "Code 654321"}
        ]
        with patch("common.temp_email.fetch_messages", return_value=messages):
            codes = asyncio.run(_icloud_existing_codes("person@icloud.com"))
        self.assertEqual(codes, {"123456", "654321"})

    def test_phone_retry_treats_navigation_past_add_phone_as_success(self):
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/codex/consent"
            page.locator.return_value.wait_for = AsyncMock(side_effect=RuntimeError())
            with patch(
                "common.oauth_codex._goto_add_phone",
                new=AsyncMock(return_value=False),
            ):
                return await handle_add_phone(
                    page,
                    auth_url="https://auth.openai.com/oauth/authorize",
                    account_email="person@example.com",
                    attempts=1,
                    sms_timeout=1,
                )

        self.assertTrue(asyncio.run(exercise()))

    def test_phone_retry_does_not_treat_login_page_as_success(self):
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/log-in"
            page.locator.return_value.wait_for = AsyncMock(side_effect=RuntimeError())
            with patch(
                "common.oauth_codex._goto_add_phone",
                new=AsyncMock(return_value=False),
            ):
                return await handle_add_phone(
                    page,
                    auth_url="https://auth.openai.com/oauth/authorize",
                    account_email="person@example.com",
                    attempts=1,
                    sms_timeout=1,
                )

        self.assertFalse(asyncio.run(exercise()))

    def test_forced_hero_provider_is_forwarded_to_sms_client(self):
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/add-phone"
            page.locator.return_value.wait_for = AsyncMock(return_value=None)
            with patch("common.sms.get_phone", return_value=("15550001111", "", "hero_1")) as get_phone, patch(
                "common.oauth_codex._fill_phone_continue", new=AsyncMock()
            ), patch("common.sms.get_code", return_value="123456"), patch(
                "common.oauth_codex._enter_otp", new=AsyncMock()
            ):
                async def advance(*args, **kwargs):
                    page.url = "https://auth.openai.com/codex/consent"

                with patch("common.oauth_codex.asyncio.sleep", new=advance):
                    ok = await handle_add_phone(
                        page, attempts=1, sms_timeout=1, sms_provider="hero"
                    )
            return ok, get_phone

        ok, get_phone = asyncio.run(exercise())
        self.assertTrue(ok)
        self.assertEqual(get_phone.call_args.kwargs["provider"], "hero")


if __name__ == "__main__":
    unittest.main()
