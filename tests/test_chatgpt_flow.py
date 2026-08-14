import argparse
import asyncio
import inspect
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import register_chatgpt
import register_three_platforms
import run_full_flow
import oauth_codex


class ChatGPTFlowTests(unittest.TestCase):
    def test_chatgpt_icloud_mailbox_is_service_filtered(self):
        with patch.object(
            register_chatgpt,
            "create_mailbox",
            return_value={"email": "code@example.com"},
        ) as create:
            mailbox = register_chatgpt.create_chatgpt_icloud_mailbox()

        self.assertEqual(mailbox["email"], "code@example.com")
        create.assert_called_once_with(
            provider="icloud", mail_type="icloud-code", service="openai"
        )

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

    def test_age_selector_does_not_capture_generic_number_inputs(self):
        self.assertNotIn('input[type="number"]', register_chatgpt._AGE_SELECTOR)
        self.assertIn('name="age"', register_chatgpt._AGE_SELECTOR)

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
                AsyncMock(side_effect=lambda _page, labels, **_kwargs: "重试" in labels),
            ) as click,
            patch.object(register_chatgpt, "dump_state", AsyncMock()),
            patch.object(register_chatgpt.asyncio, "sleep", AsyncMock()),
        ):
            page.url = "https://chatgpt.com/"
            asyncio.run(
                register_chatgpt.submit_email_verification_code(
                    page, 'input[name="code"]', "519907"
                )
            )

        self.assertTrue(any("重试" in call.args[1] for call in click.await_args_list))

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

    def test_standalone_oauth_propagates_failure_exit_code(self):
        source = inspect.getsource(oauth_codex.main)
        self.assertNotIn("sys.exit(", source)
        module_source = inspect.getsource(oauth_codex)
        self.assertIn("sys.exit(asyncio.run(main()))", module_source)


if __name__ == "__main__":
    unittest.main()
