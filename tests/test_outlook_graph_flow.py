import unittest
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import outlook_reg_loop
from tools import extract_graph_tokens
import register_outlook_standalone
from webui import scripts as webui_scripts


class OutlookGraphFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_outlook_open_falls_back_when_preferred_core_cannot_install(self):
        browser = MagicMock()
        browser.open_browser.side_effect = [
            Exception("内核更新失败，请重新启动客户端再打开"),
            {"ws": "ws://fallback-ready"},
        ]

        result = outlook_reg_loop.bb_open_for_outlook_reg(browser, "profile")

        self.assertEqual(result["ws"], "ws://fallback-ready")
        browser._post.assert_called_once_with(
            "/browser/update/partial",
            {
                "ids": ["profile"],
                "browserFingerPrint": {"coreVersion": "130"},
            },
        )

    def test_outlook_max_press_defaults_to_five_everywhere(self):
        self.assertEqual(outlook_reg_loop.DEFAULT_MAX_PRESS, "5")
        outlook_entry = next(
            item for item in webui_scripts.SCRIPTS
            if item.get("id") == "outlook_reg_loop"
        )
        max_press = next(
            item for item in outlook_entry["args"]
            if item.get("flag") == "--max-press"
        )
        self.assertEqual(max_press["default"], "5")

    def test_outlook_recovery_config_is_prominent_and_supports_owned_mailbox(self):
        group = next(
            item for item in webui_scripts.ENV_SCHEMA
            if item["group"] == "Outlook 自注册"
        )
        self.assertEqual(group["notice_level"], "warning")
        self.assertIn("Graph RT", group["notice"])
        self.assertIn("outlook-recovery", {test["target"] for test in group["tests"]})
        recovery = {
            item["key"]: item for item in group["items"]
        }
        self.assertEqual(recovery["OUTLOOK_GRAPH_RECOVERY_EMAIL"]["type"], "bool")
        self.assertEqual(recovery["OUTLOOK_GRAPH_RECOVERY_EMAIL"]["default"], "true")
        self.assertIn("outlook", recovery["OUTLOOK_GRAPH_RECOVERY_PROVIDER"]["choices"])
        self.assertTrue(recovery["OUTLOOK_GRAPH_RECOVERY_OUTLOOK_MAILBOX"]["secret"])

    async def test_optional_recovery_email_is_skipped(self):
        page = MagicMock()
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value="Help us protect your account Add a recovery email Skip for now"
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="skip for now")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, skipped = await (
                register_outlook_standalone._skip_optional_recovery_email(page, 3)
            )

        self.assertTrue(detected)
        self.assertTrue(skipped)

    async def test_device_app_consent_clicks_accept_not_deny(self):
        page = MagicMock()
        page.url = "https://account.live.com/Consent/Update?mkt=EN-SG"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value="Let this app access your info? Deny Accept"
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="accept")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, accepted = await (
                register_outlook_standalone._accept_microsoft_app_consent(page, 2)
            )

        self.assertTrue(detected)
        self.assertTrue(accepted)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("accept", payload["labels"])
        self.assertIn("deny", payload["negativeLabels"])
        self.assertIn("idBtn_Accept", payload["preferredIds"])

    async def test_spanish_device_app_consent_is_detected(self):
        page = MagicMock()
        page.url = "https://account.live.com/Consent/Update?mkt=es-ES"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value="Permitir que esta aplicación acceda a tu información Denegar Aceptar"
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="aceptar")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, accepted = await (
                register_outlook_standalone._accept_microsoft_app_consent(page, 5)
            )

        self.assertTrue(detected)
        self.assertTrue(accepted)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("aceptar", payload["labels"])
        self.assertIn("denegar", payload["negativeLabels"])

    async def test_japanese_device_app_consent_is_accepted(self):
        page = MagicMock()
        page.url = "https://account.live.com/Consent/Update?mkt=ja-JP"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                "このアプリがあなたの情報にアクセスすることを許可しますか? "
                "拒否 許可"
            )
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="許可")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, accepted = await (
                register_outlook_standalone._accept_microsoft_app_consent(page, 9)
            )

        self.assertTrue(detected)
        self.assertTrue(accepted)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("許可する", payload["labels"])
        self.assertIn("拒否する", payload["negativeLabels"])
        self.assertIn("idBtn_Accept", payload["preferredIds"])

    async def test_japanese_signup_consent_gate_is_detected(self):
        page = MagicMock()
        page.url = "https://signup.live.com/privacynotice?mkt=ja-JP"
        page.title = AsyncMock(return_value="Microsoft アカウントのプライバシー")
        page.evaluate = AsyncMock(
            side_effect=[
                "個人データのエクスポート 同意して続行",
                "同意して続行",
            ]
        )
        with (
            patch.dict(register_outlook_standalone.os.environ,
                       {"OUTLOOK_CONFIRM_BEFORE_REGISTER": "1"}, clear=False),
            patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()),
        ):
            await register_outlook_standalone._maybe_confirm_before_register(
                page, "[#10]"
            )

        self.assertGreaterEqual(page.evaluate.await_count, 2)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("同意して続行", payload["labels"])

    async def test_arabic_device_app_consent_is_accepted(self):
        page = MagicMock()
        page.url = "https://account.live.com/Consent/Update?mkt=ar-AE"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                "هل تريد السماح لهذا التطبيق بالوصول إلى معلوماتك؟ "
                "إلغاء قبول"
            )
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="قبول")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, accepted = await (
                register_outlook_standalone._accept_microsoft_app_consent(page, 7)
            )

        self.assertTrue(detected)
        self.assertTrue(accepted)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("قبول", payload["labels"])
        self.assertIn("إلغاء", payload["negativeLabels"])

    async def test_arabic_stay_signed_in_prompt_clicks_yes(self):
        page = MagicMock()
        page.url = "https://login.live.com/kmsi"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value="هل تريد أن يظل دخولك مسجلاً؟ نعم لا"
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="نعم")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, continued = await (
                register_outlook_standalone._handle_microsoft_kmsi(page, 8)
            )

        self.assertTrue(detected)
        self.assertTrue(continued)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("نعم", payload["labels"])
        self.assertIn("لا", payload["negativeLabels"])
        self.assertIn("idSIButton9", payload["preferredIds"])

    async def test_traditional_chinese_stay_signed_in_clicks_yes_not_no(self):
        page = MagicMock()
        page.url = "https://login.live.com/common/SAS/ProcessAuth"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                "nwz08jxp0xwq@outlook.com "
                "\u8981\u4fdd\u6301\u767b\u5165\u55ce? "
                "\u4e0d\u8981\u6bcf\u6b21\u90fd\u8981\u91cd\u65b0\u767b\u5165\u3002 "
                "\u662f \u5426"
            )
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="\u662f")

        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, continued = await (
                register_outlook_standalone._handle_microsoft_kmsi(page, 4)
            )

        self.assertTrue(detected)
        self.assertTrue(continued)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("\u662f", payload["labels"])
        self.assertIn("\u5426", payload["negativeLabels"])

    async def test_japanese_stay_signed_in_prompt_clicks_yes_not_no(self):
        page = MagicMock()
        page.url = "https://login.live.com/kmsi"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                "サインインの状態を維持しますか? "
                "毎回サインインする必要がないようにします。 はい いいえ"
            )
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="はい")

        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, continued = await (
                register_outlook_standalone._handle_microsoft_kmsi(page, 11)
            )

        self.assertTrue(detected)
        self.assertTrue(continued)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("はい", payload["labels"])
        self.assertIn("いいえ", payload["negativeLabels"])
        self.assertIn("idSIButton9", payload["preferredIds"])

    async def test_japanese_stay_signed_in_prompt_clicks_yes_inside_iframe(self):
        page = MagicMock()
        page.url = "https://login.live.com/kmsi"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                "サインインの状態を維持しますか? "
                "毎回サインインする必要がないようにします。 はい いいえ"
            )
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="")
        frame = MagicMock()
        frame.evaluate = AsyncMock(return_value="はい")
        page.frames = [frame]

        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, continued = await (
                register_outlook_standalone._handle_microsoft_kmsi(page, 12)
            )

        self.assertTrue(detected)
        self.assertTrue(continued)
        page.evaluate.assert_awaited_once()
        frame.evaluate.assert_awaited_once()

    async def test_german_recovery_email_is_skipped(self):
        page = MagicMock()
        page.url = "https://account.live.com/proofs/Add"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value="Sicherheitsinformationen Wiederherstellungs-E-Mail Nicht jetzt"
        )
        page.locator.return_value = body
        page.evaluate = AsyncMock(return_value="nicht jetzt")
        with patch.object(register_outlook_standalone.asyncio, "sleep", AsyncMock()):
            detected, skipped = await (
                register_outlook_standalone._skip_optional_recovery_email(page, 6)
            )

        self.assertTrue(detected)
        self.assertTrue(skipped)
        payload = page.evaluate.call_args.args[1]
        self.assertIn("nicht jetzt", payload["labels"])
        self.assertIn("idBtn_Skip", payload["preferredIds"])

    def test_birthdate_fields_are_classified_across_locales(self):
        cases = (
            ({"id": "BirthMonthDropdown", "ariaLabel": "Monat"}, "month"),
            ({"name": "BirthDay", "ariaLabel": "Día"}, "day"),
            ({"id": "BirthYearInput", "ariaLabel": "Rok"}, "year"),
            ({"id": "countryDropdownId", "ariaLabel": "País"}, "country"),
        )
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                self.assertEqual(
                    register_outlook_standalone._birthdate_field_kind(metadata),
                    expected,
                )

    def test_microsoft_urls_receive_locale_hints_without_losing_query(self):
        with patch.object(register_outlook_standalone, "MICROSOFT_UI_LOCALE", "en-US"):
            url = register_outlook_standalone._microsoft_url_with_locale(
                "https://www.microsoft.com/link?otc=1"
            )

        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["otc"], ["1"])
        self.assertEqual(query["mkt"], ["en-US"])
        self.assertEqual(query["ui_locales"], ["en-US"])

    def test_http_graph_form_parser_ignores_attribute_order_and_locale(self):
        markup = """
            <form method='post' action='../consent/submit?x=1&amp;y=2'>
              <p>Autoriser cette application</p>
              <input value='canary-token' type='hidden' name='canary'>
              <input name='ucaction' value='Yes' type='hidden'>
            </form>
        """
        forms, inputs = extract_graph_tokens._parse_microsoft_forms(
            markup, "https://account.live.com/Consent/Update"
        )

        self.assertEqual(len(forms), 1)
        self.assertEqual(
            forms[0]["action"],
            "https://account.live.com/consent/submit?x=1&y=2",
        )
        self.assertEqual(forms[0]["inputs"]["canary"], "canary-token")
        self.assertEqual(inputs["ucaction"], "Yes")

    def test_http_graph_recovery_form_binds_temp_email(self):
        response = MagicMock(
            url="https://account.live.com/proofs/Add",
            text=(
                "<form method='post' action='/proofs/Add'>"
                "<input type='hidden' name='canary' value='csrf'>"
                "<input type='email' id='EmailAddress' name='EmailAddress'>"
                "</form>"
            ),
        )
        posted = MagicMock(status_code=200, url="https://account.live.com/proofs/Add", text="")
        session = MagicMock()
        session.post.return_value = posted
        mailbox = {
            "id": "box-1", "email": "alias@example.test", "token": "box-token",
            "provider": "custom",
        }
        with (
            patch.object(extract_graph_tokens, "_create_graph_recovery_mailbox", return_value=mailbox),
            patch.object(extract_graph_tokens, "_graph_recovery_settings",
                         return_value=(True, "custom", 30, 1)),
        ):
            next_response, state = extract_graph_tokens._advance_recovery_email(
                session, response, {}, "[#1]"
            )

        self.assertIs(next_response, posted)
        self.assertEqual(state, "email")
        self.assertEqual(session.post.call_args.kwargs["data"]["EmailAddress"], mailbox["email"])

    def test_http_graph_recovery_form_submits_mailbox_code(self):
        response = MagicMock(
            url="https://account.live.com/proofs/Verify",
            text=(
                "<form method='post' action='/proofs/Verify'>"
                "<input type='hidden' name='canary' value='csrf'>"
                "<input type='text' id='iOttText' name='iOttText'>"
                "</form>"
            ),
        )
        posted = MagicMock(status_code=302, url="https://login.live.com/oauth20_authorize.srf", text="")
        session = MagicMock()
        session.post.return_value = posted
        mailbox = {
            "id": "box-1", "email": "alias@example.test", "token": "box-token",
            "provider": "custom",
        }
        with (
            patch.object(extract_graph_tokens, "_poll_graph_recovery_code", return_value="123456"),
            patch.object(extract_graph_tokens, "_graph_recovery_settings",
                         return_value=(True, "custom", 30, 1)),
        ):
            next_response, state = extract_graph_tokens._advance_recovery_email(
                session, response, {"mailbox": mailbox}, "[#1]"
            )

        self.assertIs(next_response, posted)
        self.assertEqual(state, "code")
        self.assertEqual(session.post.call_args.kwargs["data"]["iOttText"], "123456")

    def test_http_graph_redirect_resolves_relative_location(self):
        response = MagicMock()
        response.url = "https://login.live.com/oauth20_authorize.srf"

        result = extract_graph_tokens._redirect_url(
            response, "/Consent/Update?x=1&amp;y=2"
        )

        self.assertEqual(
            result, "https://login.live.com/Consent/Update?x=1&y=2"
        )

    async def test_attempted_device_flow_does_not_fall_back_to_broken_web_oauth(self):
        page = MagicMock()
        context = MagicMock()
        with (
            patch.object(
                register_outlook_standalone,
                "_extract_graph_token_device",
                AsyncMock(return_value=None),
            ),
            patch.object(
                register_outlook_standalone,
                "_extract_graph_token_authorization_code",
                AsyncMock(),
            ) as fallback,
        ):
            result = await register_outlook_standalone.extract_graph_token(
                page, context, "user@outlook.com", "password", 4
            )

        self.assertIsNone(result)
        fallback.assert_not_awaited()

    def test_consumer_graph_oauth_uses_proven_thunderbird_public_client(self):
        self.assertEqual(
            register_outlook_standalone.GRAPH_CLIENT_ID,
            "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        )
        self.assertEqual(register_outlook_standalone.GRAPH_REDIRECT_URI, "http://localhost")
        self.assertEqual(
            register_outlook_standalone.GRAPH_SCOPE,
            "offline_access https://graph.microsoft.com/Mail.Read",
        )

    def test_graph_token_exchange_uses_direct_session(self):
        with patch.object(register_outlook_standalone.requests, "Session") as session_factory:
            session = session_factory.return_value
            direct = register_outlook_standalone._microsoft_direct_session()

        self.assertIs(direct, session)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {"http": None, "https": None})

    def test_device_code_token_poll_returns_refresh_token_and_client_id(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }
        session = MagicMock()
        session.post.return_value = response
        with patch.object(
            register_outlook_standalone,
            "_microsoft_direct_session",
            return_value=session,
        ):
            state, result = register_outlook_standalone._exchange_graph_device_code(
                "device-code"
            )

        self.assertEqual(state, "ready")
        self.assertEqual(result["refresh_token"], "refresh-token")
        self.assertEqual(
            result["client_id"], register_outlook_standalone.GRAPH_CLIENT_ID
        )
        payload = session.post.call_args.kwargs["data"]
        self.assertEqual(
            payload["grant_type"],
            "urn:ietf:params:oauth:grant-type:device_code",
        )
        session.close.assert_called_once()

    def test_expected_target_closed_background_error_is_suppressed(self):
        target_closed_error = type("TargetClosedError", (Exception,), {})
        loop = MagicMock()

        outlook_reg_loop._playwright_shutdown_exception_handler(
            loop, {"exception": target_closed_error("browser has closed")}
        )

        loop.default_exception_handler.assert_not_called()

    def test_other_background_errors_are_reported(self):
        loop = MagicMock()
        context = {"exception": RuntimeError("unexpected")}

        outlook_reg_loop._playwright_shutdown_exception_handler(loop, context)

        loop.default_exception_handler.assert_called_once_with(context)

    async def test_graph_token_is_extracted_before_live_context_closes(self):
        page = MagicMock()
        context = MagicMock()
        context.pages = []
        context.clear_cookies = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        context.cookies = AsyncMock(return_value=[])

        module = MagicMock()
        module.GRAPH_CLIENT_ID = "browser-client-id"
        module.register_outlook = AsyncMock(
            return_value=("user@outlook.com", "password")
        )
        module.extract_graph_token = AsyncMock(
            return_value={"refresh_token": "refresh-token"}
        )

        email, password, cookies, graph = await outlook_reg_loop._run_outlook_on_ctx(
            module, context, 7
        )

        self.assertEqual(email, "user@outlook.com")
        self.assertEqual(password, "password")
        self.assertEqual(cookies, [])
        self.assertEqual(graph["refresh_token"], "refresh-token")
        self.assertEqual(graph["client_id"], "browser-client-id")
        module.extract_graph_token.assert_awaited_once_with(
            page, context, "user@outlook.com", "password", 7
        )

    async def test_standalone_main_uses_device_browser_before_http_fallback(self):
        bb = MagicMock()
        results = []
        with (
            patch.object(
                register_outlook_standalone,
                "register_outlook_protocol",
                return_value=("user@outlook.com", "password"),
            ),
            patch.object(
                register_outlook_standalone,
                "extract_graph_token_browser",
                AsyncMock(
                    return_value={
                        "refresh_token": "refresh-token",
                        "client_id": "browser-client-id",
                    }
                ),
            ) as browser_extract,
            patch.object(
                register_outlook_standalone,
                "extract_graph_token_http",
            ) as http_extract,
        ):
            await register_outlook_standalone.register_one(
                bb,
                1,
                None,
                results,
                __import__("asyncio").Lock(),
                mode="protocol",
            )

        browser_extract.assert_awaited_once_with(
            bb, "user@outlook.com", "password", 1, None
        )
        http_extract.assert_not_called()
        self.assertEqual(results[0]["status"], "OK")
        self.assertEqual(results[0]["graph"]["refresh_token"], "refresh-token")

    async def test_browser_registration_profile_is_reused_for_graph(self):
        bb = MagicMock()
        results = []
        with (
            patch.object(
                register_outlook_standalone,
                "_register_one_browser",
                AsyncMock(
                    return_value=(
                        "user@outlook.com",
                        "password",
                        "registration-profile",
                        "ws://registration",
                    )
                ),
            ) as browser_register,
            patch.object(
                register_outlook_standalone,
                "extract_graph_token_browser",
                AsyncMock(
                    return_value={
                        "refresh_token": "refresh-token",
                        "client_id": "browser-client-id",
                    }
                ),
            ) as browser_extract,
        ):
            await register_outlook_standalone.register_one(
                bb,
                1,
                None,
                results,
                __import__("asyncio").Lock(),
                mode="browser",
            )

        browser_register.assert_awaited_once_with(
            bb, 1, None, keep_profile=True
        )
        browser_extract.assert_awaited_once_with(
            bb,
            "user@outlook.com",
            "password",
            1,
            None,
            profile_id="registration-profile",
            ws="ws://registration",
        )
        self.assertEqual(results[0]["status"], "OK")


if __name__ == "__main__":
    unittest.main()
