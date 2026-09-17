import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common.oauth_codex import (
    CONSENT_LABELS,
    callback_url,
    generate_cpa_auth_url,
    submit_cpa_callback,
    _abandon_phone,
    _complete_auth_email_login,
    _enter_otp,
    _icloud_existing_codes,
    _has_phone_error,
    _is_phone_flow_url,
    _totp_code,
    _wait_for_phone_flow_exit,
    authorize_with_retry,
    handle_add_phone,
)


class OAuthCodexTests(unittest.TestCase):
    def test_cpa_auth_url_extracts_nested_state(self):
        response = MagicMock(ok=True)
        response.json.return_value = {
            "data": {
                "auth_url": "https://auth.test/oauth?state=cpa-state",
            }
        }
        with patch("common.oauth_codex.requests.get", return_value=response) as get:
            auth_url, state = generate_cpa_auth_url("https://cpa.test/management.html", "secret")
        self.assertEqual(auth_url, "https://auth.test/oauth?state=cpa-state")
        self.assertEqual(state, "cpa-state")
        self.assertEqual(get.call_args.args[0], "https://cpa.test/v0/management/codex-auth-url")

    def test_cpa_callback_retries_transient_conflict(self):
        failed = MagicMock(ok=False, status_code=409, text="Timeout waiting for OAuth callback")
        failed.json.return_value = {"error": "Timeout waiting for OAuth callback"}
        success = MagicMock(ok=True, status_code=200)
        success.json.return_value = {"message": "accepted"}
        with patch("common.oauth_codex.requests.post", side_effect=[failed, success]) as post, patch(
            "common.oauth_codex.time.sleep"
        ) as sleep:
            payload = submit_cpa_callback(
                "https://cpa.test",
                "secret",
                "http://localhost:1455/auth/callback?code=code&state=state",
                retries=2,
                retry_delay=0,
            )
        self.assertEqual(payload["message"], "accepted")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        self.assertNotIn("secret", str(post.call_args_list[0].kwargs["json"]))

    def test_callback_url_uses_redirect_uri_from_authorize_url(self):
        result = callback_url(
            "code-value",
            "state-value",
            "https://auth.openai.com/oauth/authorize?redirect_uri="
            "http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback",
        )
        self.assertEqual(
            result,
            "http://localhost:1455/auth/callback?code=code-value&state=state-value",
        )

    def test_authorize_retry_rejects_callback_without_state(self):
        async def exercise():
            with patch(
                "common.oauth_codex.drive_authorize",
                new=AsyncMock(return_value=("code-value", None, "ok")),
            ), patch("common.oauth_codex.asyncio.sleep", new=AsyncMock()):
                return await authorize_with_retry(
                    MagicMock(),
                    lambda: ("https://auth.test", "session", "expected-state"),
                    phone_skip_attempts=0,
                    allow_phone=False,
                )

        code, session_id, state, message = asyncio.run(exercise())
        self.assertIsNone(code)
        self.assertIsNone(session_id)
        self.assertIsNone(state)
        self.assertIn("缺少 state", message)

    def test_totp_code_matches_rfc_6238_vector(self):
        self.assertEqual(
            _totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", timestamp=59),
            "287082",
        )

    def test_czech_consent_labels_are_supported(self):
        self.assertIn("Přijmout a pokračovat", CONSENT_LABELS)
        self.assertIn("Povolit", CONSENT_LABELS)

    def test_phone_verification_route_remains_in_phone_flow(self):
        self.assertTrue(
            _is_phone_flow_url("https://auth.openai.com/phone-verification")
        )
        self.assertTrue(_is_phone_flow_url("https://auth.openai.com/add-phone"))
        self.assertFalse(_is_phone_flow_url("https://auth.openai.com/codex/consent"))

    def test_whatsapp_fallback_message_is_a_phone_error(self):
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value=(
                "We couldn't send a text message to this phone number, so we switched "
                "to WhatsApp. Continue to send a verification code on WhatsApp."
            )
        )
        self.assertTrue(asyncio.run(_has_phone_error(page)))

    def test_phone_already_used_message_is_a_phone_error(self):
        """「该电话号码已被使用。请使用其他电话号码。」→ 触发换号重试。"""
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value="该电话号码已被使用。请使用其他电话号码。"
        )
        self.assertTrue(asyncio.run(_has_phone_error(page)))

    def test_traditional_chinese_whatsapp_fallback_is_detected(self):
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value="我們無法將簡訊傳送到此電話號碼，因此已切換為 WhatsApp。"
        )
        self.assertTrue(asyncio.run(_has_phone_error(page)))

    def test_voip_rejection_message_is_a_phone_error(self):
        """「这似乎是个虚拟号码（也称为 VoIP）…」→ 立即换号重试。"""
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value=(
                "电话号码是必填项\n添加电话号码以继续。我们将向该电话号码发送一次性验证码。\n"
                "美国 (+1)\n+1 606 322 1238\n电话号码\n短信\nWhatsApp\n"
                "这似乎是个虚拟号码（也称为 VoIP）。请提供有效的非虚拟电话号码以继续。\n继续"
            )
        )
        self.assertTrue(asyncio.run(_has_phone_error(page)))

    def test_english_voip_rejection_message_is_a_phone_error(self):
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value=(
                "This appears to be a virtual phone number (also known as VoIP). "
                "Please provide a valid non-virtual phone number to continue."
            )
        )
        self.assertTrue(asyncio.run(_has_phone_error(page)))

    def test_normal_phone_page_without_error_is_not_a_phone_error(self):
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value=(
                "电话号码是必填项\n添加电话号码以继续。我们将向该电话号码发送一次性验证码。\n"
                "美国 (+1)\n电话号码\n短信\nWhatsApp\n继续"
            )
        )
        self.assertFalse(asyncio.run(_has_phone_error(page)))

    def test_unrelated_already_text_is_not_a_phone_error(self):
        page = MagicMock()
        page.inner_text = AsyncMock(return_value="Already signed in")
        self.assertFalse(asyncio.run(_has_phone_error(page)))

    def test_phone_flow_exit_requires_consent_or_callback(self):
        async def exercise(url):
            page = MagicMock()
            page.url = url
            with patch(
                "common.oauth_codex._has_phone_error", new=AsyncMock(return_value=False)
            ), patch(
                # timeout=0 会让 deadline 循环一次都不进（时钟分辨率），必须给正时长
                "common.oauth_codex.asyncio.sleep", new=AsyncMock()
            ):
                return await _wait_for_phone_flow_exit(page, timeout=0.2)

        self.assertFalse(
            asyncio.run(exercise("https://auth.openai.com/phone-verification"))
        )
        self.assertTrue(
            asyncio.run(exercise("https://auth.openai.com/codex/consent"))
        )

    def test_single_otp_uses_react_fill_and_visible_submit(self):
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/phone-verification"

            otp = MagicMock()
            otp.first = otp
            otp.wait_for = AsyncMock()
            otp.count = AsyncMock(return_value=1)

            submit = MagicMock()
            submit.first = submit
            submit.count = AsyncMock(return_value=1)
            submit.is_visible = AsyncMock(return_value=True)
            submit.click = AsyncMock()
            page.locator.side_effect = [otp, submit]

            with patch(
                "common.browser.react_fill", new=AsyncMock(return_value=True)
            ) as react_fill, patch(
                "common.oauth_codex.asyncio.sleep", new=AsyncMock()
            ):
                ok = await _enter_otp(page, "606325")
            return ok, react_fill, submit

        ok, react_fill, submit = asyncio.run(exercise())
        self.assertTrue(ok)
        self.assertEqual(react_fill.await_args.args[2], "606325")
        self.assertIn(":visible", react_fill.await_args.args[1])
        submit.click.assert_awaited_once()

    def test_existing_icloud_codes_collects_extracted_and_body_codes(self):
        messages = [
            {"extracted": {"codes": ["123456"]}, "subject": "Code 654321"}
        ]
        with patch("common.temp_email.fetch_messages", return_value=messages):
            codes = asyncio.run(_icloud_existing_codes("person@icloud.com"))
        self.assertEqual(codes, {"123456", "654321"})

    def test_email_code_provider_is_used_for_outlook_oauth_login(self):
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/log-in"
            email_input = MagicMock()
            email_input.first = email_input
            email_input.count = AsyncMock(return_value=1)
            code_input = MagicMock()
            code_input.first = code_input
            code_input.count = AsyncMock(return_value=1)
            submit = MagicMock()
            submit.first = submit
            submit.count = AsyncMock(return_value=1)

            click_count = 0

            async def click():
                nonlocal click_count
                click_count += 1
                page.url = (
                    "https://auth.openai.com/email-verification"
                    if click_count == 1
                    else "https://auth.openai.com/codex/consent"
                )

            submit.click = AsyncMock(side_effect=click)
            page.locator.side_effect = [email_input, submit, code_input, submit]
            provider = AsyncMock(return_value="123456")
            with patch("common.browser.react_fill", new=AsyncMock(return_value=True)) as fill, patch(
                "common.oauth_codex.asyncio.sleep", new=AsyncMock()
            ):
                ok, message = await _complete_auth_email_login(
                    page, "person@outlook.jp", email_code_provider=provider
                )
            return ok, message, provider, fill, page

        ok, message, provider, fill, page = asyncio.run(exercise())
        self.assertTrue(ok, message)
        provider.assert_awaited_once()
        self.assertEqual(provider.await_args.args[0], "person@outlook.jp")
        self.assertIsInstance(provider.await_args.args[1], float)
        self.assertGreaterEqual(fill.await_count, 2)
        self.assertEqual(page.url, "https://auth.openai.com/codex/consent")

    def test_oauth_password_page_is_completed_before_email_code(self):
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/log-in/password"
            password_input = MagicMock()
            password_input.first = password_input
            password_input.count = AsyncMock(return_value=1)
            password_input.is_visible = AsyncMock(return_value=True)
            code_input = MagicMock()
            code_input.first = code_input
            code_input.count = AsyncMock(return_value=1)
            submit = MagicMock()
            submit.first = submit
            submit.count = AsyncMock(return_value=1)

            click_count = 0

            async def click():
                nonlocal click_count
                click_count += 1
                page.url = (
                    "https://auth.openai.com/email-verification"
                    if click_count == 1
                    else "https://auth.openai.com/codex/consent"
                )

            submit.click = AsyncMock(side_effect=click)
            page.locator.side_effect = [password_input, submit, code_input, submit]
            provider = AsyncMock(return_value="123456")
            with patch("common.browser.react_fill", new=AsyncMock(return_value=True)) as fill, patch(
                "common.oauth_codex.asyncio.sleep", new=AsyncMock()
            ):
                ok, message = await _complete_auth_email_login(
                    page,
                    "person@outlook.jp",
                    email_code_provider=provider,
                    account_password="fixture-password",
                )
            return ok, message, provider, fill, page

        ok, message, provider, fill, page = asyncio.run(exercise())
        self.assertTrue(ok, message)
        provider.assert_awaited_once()
        self.assertIn("fixture-password", str(fill.await_args_list))
        self.assertEqual(page.url, "https://auth.openai.com/codex/consent")

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

    # ---------------- liye 卡密式：被拒/收不到码的放弃号码策略 ----------------

    def test_abandon_phone_liye_replace_returns_new_number(self):
        """liye 被拒：同卡 replace 换号成功，新号直接返回，不取消不延迟。"""
        with patch("common.liye_sms.order_consumed", return_value=False), patch(
            "common.liye_sms.replace", return_value=("447700900123", "", "liye_999")
        ) as replace, patch("common.sms.release") as release, patch(
            "common.sms.defer_release"
        ) as defer:
            res = _abandon_phone("liye_123", 100.0, ("63",))
        self.assertEqual(res, ("447700900123", "", "liye_999"))
        replace.assert_called_once_with("liye_123")
        release.assert_not_called()
        defer.assert_not_called()

    def test_abandon_phone_liye_blacklisted_replace_keeps_replacing(self):
        """换出的号命中黑名单(+63)就继续换，直到换出非黑名单号。"""
        with patch("common.liye_sms.order_consumed", return_value=False), patch(
            "common.liye_sms.replace",
            side_effect=[("639171234567", "", "liye_201"), ("447700900456", "", "liye_202")],
        ) as replace, patch("common.sms.release") as release:
            res = _abandon_phone("liye_123", 100.0, ("63",))
        self.assertEqual(res, ("447700900456", "", "liye_202"))
        self.assertEqual(replace.call_count, 2)
        release.assert_not_called()

    def test_abandon_phone_liye_blacklisted_exhausts_into_release(self):
        """3 次换号全是黑名单号：取消最后换出的号退回卡密。"""
        with patch("common.liye_sms.order_consumed", return_value=False), patch(
            "common.liye_sms.replace",
            side_effect=[("631", "", "liye_201"), ("632", "", "liye_202"), ("633", "", "liye_203")],
        ), patch("common.sms.release") as release:
            res = _abandon_phone("liye_123", 100.0, ("63",))
        self.assertIsNone(res)
        release.assert_called_once_with("liye_203")

    def test_abandon_phone_liye_replace_failure_releases_immediately(self):
        """replace 不可用(冷却/无号)：立即同步取消退回，不走 120s 延迟取消。"""
        with patch("common.liye_sms.order_consumed", return_value=False), patch(
            "common.liye_sms.replace", return_value=None
        ), patch("common.sms.release") as release, patch("common.sms.defer_release") as defer:
            res = _abandon_phone("liye_123", 100.0)
        self.assertIsNone(res)
        release.assert_called_once_with("liye_123")
        defer.assert_not_called()

    def test_abandon_phone_liye_consumed_card_is_skipped(self):
        """已收到过验证码的卡已消耗：不 replace 不取消，直接结束。"""
        with patch("common.liye_sms.order_consumed", return_value=True), patch(
            "common.liye_sms.replace"
        ) as replace, patch("common.sms.release") as release:
            res = _abandon_phone("liye_123", 100.0)
        self.assertIsNone(res)
        release.assert_not_called()
        replace.assert_not_called()

    def test_abandon_phone_non_liye_keeps_deferred_release(self):
        """hero/sms-man 维持最小持有期延迟取消(旧行为)。"""
        with patch("common.sms.defer_release") as defer, patch("common.sms.release") as release:
            res = _abandon_phone("hero_123", 123.0)
        self.assertIsNone(res)
        defer.assert_called_once_with("hero_123", 123.0)
        release.assert_not_called()

    def test_phone_retry_liye_rejection_uses_replaced_number(self):
        """liye 被拒后同卡换号：第二轮直接填 replace 的新号，不再取号。"""
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/add-phone"
            page.locator.return_value.wait_for = AsyncMock(return_value=None)
            with patch("common.sms.get_phone", return_value=("447700900123", "", "liye_1")) as get_phone, patch(
                "common.oauth_codex._fill_phone_continue", new=AsyncMock()
            ), patch(
                "common.oauth_codex._has_phone_error",
                new=AsyncMock(side_effect=[True, False]),
            ), patch(
                "common.oauth_codex._goto_add_phone", new=AsyncMock(return_value=True)
            ), patch(
                "common.oauth_codex._abandon_phone",
                return_value=("447700900456", "", "liye_2"),
            ), patch(
                "common.sms.get_code", return_value="123456"
            ), patch(
                "common.oauth_codex._enter_otp", new=AsyncMock(return_value=True)
            ), patch(
                "common.oauth_codex._wait_for_phone_flow_exit", new=AsyncMock(return_value=True)
            ), patch(
                "common.oauth_codex.asyncio.sleep", new=AsyncMock()
            ):
                ok = await handle_add_phone(
                    page,
                    auth_url="https://auth.openai.com/oauth/authorize",
                    attempts=3,
                    sms_timeout=1,
                    sms_provider="liye",
                )
            return ok, get_phone

        ok, get_phone = asyncio.run(exercise())
        self.assertTrue(ok)
        # 只有第一轮取了号；被拒后第二轮直接用 replace 换出的号
        self.assertEqual(get_phone.call_count, 1)

    def test_phone_retry_stops_early_when_strict_cards_unavailable(self):
        """严格模式卡池不可用：立即停止，不把剩余尝试次数烧成重复报错。"""
        async def exercise():
            page = MagicMock()
            page.url = "https://auth.openai.com/add-phone"
            page.locator.return_value.wait_for = AsyncMock(return_value=None)
            err = RuntimeError(
                "liye: 勾选的卡密均不可用（耗尽/占用/冷却）；「仅用勾选卡密」开启中，不回落未勾选卡 for service chatai"
            )
            with patch("common.sms.get_phone", side_effect=err) as get_phone:
                return await handle_add_phone(page, attempts=5, sms_timeout=1), get_phone

        ok, get_phone = asyncio.run(exercise())
        self.assertFalse(ok)
        self.assertEqual(get_phone.call_count, 1)


if __name__ == "__main__":
    unittest.main()
