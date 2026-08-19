import argparse
import asyncio
import inspect
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import register_chatgpt
import register_three_platforms
import run_full_flow
import oauth_codex


class ChatGPTFlowTests(unittest.TestCase):
    def test_password_step_classifies_existing_account_before_signup_fallback(self):
        self.assertEqual(
            register_chatgpt._classify_chatgpt_password_step(
                "https://auth.openai.com/log-in/password",
                "Enter your password",
                "current-password",
                "password",
            ),
            "login",
        )
        self.assertEqual(
            register_chatgpt._classify_chatgpt_password_step(
                "https://auth.openai.com/create-account/password",
                "Create a password",
                "new-password",
                "password",
            ),
            "create",
        )

    def test_password_step_does_not_treat_hidden_or_missing_fields_as_password_page(self):
        field = MagicMock()
        field.count = AsyncMock(return_value=1)
        field.nth.return_value.is_visible = AsyncMock(return_value=False)
        page = MagicMock()
        page.locator.return_value = field
        self.assertEqual(asyncio.run(register_chatgpt.detect_chatgpt_password_step(page)), "none")

    def test_chatgpt_registration_prefers_a_verified_graph_mailbox(self):
        async def exercise():
            mailbox = ("good@outlook.com", "pw", "rt", "client")
            with patch.object(
                register_chatgpt.email_pool,
                "retryable_email",
                return_value=None,
            ) as retryable, patch.object(
                register_chatgpt.email_pool,
                "latest_email",
                return_value=mailbox,
            ) as latest, patch.object(
                register_chatgpt.email_pool, "next_email"
            ) as fallback, patch.object(
                register_chatgpt.email_pool, "mark_error"
            ), patch.dict(
                os.environ, {"CHATGPT_GOTO_ATTEMPTS": "1"}, clear=False
            ), patch.object(
                register_chatgpt,
                "open_and_connect",
                AsyncMock(side_effect=RuntimeError("stop after mailbox selection")),
            ):
                await register_chatgpt.register_one(1, 1, MagicMock())
            return retryable, latest, fallback

        retryable, latest, fallback = asyncio.run(exercise())

        latest.assert_called_once_with(
            register_chatgpt.PLATFORM,
            require_token=True,
            validate_token=True,
        )
        retryable.assert_called_once_with(
            register_chatgpt.PLATFORM,
            require_token=True,
            validate_token=True,
        )
        fallback.assert_not_called()

    def test_chatgpt_icloud_mailbox_uses_cheap_submail(self):
        with patch.object(
            register_chatgpt,
            "create_mailbox",
            return_value={"email": "code@example.com"},
        ) as create:
            mailbox = register_chatgpt.create_chatgpt_icloud_mailbox()

        self.assertEqual(mailbox["email"], "code@example.com")
        create.assert_called_once_with(provider="icloud", mail_type="icloud")

    def test_icloud_allocation_skips_mother_mailbox_rejected_by_openai(self):
        with tempfile.TemporaryDirectory() as root:
            with open(
                os.path.join(root, "emails_error_chatgpt.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(
                    "used+old@icloud.com--------user_already_exists: existing\n"
                )
            mailboxes = [
                {"email": "used+new@icloud.com"},
                {"email": "fresh+new@icloud.com"},
            ]
            with patch.dict(
                os.environ, {"REG_FACTORY_DATA_DIR": root}, clear=False
            ), patch.object(
                register_chatgpt,
                "create_chatgpt_icloud_mailbox",
                side_effect=mailboxes,
            ) as create:
                mailbox = register_chatgpt._allocate_untainted_chatgpt_icloud_mailbox()

        self.assertEqual(mailbox["email"], "fresh+new@icloud.com")
        self.assertEqual(create.call_count, 2)

    def test_icloud_registration_retries_until_requested_account_succeeds(self):
        with patch.object(
            register_chatgpt, "EMAIL_PROVIDER", "icloud"
        ), patch.object(
            register_chatgpt, "FIXED_EMAIL", None
        ), patch.dict(
            os.environ, {"CHATGPT_ICLOUD_MAILBOX_ATTEMPTS": "3"}, clear=False
        ), patch.object(
            register_chatgpt,
            "register_one",
            AsyncMock(side_effect=[None, "session-cookie"]),
        ) as register:
            result = asyncio.run(
                register_chatgpt.register_one_with_mailbox_retries(
                    1, 1, MagicMock()
                )
            )

        self.assertEqual(result, "session-cookie")
        self.assertEqual(register.await_count, 2)

    def test_exhausted_outlook_pool_falls_back_to_icloud(self):
        mailbox = {
            "id": "icloud-box",
            "email": "fallback@icloud.com",
            "provider": "icloud",
        }
        with patch.object(register_chatgpt, "EMAIL_PROVIDER", "pool"), patch.object(
            register_chatgpt.email_pool, "retryable_email", return_value=None
        ), patch.object(
            register_chatgpt.email_pool, "latest_email", return_value=None
        ), patch.object(
            register_chatgpt.email_pool, "next_email", return_value=None
        ), patch.object(
            register_chatgpt, "create_chatgpt_icloud_mailbox", return_value=mailbox
        ) as create, patch.dict(
            os.environ, {"CHATGPT_POOL_ICLOUD_FALLBACK": "true"}, clear=False
        ):
            result = register_chatgpt.allocate_chatgpt_registration_mailbox()

        self.assertEqual(result["email"], "fallback@icloud.com")
        self.assertIs(result["mailbox"], mailbox)
        create.assert_called_once_with()

    def test_outlook_pool_fallback_can_be_disabled(self):
        with patch.object(register_chatgpt, "EMAIL_PROVIDER", "pool"), patch.object(
            register_chatgpt.email_pool, "retryable_email", return_value=None
        ), patch.object(
            register_chatgpt.email_pool, "latest_email", return_value=None
        ), patch.object(
            register_chatgpt.email_pool, "next_email", return_value=None
        ), patch.object(
            register_chatgpt, "create_chatgpt_icloud_mailbox"
        ) as create, patch.dict(
            os.environ, {"CHATGPT_POOL_ICLOUD_FALLBACK": "false"}, clear=False
        ):
            result = register_chatgpt.allocate_chatgpt_registration_mailbox()

        self.assertIsNone(result)
        create.assert_not_called()

    def test_registration_runs_targeted_plus_trial_check(self):
        async def exercise():
            with patch(
                "common.asset_scanner.check_chatgpt_plus_trial_for_session",
                return_value={
                    "plus_trial": "zero_price",
                    "plus_trial_detail": "命中明确显示 0 元的 Plus 优惠",
                },
            ) as check:
                result = await register_chatgpt.check_chatgpt_plus_trial_after_registration(
                    {"accessToken": "token"}, "trial@example.com"
                )
            return result, check

        result, check = asyncio.run(exercise())

        self.assertEqual(result["plus_trial"], "zero_price")
        check.assert_called_once_with({"accessToken": "token"}, "trial@example.com", 15)

    def test_expired_auth_entry_is_reopened(self):
        async def exercise():
            field = MagicMock()
            field.first = field
            field.count = AsyncMock(side_effect=[0, 1])
            field.is_visible = AsyncMock(return_value=True)
            body = MagicMock()
            body.inner_text = AsyncMock(return_value="Your session has ended")
            page = MagicMock()
            page.locator.side_effect = lambda selector: (
                field if selector.startswith('input[type="email"]') else body
            )
            page.goto = AsyncMock()
            with patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()):
                recovered = await register_chatgpt.ensure_chatgpt_email_entry(page)
            return recovered, page

        recovered, page = asyncio.run(exercise())

        self.assertTrue(recovered)
        page.goto.assert_awaited_once_with(
            register_chatgpt.SIGNUP_URL,
            timeout=60000,
            wait_until="domcontentloaded",
        )

    def test_missing_email_entry_without_expired_session_does_not_loop(self):
        field = MagicMock()
        field.first = field
        field.count = AsyncMock(return_value=0)
        body = MagicMock()
        body.inner_text = AsyncMock(return_value="Service unavailable")
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            field if selector.startswith('input[type="email"]') else body
        )
        page.goto = AsyncMock()

        recovered = asyncio.run(register_chatgpt.ensure_chatgpt_email_entry(page))

        self.assertFalse(recovered)
        page.goto.assert_not_awaited()

    def test_signup_timeout_accepts_an_already_committed_auth_document(self):
        email = MagicMock()
        email.count = AsyncMock(return_value=1)
        page = MagicMock()
        page.url = register_chatgpt.SIGNUP_URL
        page.goto = AsyncMock(side_effect=TimeoutError("navigation timeout"))
        page.locator.return_value = email
        context = MagicMock()
        context.new_page = AsyncMock()

        result = asyncio.run(register_chatgpt.navigate_chatgpt_signup(context, page))

        self.assertIs(result, page)
        page.goto.assert_awaited_once_with(
            register_chatgpt.SIGNUP_URL,
            timeout=30000,
            wait_until="domcontentloaded",
        )
        context.new_page.assert_not_awaited()

    def test_signup_timeout_rotates_auto_node_and_replaces_wedged_tab(self):
        old_page = MagicMock()
        old_page.url = "about:blank"
        old_page.goto = AsyncMock(side_effect=TimeoutError("navigation timeout"))
        old_page.close = AsyncMock()
        fresh_page = MagicMock()
        fresh_page.goto = AsyncMock(return_value=None)
        context = MagicMock()
        context.new_page = AsyncMock(return_value=fresh_page)
        observer = MagicMock()

        with (
            patch.dict(
                os.environ,
                {"CHATGPT_GOTO_TIMEOUT_SECONDS": "15", "CHATGPT_GOTO_ATTEMPTS": "2"},
                clear=False,
            ),
            patch.object(register_chatgpt, "CHATGPT_NODE", "auto"),
            patch.object(register_chatgpt.proxy_switch, "proxy_mode", return_value="clash_auto"),
            patch.object(
                register_chatgpt,
                "_rotate_chatgpt_auto_node",
                AsyncMock(return_value="next-node"),
            ) as rotate,
        ):
            result = asyncio.run(
                register_chatgpt.navigate_chatgpt_signup(context, old_page, observer)
            )

        self.assertIs(result, fresh_page)
        rotate.assert_awaited_once_with()
        context.new_page.assert_awaited_once_with()
        old_page.close.assert_awaited_once_with()
        fresh_page.on.assert_called_once_with("response", observer)
        fresh_page.goto.assert_awaited_once_with(
            register_chatgpt.SIGNUP_URL,
            timeout=15000,
            wait_until="domcontentloaded",
        )

    def test_auth_asset_monitor_tracks_success_and_connection_failures(self):
        monitor = register_chatgpt.ChatGPTAuthAssetMonitor()
        success = MagicMock(
            url="https://chatgpt.com/cdn/assets/auth-entry.js", status=200
        )
        unrelated = MagicMock(url="https://example.com/app.js", status=200)
        failed = MagicMock(
            url="https://chatgpt.com/cdn/assets/auth-chunk.js",
            failure="net::ERR_CONNECTION_CLOSED",
        )

        monitor.observe_response(success)
        monitor.observe_response(unrelated)
        monitor.observe_failure(failed)

        self.assertEqual(monitor.loaded, {"/cdn/assets/auth-entry.js"})
        self.assertEqual(monitor.failed, ["net::ERR_CONNECTION_CLOSED"])

    def test_auth_assets_require_a_hydrated_email_form(self):
        email = MagicMock()
        email.first = email
        email.count = AsyncMock(return_value=1)
        email.is_visible = AsyncMock(return_value=True)
        page = MagicMock()
        page.locator.return_value = email
        monitor = register_chatgpt.ChatGPTAuthAssetMonitor()
        monitor.loaded.add("/cdn/assets/auth-entry.js")

        with patch.object(
            register_chatgpt,
            "_chatgpt_email_form_hydrated",
            AsyncMock(return_value=True),
        ):
            ready = asyncio.run(
                register_chatgpt.wait_for_chatgpt_auth_assets(
                    page, monitor, timeout=1
                )
            )

        self.assertTrue(ready)

    def test_auth_asset_failures_reject_the_server_rendered_shell(self):
        monitor = register_chatgpt.ChatGPTAuthAssetMonitor()
        monitor.failed.extend(["closed", "closed", "closed"])

        ready = asyncio.run(
            register_chatgpt.wait_for_chatgpt_auth_assets(
                MagicMock(), monitor, timeout=1
            )
        )

        self.assertFalse(ready)

    def test_auth_assets_accept_visible_form_when_new_react_markers_are_hidden(self):
        email = MagicMock()
        email.first = email
        email.count = AsyncMock(return_value=1)
        email.is_visible = AsyncMock(return_value=True)
        page = MagicMock()
        page.locator.return_value = email
        monitor = register_chatgpt.ChatGPTAuthAssetMonitor()
        monitor.loaded.add("/cdn/assets/auth-entry.js")

        with patch.object(
            register_chatgpt,
            "_chatgpt_email_form_hydrated",
            AsyncMock(return_value=False),
        ):
            ready = asyncio.run(
                register_chatgpt.wait_for_chatgpt_auth_assets(
                    page, monitor, timeout=1, fallback_after=0
                )
            )

        self.assertTrue(ready)

    def test_age_selector_does_not_capture_generic_number_inputs(self):
        self.assertNotIn('input[type="number"]', register_chatgpt._AGE_SELECTOR)
        self.assertIn('name="age"', register_chatgpt._AGE_SELECTOR)

    def test_email_retry_preserves_already_committed_value(self):
        async def exercise():
            current = MagicMock()
            current.first = current
            current.count = AsyncMock(return_value=1)
            current.is_visible = AsyncMock(return_value=True)
            current.input_value = AsyncMock(return_value="user@example.com")
            fresh = MagicMock()
            fresh.first = fresh
            fresh.count = AsyncMock(return_value=1)
            fresh.is_visible = AsyncMock(return_value=True)
            fresh.input_value = AsyncMock(return_value="user@example.com")
            page = MagicMock()
            page.locator.side_effect = [current, fresh]
            with (
                patch.object(register_chatgpt, "dismiss_cookie_banner", AsyncMock()),
                patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
                patch.object(register_chatgpt, "react_fill", AsyncMock()) as fill,
            ):
                result = await register_chatgpt.fill_email_verified(
                    page, current, "user@example.com", tries=2
                )
            return result, fill

        result, fill = asyncio.run(exercise())

        self.assertTrue(result)
        fill.assert_not_awaited()

    def test_email_retry_does_not_treat_detached_field_as_success(self):
        async def exercise():
            field = MagicMock()
            field.first = field
            field.count = AsyncMock(return_value=1)
            field.is_visible = AsyncMock(return_value=True)
            field.input_value = AsyncMock(side_effect=RuntimeError("detached"))
            page = MagicMock()
            page.locator.return_value = field
            with (
                patch.object(register_chatgpt, "dismiss_cookie_banner", AsyncMock()),
                patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
                patch.object(register_chatgpt, "react_fill", AsyncMock(return_value=False)),
            ):
                return await register_chatgpt.fill_email_verified(
                    page, field, "user@example.com", tries=1
                )

        self.assertFalse(asyncio.run(exercise()))

    def test_email_query_hint_does_not_override_a_visible_email_form(self):
        email_input = MagicMock()
        email_input.count = AsyncMock(return_value=1)
        email_input.is_visible = AsyncMock(return_value=True)
        page = MagicMock()
        page.url = "https://chatgpt.com/auth/login?email=speedlol8%2Babc%40icloud.com"
        page.locator.return_value.first = email_input

        self.assertFalse(
            asyncio.run(register_chatgpt.chatgpt_email_submission_advanced(page))
        )

    def test_birthday_part_handles_camel_case_without_misreading_birthday(self):
        self.assertIsNone(register_chatgpt._birthday_part("birthday"))
        self.assertEqual(
            register_chatgpt._birthday_part("birthdayMonth"), "month"
        )
        self.assertEqual(register_chatgpt._birthday_part("dob-year"), "year")

    def test_single_birthday_text_field_is_filled(self):
        async def exercise():
            field = MagicMock()
            field.get_attribute = AsyncMock(
                side_effect=lambda name: {
                    "type": "text",
                    "name": "birthday",
                    "placeholder": "MM/DD/YYYY",
                }.get(name)
            )
            inputs = MagicMock()
            inputs.count = AsyncMock(return_value=1)
            inputs.first = field
            page = MagicMock()
            page.locator.return_value = inputs

            with patch.object(
                register_chatgpt,
                "_keyboard_fill_control",
                AsyncMock(return_value=True),
            ) as fill, patch.object(
                register_chatgpt, "_blur_control", AsyncMock()
            ), patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()):
                result = await register_chatgpt.fill_birthday_fields(
                    page, "Enter your date of birth"
                )
            return result, fill

        result, fill = asyncio.run(exercise())
        self.assertEqual(result, (True, True))
        self.assertEqual(fill.await_args.args[2], "06/15/1995")

    def test_segmented_number_birthday_fields_are_not_treated_as_age(self):
        async def exercise():
            fields = []
            for name in ("birthdayMonth", "birthdayDay", "birthdayYear"):
                field = MagicMock()
                field.get_attribute = AsyncMock(
                    side_effect=lambda attr, value=name: value if attr == "name" else None
                )
                fields.append(field)
            inputs = MagicMock()
            inputs.count = AsyncMock(return_value=3)
            inputs.nth.side_effect = fields
            page = MagicMock()
            page.locator.return_value = inputs

            with patch.object(
                register_chatgpt,
                "_keyboard_fill_control",
                AsyncMock(return_value=True),
            ) as fill, patch.object(
                register_chatgpt, "_blur_control", AsyncMock()
            ):
                result = await register_chatgpt.fill_birthday_fields(
                    page, "Birthday"
                )
            return result, fill

        result, fill = asyncio.run(exercise())
        self.assertEqual(result, (True, True))
        self.assertEqual(
            [call.args[2] for call in fill.await_args_list],
            ["06", "15", "1995"],
        )

    def test_react_aria_birthday_segments_are_filled(self):
        async def exercise():
            segments_list = []
            for label in ("Month", "Day", "Year"):
                segment = MagicMock()
                segment.get_attribute = AsyncMock(
                    side_effect=lambda attr, value=label: value if attr == "aria-label" else None
                )
                segments_list.append(segment)

            empty = MagicMock()
            empty.count = AsyncMock(return_value=0)
            segments = MagicMock()
            segments.count = AsyncMock(return_value=3)
            segments.nth.side_effect = segments_list
            page = MagicMock()

            def locator(selector):
                if selector == register_chatgpt._BIRTHDAY_SEGMENT_SELECTOR:
                    return segments
                return empty

            page.locator.side_effect = locator
            with patch.object(
                register_chatgpt,
                "_keyboard_fill_control",
                AsyncMock(return_value=True),
            ) as fill, patch.object(
                register_chatgpt, "_blur_control", AsyncMock()
            ):
                result = await register_chatgpt.fill_birthday_fields(
                    page, "Let's confirm your age Birthday 08/04/2026"
                )
            return result, fill

        result, fill = asyncio.run(exercise())
        self.assertEqual(result, (True, True))
        self.assertEqual(
            [call.args[2] for call in fill.await_args_list],
            ["6", "15", "1995"],
        )

    def test_auth_step_waits_through_blank_redirect(self):
        page = MagicMock()
        page.url = "https://chatgpt.com/auth/login"
        email = MagicMock()
        email.first = email
        email.count = AsyncMock(return_value=0)
        email.is_visible = AsyncMock(return_value=False)
        code = MagicMock()
        code.first = code
        code.count = AsyncMock(side_effect=[0, 1])
        code.is_visible = AsyncMock(return_value=True)
        password = MagicMock()
        password.first = password
        password.count = AsyncMock(return_value=0)
        password.is_visible = AsyncMock(return_value=False)
        body = MagicMock()
        body.inner_text = AsyncMock(return_value="")

        def locator(selector):
            if 'type="email"' in selector:
                return email
            if 'name="code"' in selector:
                return code
            if 'type="password"' in selector:
                return password
            return body

        page.locator.side_effect = locator
        with (
            patch.object(register_chatgpt, "detect_challenge", AsyncMock(return_value=False)),
            patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
        ):
            step = asyncio.run(
                register_chatgpt.wait_for_chatgpt_auth_step(page, timeout=1)
            )

        self.assertEqual(step, "code")

    def test_stuck_email_submit_reopens_clean_login_and_advances(self):
        async def exercise():
            field = MagicMock()
            field.first = field
            field.count = AsyncMock(return_value=1)
            field.is_visible = AsyncMock(return_value=True)
            field.press = AsyncMock()
            button = MagicMock()
            button.first = button
            button.count = AsyncMock(return_value=1)
            button.click = AsyncMock()
            page = MagicMock()
            page.locator.side_effect = lambda selector: (
                field if 'type="email"' in selector or 'name="email"' in selector
                else button
            )
            page.goto = AsyncMock()
            with (
                patch.object(register_chatgpt, "dismiss_cookie_banner", AsyncMock()),
                patch.object(register_chatgpt, "fill_email_verified", AsyncMock(return_value=True)),
                patch.object(register_chatgpt, "click_any_exact", AsyncMock(return_value=True)),
                patch.object(register_chatgpt, "wait_for_chatgpt_auth_step", AsyncMock(return_value="code")),
                patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
            ):
                result = await register_chatgpt.recover_stuck_chatgpt_email_submit(
                    page, "user@example.com"
                )
            return result, page

        result, page = asyncio.run(exercise())
        self.assertEqual(result, "code")
        page.goto.assert_awaited_once_with(
            register_chatgpt.SIGNUP_URL,
            timeout=60000,
            wait_until="domcontentloaded",
        )

    def test_hidden_turnstile_response_is_detected_as_challenge(self):
        challenge = MagicMock()
        challenge.count = AsyncMock(return_value=1)
        page = MagicMock()
        page.locator.return_value = challenge

        detected = asyncio.run(register_chatgpt.detect_challenge(page))

        self.assertTrue(detected)
        self.assertIn(
            'input[name="cf-turnstile-response"]',
            page.locator.call_args.args[0],
        )

    def test_onboarding_rejection_propagates_from_finish_click(self):
        button = MagicMock()
        button.first = button
        button.count = AsyncMock(return_value=1)
        button.get_attribute = AsyncMock(return_value=None)
        button.click = AsyncMock()
        page = MagicMock()
        page.url = "https://auth.openai.com/about-you"
        page.get_by_role.return_value = button
        auth_monitor = MagicMock()
        auth_monitor.clear = AsyncMock()

        with (
            patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
            patch.object(
                register_chatgpt,
                "_raise_onboarding_error",
                AsyncMock(
                    side_effect=register_chatgpt.OnboardingRejected(
                        "unsupported_email: domain rejected"
                    )
                ),
            ),
        ):
            with self.assertRaises(register_chatgpt.OnboardingRejected):
                asyncio.run(
                    register_chatgpt.click_finish_button(
                        page,
                        0,
                        'input[name="birthday"]',
                        auth_monitor=auth_monitor,
                        max_wait=0.1,
                    )
                )

    def setUp(self):
        self.proxy_env = patch.dict(os.environ, {"PROXY_MODE": "clash_auto"})
        self.proxy_env.start()
        self.addCleanup(self.proxy_env.stop)

    def test_visible_email_form_means_submission_did_not_advance(self):
        page = MagicMock()
        email_input = MagicMock()
        email_input.count = AsyncMock(return_value=1)
        email_input.is_visible = AsyncMock(return_value=True)
        page.locator.return_value.first = email_input

        advanced = asyncio.run(
            register_chatgpt.chatgpt_email_submission_advanced(page)
        )

        self.assertFalse(advanced)

    def test_missing_email_form_means_submission_advanced(self):
        page = MagicMock()
        email_input = MagicMock()
        email_input.count = AsyncMock(return_value=0)
        page.locator.return_value.first = email_input

        advanced = asyncio.run(
            register_chatgpt.chatgpt_email_submission_advanced(page)
        )

        self.assertTrue(advanced)

    def test_browser_mail_fallback_only_runs_on_last_graph_attempt(self):
        self.assertFalse(
            register_chatgpt.should_use_browser_mail_fallback(True, 0)
        )
        self.assertFalse(
            register_chatgpt.should_use_browser_mail_fallback(True, 1)
        )
        self.assertTrue(
            register_chatgpt.should_use_browser_mail_fallback(True, 2)
        )
        self.assertFalse(
            register_chatgpt.should_use_browser_mail_fallback(False, 2)
        )

    def test_stuck_onboarding_recovers_when_session_and_main_ui_exist(self):
        page = MagicMock()
        page.goto = AsyncMock()
        probe = MagicMock()
        probe.goto = AsyncMock()
        probe.close = AsyncMock()
        composer = MagicMock()
        composer.count = AsyncMock(return_value=1)
        probe.locator.return_value = composer
        page.context.new_page = AsyncMock(return_value=probe)

        with (
            patch(
                "common.session_export.fetch_chatgpt_session",
                AsyncMock(return_value={"accessToken": "token"}),
            ),
            patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
        ):
            recovered = asyncio.run(
                register_chatgpt.recover_stuck_onboarding_session(page)
            )

        self.assertTrue(recovered)
        page.goto.assert_awaited_once()
        probe.close.assert_awaited_once()

    def test_required_onboarding_consents_are_checked(self):
        boxes = []
        for _ in range(3):
            box = MagicMock()
            box.is_checked = AsyncMock(side_effect=[False, True])
            box.check = AsyncMock()
            boxes.append(box)
        locator = MagicMock()
        locator.count = AsyncMock(return_value=3)
        locator.nth.side_effect = boxes
        page = MagicMock()
        page.locator.return_value = locator

        with patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()):
            total, checked = asyncio.run(
                register_chatgpt.ensure_required_onboarding_consents(page)
            )

        self.assertEqual((total, checked), (3, 3))
        for box in boxes:
            box.check.assert_awaited_once_with(force=True, timeout=4000)

    def test_onboarding_consent_helper_ignores_optional_only_page(self):
        locator = MagicMock()
        locator.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.locator.return_value = locator

        total, checked = asyncio.run(
            register_chatgpt.ensure_required_onboarding_consents(page)
        )

        self.assertEqual((total, checked), (0, 0))

    def test_cookie_buttons_include_german_accept_labels(self):
        self.assertIn("Annehmen", register_chatgpt._COOKIE_BTNS)
        self.assertIn("Alle akzeptieren", register_chatgpt._COOKIE_BTNS)
        self.assertNotIn("Ablehnen", register_chatgpt._COOKIE_BTNS)

    def test_browser_profile_uses_configured_clash_proxy(self):
        with patch.dict(
            os.environ,
            {
                "PROXY_MODE": "clash_auto",
                "CLASH_PROXY": "http://proxy-user:proxy-pass@127.0.0.1:7897",
            },
            clear=True,
        ):
            fields = register_chatgpt.clash_browser_proxy_fields()

        self.assertEqual(fields["proxyType"], "http")
        self.assertEqual(fields["host"], "127.0.0.1")
        self.assertEqual(fields["port"], "7897")
        self.assertEqual(fields["proxyUserName"], "proxy-user")
        self.assertEqual(fields["proxyPassword"], "proxy-pass")

    def test_region_rejection_is_parsed_from_auth_response(self):
        error = register_chatgpt._openai_error_from_text(
            '{"error":{"code":"unsupported_country_region_territory",'
            '"message":"Country, region, or territory not supported"}}',
            status=403,
            url="/api/accounts/create",
        )

        self.assertEqual(error["code"], "unsupported_country_region_territory")
        self.assertEqual(error["status"], 403)

    def test_email_verification_html_route_error_is_detected(self):
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                'Route Error (400 Invalid content type: text/html; charset=UTF-8)'
            )
        )
        page = MagicMock()
        page.locator.return_value = body

        detected = asyncio.run(
            register_chatgpt.is_email_verification_route_error(page)
        )

        self.assertTrue(detected)

    def test_email_verification_route_error_clicks_retry(self):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        code_input = MagicMock()
        code_input.first = code_input
        code_input.count = AsyncMock(side_effect=[1, 0])
        page.locator.return_value = code_input

        async def click_retry(_page, labels, **_kwargs):
            if "重试" in labels:
                page.url = "https://chatgpt.com/"
                return True
            return False

        with (
            patch.object(
                register_chatgpt,
                "_fill_and_submit_email_code",
                AsyncMock(return_value=True),
            ),
            patch.object(
                register_chatgpt,
                "is_email_verification_route_error",
                AsyncMock(side_effect=[True, False]),
            ),
            patch.object(
                register_chatgpt,
                "click_any_exact",
                AsyncMock(side_effect=click_retry),
            ) as click,
            patch.object(register_chatgpt, "dump_state", AsyncMock()),
            patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
        ):
            asyncio.run(
                register_chatgpt.submit_email_verification_code(
                    page, 'input[name="code"]', "519907"
                )
            )

        self.assertTrue(any("重试" in call.args[1] for call in click.await_args_list))

    def test_email_verification_does_not_submit_an_uncommitted_code(self):
        code_input = MagicMock()
        code_input.first = code_input
        code_input.count = AsyncMock(return_value=1)
        page = MagicMock()
        page.locator.return_value = code_input

        with patch.object(
            register_chatgpt, "react_fill", AsyncMock(return_value=False)
        ), patch.object(
            register_chatgpt, "click_any_exact", AsyncMock()
        ) as click:
            submitted = asyncio.run(
                register_chatgpt._fill_and_submit_email_code(
                    page, 'input[name="code"]', "134832"
                )
            )

        self.assertFalse(submitted)
        click.assert_not_awaited()

    def test_stuck_verification_click_uses_native_form_submit(self):
        code_input = MagicMock()
        code_input.first = code_input
        code_input.count = AsyncMock(return_value=1)
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        page.locator.return_value = code_input
        page.reload = AsyncMock()

        async def submit(_page, _selector, _code, **kwargs):
            if kwargs.get("submit_method") == "request_submit":
                page.url = "https://auth.openai.com/about-you"
            return True

        with patch.object(
            register_chatgpt,
            "_fill_and_submit_email_code",
            AsyncMock(side_effect=submit),
        ) as fill_submit, patch.object(
            register_chatgpt,
            "is_email_verification_route_error",
            AsyncMock(return_value=False),
        ), patch.object(
            register_chatgpt, "dump_state", AsyncMock()
        ), patch.object(
            register_chatgpt.asyncio, "sleep", AsyncMock()
        ):
            asyncio.run(
                register_chatgpt.submit_email_verification_code(
                    page, 'input[name="code"]', "134832"
                )
            )

        methods = [call.kwargs.get("submit_method", "click") for call in fill_submit.await_args_list]
        self.assertEqual(methods, ["click", "request_submit"])
        page.reload.assert_not_awaited()

    def test_stuck_native_verification_submit_requests_a_fresh_code(self):
        code_input = MagicMock()
        code_input.first = code_input
        code_input.count = AsyncMock(return_value=1)
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        page.locator.return_value = code_input
        page.reload = AsyncMock()

        async def submit(_page, _selector, _code, **kwargs):
            return True

        with patch.object(
            register_chatgpt,
            "_fill_and_submit_email_code",
            AsyncMock(side_effect=submit),
        ) as fill_submit, patch.object(
            register_chatgpt,
            "is_email_verification_route_error",
            AsyncMock(return_value=False),
        ), patch.object(
            register_chatgpt, "dump_state", AsyncMock()
        ), patch.object(
            register_chatgpt.asyncio, "sleep", AsyncMock()
        ):
            with self.assertRaises(register_chatgpt.EmailVerificationRetryNeeded):
                asyncio.run(
                    register_chatgpt.submit_email_verification_code(
                        page, 'input[name="code"]', "134832"
                    )
                )

        methods = [call.kwargs.get("submit_method", "click") for call in fill_submit.await_args_list]
        self.assertEqual(methods, ["click", "request_submit"])
        page.reload.assert_not_awaited()

    def test_email_verified_success_screen_is_not_treated_as_failure(self):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        body = MagicMock()
        body.inner_text = AsyncMock(
            return_value=(
                "Email verified Your email (user@example.com) "
                "has already been verified"
            )
        )
        page.locator.return_value = body

        with patch.object(
            register_chatgpt,
            "_fill_and_submit_email_code",
            AsyncMock(return_value=True),
        ) as submit, patch.object(
            register_chatgpt, "dump_state", AsyncMock()
        ), patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()):
            asyncio.run(
                register_chatgpt.submit_email_verification_code(
                    page, 'input[name="code"]', "994250"
                )
            )

        submit.assert_awaited_once()

    def test_email_verification_reports_service_rejection(self):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        code_input = MagicMock()
        code_input.first = code_input
        code_input.count = AsyncMock(return_value=1)
        page.locator.return_value = code_input
        monitor = MagicMock()
        monitor.clear = AsyncMock()
        monitor.latest = AsyncMock(return_value={
            "code": "invalid_code",
            "message": "Verification code is invalid or expired",
            "status": 400,
            "url": "/email-verification",
        })

        with patch.object(
            register_chatgpt,
            "_fill_and_submit_email_code",
            AsyncMock(return_value=True),
        ), patch.object(
            register_chatgpt,
            "is_email_verification_route_error",
            AsyncMock(return_value=False),
        ), patch.object(
            register_chatgpt, "dump_state", AsyncMock()
        ), patch.object(
            register_chatgpt.asyncio, "sleep", AsyncMock()
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid_code"):
                asyncio.run(
                    register_chatgpt.submit_email_verification_code(
                        page,
                        'input[name="code"]',
                        "134832",
                        auth_monitor=monitor,
                    )
                )

        monitor.clear.assert_awaited_once_with()
        monitor.latest.assert_awaited_once_with()

    def test_blank_codex_numeric_env_uses_default(self):
        with patch.dict(os.environ, {"CODEX_SMS_TIMEOUT": ""}):
            self.assertEqual(register_chatgpt._env_int("CODEX_SMS_TIMEOUT", 150), 150)

    def test_oauth_can_continue_when_cookie_less_probe_is_blocked(self):
        with patch.object(register_chatgpt, "_active_cf_nodes", []):
            with patch.object(register_chatgpt, "CF_NODES", ["node-a"]):
                with patch.object(register_chatgpt.time, "sleep"):
                    with patch.object(register_chatgpt, "_activate_cf_node", return_value="node-a"):
                        with patch.object(
                            register_chatgpt,
                            "_probe_chatgpt_node",
                            return_value=(False, "JP", 403),
                        ):
                            selected = register_chatgpt.select_chatgpt_node(
                                "auto", allow_blocked=True
                            )
        self.assertEqual(selected, "node-a")

    def test_auto_nodes_are_discovered_from_current_clash_group(self):
        catalog = {
            "GLOBAL": {
                "type": "Selector",
                "all": [
                    "DIRECT",
                    "剩余流量：10 GB",
                    "🇯🇵 日本 | 01",
                    "nested-group",
                    "🇸🇬 新加坡 | 01",
                ],
            },
            "🇯🇵 日本 | 01": {"type": "VLESS"},
            "nested-group": {"type": "Selector"},
            "🇸🇬 新加坡 | 01": {"type": "Trojan"},
        }

        with patch.object(register_chatgpt, "_active_cf_nodes", []):
            with patch.object(register_chatgpt, "CF_NODES", []):
                with patch("_clash_verge.ClashClient") as client_class:
                    client_class.return_value.proxies.return_value = {
                        "proxies": catalog
                    }
                    candidates = register_chatgpt._chatgpt_node_candidates()

        self.assertEqual(candidates, ["🇯🇵 日本 | 01", "🇸🇬 新加坡 | 01"])

    def test_auto_selection_uses_discovered_node_names(self):
        probes = [(False, "JP", 403), (True, "SG", 200)]
        with patch.object(register_chatgpt, "_active_cf_nodes", []):
            with patch.object(
                register_chatgpt,
                "_discover_chatgpt_nodes",
                return_value=["🇯🇵 日本 | 01", "🇸🇬 新加坡 | 01"],
            ):
                with patch.object(register_chatgpt.time, "sleep"):
                    with patch.object(
                        register_chatgpt,
                        "_activate_cf_node",
                        side_effect=lambda node: node,
                    ) as activate:
                        with patch.object(
                            register_chatgpt,
                            "_probe_chatgpt_node",
                            side_effect=probes,
                        ):
                            selected = register_chatgpt.select_chatgpt_node("auto")

        self.assertEqual(selected, "🇸🇬 新加坡 | 01")
        self.assertEqual(
            [call.args[0] for call in activate.call_args_list],
            ["🇯🇵 日本 | 01", "🇸🇬 新加坡 | 01"],
        )

    def test_country_selection_skips_usable_wrong_country(self):
        probes = [(True, "SG", 200), (True, "JP", 200)]
        with patch.object(register_chatgpt.proxy_switch, "proxy_mode", return_value="clash_auto"):
            with patch.object(register_chatgpt, "_chatgpt_node_candidates", return_value=["sg-node", "jp-node"]):
                with patch.object(register_chatgpt.time, "sleep"):
                    with patch.object(register_chatgpt, "_activate_cf_node", side_effect=lambda node: node):
                        with patch.object(register_chatgpt, "_probe_chatgpt_node", side_effect=probes):
                            selected = register_chatgpt.select_chatgpt_node(
                                "auto", country="jp"
                            )

        self.assertEqual(selected, "jp-node")
        self.assertEqual(register_chatgpt._get_active_chatgpt_country(), "JP")

    def test_country_code_validation_rejects_non_iso_value(self):
        self.assertEqual(register_chatgpt._normalize_chatgpt_country("sg"), "SG")
        self.assertEqual(register_chatgpt._normalize_chatgpt_country("any"), "auto")
        with self.assertRaises(ValueError):
            register_chatgpt._normalize_chatgpt_country("Japan")

    def test_direct_browser_mode_explicitly_disables_proxy(self):
        with patch.object(register_chatgpt, "CHATGPT_NODE", "none"):
            fields = register_chatgpt.chatgpt_browser_proxy_fields()

        self.assertEqual(fields["proxyType"], "noproxy")

    def test_worker_country_check_uses_direct_probe_for_direct_node(self):
        with patch.object(register_chatgpt, "CHATGPT_NODE", "direct"):
            with patch.object(register_chatgpt, "CHATGPT_COUNTRY", "US"):
                with patch.object(register_chatgpt.proxy_switch, "proxy_mode", return_value="clash_auto"):
                    with patch.object(
                        register_chatgpt,
                        "_probe_chatgpt_node",
                        return_value=(True, "US", 200),
                    ) as probe:
                        country = register_chatgpt.ensure_chatgpt_worker_country()

        self.assertEqual(country, "US")
        probe.assert_called_once_with(direct=True)

    def test_residential_auto_selection_defers_http_403_to_browser(self):
        with patch.object(
            register_chatgpt.proxy_switch, "proxy_mode", return_value="residential"
        ), patch.object(
            register_chatgpt, "_probe_chatgpt_node", return_value=(False, "US", 403)
        ) as probe, patch.object(
            register_chatgpt.proxy_switch, "rotate_proxy"
        ) as rotate:
            selected = register_chatgpt.select_chatgpt_node(
                "auto", country="auto"
            )

        self.assertIsNone(selected)
        self.assertEqual(register_chatgpt._get_active_chatgpt_country(), "US")
        probe.assert_called_once_with()
        rotate.assert_not_called()

    def test_worker_auto_country_defers_http_403_to_browser(self):
        with patch.object(register_chatgpt, "CHATGPT_NODE", "auto"), patch.object(
            register_chatgpt, "CHATGPT_COUNTRY", "auto"
        ), patch.object(
            register_chatgpt.proxy_switch, "proxy_mode", return_value="residential"
        ), patch.object(
            register_chatgpt, "_probe_chatgpt_node", return_value=(False, "US", 403)
        ) as probe, patch.object(
            register_chatgpt.proxy_switch, "rotate_proxy"
        ) as rotate:
            country = register_chatgpt.ensure_chatgpt_worker_country()

        self.assertEqual(country, "US")
        probe.assert_called_once_with(direct=False)
        rotate.assert_not_called()

    def test_clash_worker_auto_country_defers_transient_http_403(self):
        with patch.object(register_chatgpt, "CHATGPT_NODE", "auto"), patch.object(
            register_chatgpt, "CHATGPT_COUNTRY", "auto"
        ), patch.object(
            register_chatgpt.proxy_switch,
            "proxy_mode",
            return_value="clash_auto",
        ), patch.object(
            register_chatgpt,
            "_probe_chatgpt_node",
            return_value=(False, "US", 403),
        ):
            country = register_chatgpt.ensure_chatgpt_worker_country()

        self.assertEqual(country, "US")

    def test_worker_explicit_country_keeps_http_403_strict(self):
        with patch.object(register_chatgpt, "CHATGPT_NODE", "auto"), patch.object(
            register_chatgpt, "CHATGPT_COUNTRY", "US"
        ), patch.object(
            register_chatgpt.proxy_switch, "proxy_mode", return_value="residential"
        ), patch.object(
            register_chatgpt, "_probe_chatgpt_node", return_value=(False, "US", 403)
        ), patch.object(
            register_chatgpt.proxy_switch,
            "rotate_proxy",
            return_value={"ok": False, "changed": False},
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                register_chatgpt.ensure_chatgpt_worker_country()

    def test_auto_nodes_interleave_regions_before_applying_probe_limit(self):
        candidates = [
            "🇯🇵 日本 | 01",
            "🇯🇵 日本 | 02",
            "🇸🇬 新加坡 | 01",
            "🇺🇸 美国 | 01",
            "other-node",
        ]

        self.assertEqual(
            register_chatgpt._order_chatgpt_nodes(candidates),
            [
                "🇯🇵 日本 | 01",
                "🇸🇬 新加坡 | 01",
                "🇺🇸 美国 | 01",
                "🇯🇵 日本 | 02",
                "other-node",
            ],
        )

    def test_three_platform_command_pins_chatgpt_node(self):
        args = argparse.Namespace(
            timeout=600,
            node="level1-test-node",
            chatgpt_country="JP",
            keep_on_fail=False,
            import_c2a=False,
            codex=False,
        )
        command = register_three_platforms.build_command(
            "chatgpt",
            args,
            ("mail@example.com", "password", "refresh-token", "client-id"),
        )

        node_index = command.index("--node")
        self.assertEqual(command[node_index + 1], "level1-test-node")
        country_index = command.index("--country")
        self.assertEqual(command[country_index + 1], "JP")

    def test_three_platform_claude_command_keeps_matching_client_id(self):
        args = argparse.Namespace(timeout=600, node="auto")
        command = register_three_platforms.build_command(
            "claude",
            args,
            ("mail@outlook.com", "password", "refresh-token", "client-id"),
        )

        self.assertEqual(command[command.index("--token") + 1], "refresh-token")
        self.assertEqual(command[command.index("--client-id") + 1], "client-id")

    def test_three_platform_forwards_claude_retry_tuning(self):
        args = argparse.Namespace(
            timeout=600,
            node="auto",
            claude_challenge_wait=21,
            claude_challenge_node_retries=5,
            claude_captcha_manual_timeout=90,
        )
        command = register_three_platforms.build_command(
            "claude",
            args,
            ("mail@outlook.com", "password", "refresh-token", "client-id"),
        )

        self.assertEqual(command[command.index("--challenge-wait") + 1], "21")
        self.assertEqual(
            command[command.index("--challenge-node-retries") + 1], "5"
        )
        self.assertEqual(
            command[command.index("--captcha-manual-timeout") + 1], "90"
        )

    def test_three_platform_forwards_custom_codex_provider(self):
        args = argparse.Namespace(
            timeout=600,
            node="auto",
            chatgpt_country="JP",
            keep_on_fail=False,
            import_c2a=False,
            codex=True,
            codex_group="codex-prod",
            codex_manual_phone=False,
            codex_sms_provider="custom",
            codex_timeout=321,
        )
        command = register_three_platforms.build_command(
            "chatgpt",
            args,
            ("mail@example.com", "password", "refresh-token", "client-id"),
        )

        self.assertEqual(
            command[command.index("--codex-sms-provider") + 1], "custom"
        )
        self.assertEqual(command[command.index("--codex-timeout") + 1], "321")

    def test_multi_platform_command_runs_full_github_registration(self):
        args = argparse.Namespace(timeout=600, keep_on_fail=False)
        command = register_three_platforms.build_command(
            "github",
            args,
            ("mail@example.com", "password", "refresh-token", "client-id"),
        )
        self.assertIn("register_github.py", command)
        self.assertIn("--auto", command)
        self.assertIn("--no-keep", command)

    def test_platform_failure_returns_nonzero(self):
        results = [("chatgpt", False, 1, "chatgpt.log")]
        self.assertEqual(register_three_platforms.results_exit_code(results), 1)
        self.assertEqual(
            register_three_platforms.results_exit_code(
                [("chatgpt", True, 0, "chatgpt.log")]
            ),
            0,
        )

    def test_end_to_end_chatgpt_gets_one_same_mailbox_recovery_attempt(self):
        args = argparse.Namespace(platform_retries=0)
        self.assertEqual(register_three_platforms.platform_retry_count("chatgpt", args), 1)
        self.assertEqual(register_three_platforms.platform_retry_count("claude", args), 0)
        args.platform_retries = 2
        self.assertEqual(register_three_platforms.platform_retry_count("chatgpt", args), 2)

    def test_full_flow_redacts_credentials(self):
        rendered = run_full_flow.redact_command(
            ["python", "child.py", "--password", "mail-pass", "--token", "graph-token"]
        )
        self.assertNotIn("mail-pass", rendered)
        self.assertNotIn("graph-token", rendered)
        self.assertEqual(rendered.count("***"), 2)

    def test_full_flow_platform_stage_is_parallel_by_default(self):
        args = argparse.Namespace(
            platforms=["claude", "chatgpt"],
            node="auto",
            chatgpt_country="auto",
            platform_timeout=600,
            broker="",
            keep_on_fail=False,
            import_c2a=False,
            plus_subscription=False,
            codex=False,
            grok_sub2api=False,
            dry_run=True,
            sequential_platforms=False,
        )
        with patch.object(run_full_flow, "log") as logger:
            self.assertEqual(
                run_full_flow.stage_platforms(args, {}, "mail@example.com", "secret"),
                0,
            )
        command_line = " ".join(call.args[0] for call in logger.call_args_list)
        self.assertIn("--parallel", command_line)

    def test_full_flow_assigns_one_platform_per_mailbox(self):
        self.assertEqual(
            [run_full_flow.platform_for_slot(["claude", "chatgpt", "grok"], i) for i in range(5)],
            ["claude", "chatgpt", "grok", "claude", "chatgpt"],
        )
        args = argparse.Namespace(
            concurrency=2,
            password="fallback",
            platforms=["claude", "chatgpt"],
        )
        accounts = [
            ("one@example.com", "pass-1", "", ""),
            ("two@example.com", "pass-2", "", ""),
        ]
        observed = {}

        def capture(account_args, _env, email, *_credentials):
            observed[email] = list(account_args.platforms)
            return 0

        with patch.object(run_full_flow, "stage_emails", return_value=accounts), \
                patch.object(run_full_flow, "stage_platforms", side_effect=capture):
            results = run_full_flow.run_wave(args, {}, 2)

        self.assertEqual({result[0] for result in results}, {0})
        self.assertEqual(observed, {
            "one@example.com": ["claude"],
            "two@example.com": ["chatgpt"],
        })

    def test_full_flow_forwards_stage_retry_and_sms_configuration(self):
        args = argparse.Namespace(
            platforms=["claude", "chatgpt"],
            node="auto",
            chatgpt_country="JP",
            platform_timeout=700,
            platform_retries=4,
            broker="",
            grok_timeout=31,
            keep_on_fail=False,
            import_c2a=False,
            plus_subscription=False,
            codex=True,
            codex_group="codex-prod",
            codex_manual_phone=False,
            codex_sms_provider="custom",
            codex_timeout=333,
            codex_phone_skip=1,
            codex_phone_attempts=5,
            codex_sms_timeout=222,
            sms_get_phone_retries=7,
            custom_sms_pool_file="runtime/custom-pool.json",
            custom_sms_allowed_hosts="xsd20vip.com",
            grok_sub2api=False,
            grok_mailbox_attempts=8,
            claude_profile_retries=6,
            claude_hcaptcha_retries=4,
            claude_challenge_wait=52,
            claude_challenge_node_retries=9,
            claude_captcha_manual_timeout=120,
            kiro_account_password="",
            kiro_full_name="Batch User",
            dry_run=True,
            sequential_platforms=False,
        )
        with patch.object(run_full_flow, "log") as logger:
            self.assertEqual(
                run_full_flow.stage_platforms(
                    args, {}, "mail@example.com", "secret"
                ),
                0,
            )

        command_line = next(
            call.args[0]
            for call in logger.call_args_list
            if call.args[0].startswith("Stage B cmd:")
        )
        for expected in (
            "--platform-retries 4",
            "--codex-sms-provider custom",
            "--codex-phone-attempts 5",
            "--codex-sms-timeout 222",
            "--sms-get-phone-retries 7",
            "--custom-sms-pool-file runtime/custom-pool.json",
            "--custom-sms-allowed-hosts xsd20vip.com",
            "--claude-profile-retries 6",
            "--claude-hcaptcha-retries 4",
            "--grok-mailbox-attempts 8",
            "--kiro-full-name Batch User",
        ):
            self.assertIn(expected, command_line)

    def test_full_flow_builds_child_environment_for_retry_and_custom_pool(self):
        args = argparse.Namespace(
            proxy="",
            clash_api="new-api",
            clash_secret="new-secret",
            clash_group="new-group",
            claude_profile_retries=6,
            claude_hcaptcha_retries=4,
            codex_phone_skip=1,
            codex_phone_attempts=5,
            codex_sms_timeout=222,
            sms_get_phone_retries=7,
            custom_sms_pool_file="runtime/custom-pool.json",
            custom_sms_allowed_hosts="xsd20vip.com",
        )
        with patch.dict(
            run_full_flow.os.environ,
            {
                "CLASH_API": "old-api",
                "CLASH_SECRET": "old-secret",
                "CLASH_GROUP": "old-group",
            },
            clear=True,
        ):
            env = run_full_flow.build_child_env(args)

        self.assertEqual(env["CLAUDE_RESIDENTIAL_PROFILE_RETRIES"], "6")
        self.assertEqual(env["CLAUDE_HCAPTCHA_SOLVE_RETRIES"], "4")
        self.assertEqual(env["CODEX_PHONE_SKIP_ATTEMPTS"], "1")
        self.assertEqual(env["CODEX_ADDPHONE_ATTEMPTS"], "5")
        self.assertEqual(env["CODEX_SMS_TIMEOUT"], "222")
        self.assertEqual(env["SMS_GETPHONE_RETRIES"], "7")
        self.assertEqual(env["CUSTOM_SMS_POOL_FILE"], "runtime/custom-pool.json")
        self.assertEqual(env["CUSTOM_SMS_ALLOWED_HOSTS"], "xsd20vip.com")
        self.assertEqual(env["CLASH_API"], "new-api")
        self.assertEqual(env["CLASH_SECRET"], "new-secret")
        self.assertEqual(env["CLASH_GROUP"], "new-group")

    def test_platform_process_retries_until_success_marker(self):
        def process(output, returncode):
            stream = MagicMock()
            stream.readline = AsyncMock(
                side_effect=[output.encode("utf-8"), b""]
            )
            proc = MagicMock(stdout=stream)
            proc.wait = AsyncMock(return_value=returncode)
            return proc

        first = process("registration failed\n", 1)
        second = process("success: 1/1\n", 0)
        with tempfile.TemporaryDirectory() as log_dir:
            with patch.object(register_three_platforms, "LOG_DIR", log_dir):
                with patch.object(
                    register_three_platforms.asyncio,
                    "create_subprocess_exec",
                    new=AsyncMock(side_effect=[first, second]),
                ) as create:
                    with patch.object(
                        register_three_platforms.asyncio,
                        "sleep",
                        new=AsyncMock(),
                    ):
                        result = asyncio.run(
                            register_three_platforms.run_platform(
                                "chatgpt",
                                ["python", "child.py"],
                                "retry-test",
                                child_env={},
                                retries=1,
                            )
                        )

        self.assertTrue(result[1])
        self.assertEqual(create.await_count, 2)
        self.assertTrue(result[3].endswith("retry-test_chatgpt_attempt2.log"))

    def test_standalone_oauth_propagates_failure_exit_code(self):
        source = inspect.getsource(oauth_codex.main)
        self.assertNotIn("sys.exit(", source)
        module_source = inspect.getsource(oauth_codex)
        self.assertIn("sys.exit(asyncio.run(main()))", module_source)


if __name__ == "__main__":
    unittest.main()
