import unittest
from unittest.mock import patch

import register
from tools import validate_keys
from webui.scripts import CHATGPT_COUNTRY_CHOICES, ENV_SCHEMA, SCRIPTS


def _script(script_id):
    return next(item for item in SCRIPTS if item["id"] == script_id)


class RegistrationSchemaTests(unittest.TestCase):
    def test_every_environment_variable_has_chinese_label_and_help(self):
        for group in ENV_SCHEMA:
            for item in group["items"]:
                self.assertTrue(item.get("label"), item["key"])
                self.assertTrue(item.get("help"), item["key"])
                self.assertRegex(item["label"], r"[\u4e00-\u9fff]", item["key"])

    def test_claude_validator_reuses_clash_and_modern_fingerprint(self):
        with patch.dict(
            validate_keys.os.environ,
            {
                "PROXY_MODE": "clash_auto",
                "CLASH_PROXY": "http://user:pass@127.0.0.1:7897",
                "CLAUDE_BROWSER_CORE_VERSION": "146",
            },
        ):
            options = validate_keys.validation_browser_options()

        self.assertEqual(options["proxyType"], "http")
        self.assertEqual(options["host"], "127.0.0.1")
        self.assertEqual(options["port"], "7897")
        self.assertEqual(options["proxyUserName"], "user")
        self.assertEqual(options["proxyPassword"], "pass")
        self.assertEqual(
            options["browserFingerPrint"]["coreVersion"], "146"
        )

    def test_claude_bitbrowser_uses_authenticated_residential_proxy(self):
        with patch.dict(
            register.os.environ,
            {
                "PROXY_MODE": "residential",
                "REG_FACTORY_PROXY": "http://resident:secret@home.test:9100",
            },
            clear=True,
        ):
            fields = register.claude_browser_proxy_fields()

        self.assertEqual(fields["proxyType"], "http")
        self.assertEqual(fields["host"], "home.test")
        self.assertEqual(fields["port"], "9100")
        self.assertEqual(fields["proxyUserName"], "resident")
        self.assertEqual(fields["proxyPassword"], "secret")

    def test_only_browser_grok_task_is_exposed(self):
        grok_tasks = [item for item in SCRIPTS if item["id"].startswith("register_grok")]
        self.assertEqual(len(grok_tasks), 1)
        self.assertEqual(grok_tasks[0]["file"], "register_grok.py")
        flags = {item["flag"] for item in grok_tasks[0]["args"]}
        self.assertIn("--sub2api", flags)
        self.assertIn("--sub2api-group", flags)

    def test_grok_browser_exposes_mailbox_rotation(self):
        args = {item["flag"]: item for item in _script("register_grok")["args"]}
        self.assertEqual(args["--mailbox-attempts"]["default"], 6)

    def test_chatgpt_exposes_fixed_node(self):
        args = {item["flag"]: item for item in _script("register_chatgpt")["args"]}
        self.assertEqual(args["--node"]["default"], "auto")
        self.assertEqual(args["--country"]["default"], "auto")
        self.assertIn("JP", args["--country"]["choices"])
        for script_id in ("run_full_flow", "register_three_platforms"):
            flow_args = {
                item["flag"]: item for item in _script(script_id)["args"]
            }
            self.assertEqual(flow_args["--chatgpt-country"]["default"], "auto")
        oauth_args = {item["flag"]: item for item in _script("oauth_codex")["args"]}
        self.assertEqual(oauth_args["--node"]["default"], "auto")

    def test_chatgpt_country_picker_exposes_complete_iso_list(self):
        self.assertEqual(len(CHATGPT_COUNTRY_CHOICES), 250)
        self.assertEqual(len(set(CHATGPT_COUNTRY_CHOICES)), 250)
        for country in ("AE", "BR", "IN", "JP", "ZA"):
            self.assertIn(country, CHATGPT_COUNTRY_CHOICES)
        for script_id, flag in (
            ("run_full_flow", "--chatgpt-country"),
            ("register_three_platforms", "--chatgpt-country"),
            ("register_chatgpt", "--country"),
        ):
            args = {item["flag"]: item for item in _script(script_id)["args"]}
            self.assertIs(args[flag]["choices"], CHATGPT_COUNTRY_CHOICES)
            self.assertTrue(args[flag]["countryNames"])

    def test_end_to_end_exposes_concurrent_parallel_pipeline(self):
        args = {item["flag"]: item for item in _script("run_full_flow")["args"]}
        self.assertEqual(args["--concurrency"]["default"], 1)
        self.assertFalse(args["--sequential-platforms"]["default"])
        self.assertEqual(args["--platform-retries"]["default"], 0)
        self.assertIn("github", args["--platforms"]["choices"])
        consumer_args = {
            item["flag"]: item
            for item in _script("register_three_platforms")["args"]
        }
        self.assertIn("--max-inflight", consumer_args)
        self.assertIn("github", consumer_args["--platforms"]["choices"])

    def test_end_to_end_exposes_complete_stage_tuning(self):
        flow_args = {
            item["flag"]: item for item in _script("run_full_flow")["args"]
        }
        expected = {
            "--platform-retries",
            "--round-sleep",
            "--email-timeout",
            "--email-total-timeout",
            "--max-press",
            "--broker",
            "--grok-timeout",
            "--keep-on-fail",
            "--claude-profile-retries",
            "--claude-hcaptcha-retries",
            "--claude-challenge-wait",
            "--claude-challenge-node-retries",
            "--claude-captcha-manual-timeout",
            "--codex-sms-provider",
            "--custom-sms-pool-file",
            "--custom-sms-allowed-hosts",
            "--codex-phone-skip",
            "--codex-phone-attempts",
            "--codex-sms-timeout",
            "--sms-get-phone-retries",
            "--codex-timeout",
            "--grok-mailbox-attempts",
            "--kiro-account-password",
            "--kiro-full-name",
            "--plus-subscription",
            "--proxy",
            "--clash-api",
            "--clash-secret",
            "--clash-group",
        }
        self.assertTrue(expected.issubset(flow_args))
        self.assertIn("custom", flow_args["--codex-sms-provider"]["choices"])
        self.assertIn("批量导入", flow_args["--custom-sms-pool-file"]["help"])
        self.assertIn("白名单", _script("run_full_flow").get("desc", "") + flow_args["--custom-sms-allowed-hosts"]["help"])

        consumer_args = {
            item["flag"]: item
            for item in _script("register_three_platforms")["args"]
        }
        stage_b_expected = expected - {
            "--round-sleep",
            "--email-timeout",
            "--email-total-timeout",
            "--max-press",
            "--proxy",
            "--clash-api",
            "--clash-secret",
            "--clash-group",
        }
        self.assertTrue(stage_b_expected.issubset(consumer_args))
        self.assertIn("custom", consumer_args["--codex-sms-provider"]["choices"])

    def test_chatgpt_promotes_direct_sub2api_import(self):
        script = _script("register_chatgpt")
        primary_flags = [item["flag"] for item in script["args"][:6]]
        args = {item["flag"]: item for item in script["args"]}

        self.assertIn("--email-provider", primary_flags)
        self.assertIn("--codex", primary_flags)
        self.assertIn("SUB2API", args["--codex"]["help"])
        self.assertIn("--codex-group", args)
        self.assertIn("custom", args["--codex-sms-provider"]["choices"])
        self.assertIn("--codex-phone", args)

        oauth_args = {item["flag"]: item for item in _script("oauth_codex")["args"]}
        self.assertIn("custom", oauth_args["--sms-provider"]["choices"])

    def test_bitbrowser_is_the_default_browser(self):
        browser_group = next(
            group for group in ENV_SCHEMA if group["group"] == "指纹浏览器"
        )
        items = {item["key"]: item for item in browser_group["items"]}

        self.assertEqual(items["FINGERPRINT_BROWSER"]["default"], "bitbrowser")
        self.assertNotIn("advanced", items["CUSTOM_BROWSER_API"])
        self.assertNotIn("advanced", items["CUSTOM_BROWSER_API_MODE"])
        self.assertNotIn("advanced", items["CUSTOM_BROWSER_API_KEY"])
        self.assertTrue(items["CUSTOM_BROWSER_API_CREATE_PATH"]["advanced"])
        self.assertTrue(items["CUSTOM_BROWSER_API_OPEN_METHOD"]["advanced"])
        self.assertEqual(
            items["FINGERPRINT_BROWSER"]["choices"][0], "bitbrowser"
        )

    def test_browser_task_warnings_describe_chromium_provider(self):
        outlook_warning = _script("outlook_reg_loop").get("warning", "")
        self.assertIn("Chromium", outlook_warning)
        self.assertIn("BitBrowser", outlook_warning)
        for script_id in ("register_claude", "register_grok"):
            warning = _script(script_id).get("warning", "")
            self.assertIn("Chromium CDP", warning)

    def test_claude_defaults_to_latest_rt(self):
        args = {item["flag"]: item for item in _script("register_claude")["args"]}
        self.assertTrue(args["--latest-rt"]["default"])
        self.assertIn("--client-id", args)
        self.assertIn("yyds", args["--provider"]["choices"])
        self.assertIn("--domain", args)
        self.assertLess(
            next(i for i, item in enumerate(_script("register_claude")["args"]) if item["flag"] == "--provider"),
            6,
        )
        self.assertEqual(args["--provider"]["labels"][""], "Outlook 资产池")
        self.assertEqual(args["--node"]["default"], "auto")
        self.assertEqual(args["--challenge-node-retries"]["default"], 3)
        self.assertEqual(args["--captcha-manual-timeout"]["default"], 0)

    def test_webui_exposes_claude_solver_configuration_only(self):
        script = _script("register_claude")
        self.assertIn("必须", script["warning"])
        self.assertIn("视觉 API", script["warning"])
        keys = {
            item["key"]
            for group in ENV_SCHEMA
            for item in group["items"]
        }
        self.assertTrue(
            {
                "CLAUDE_HCAPTCHA_SOLVE_RETRIES",
                "CLAUDE_RESIDENTIAL_PROFILE_RETRIES",
                "CLAUDE_ALLOW_ROTATING_PROXY",
                "CLAUDE_VISION_API_BASE",
                "CLAUDE_VISION_API_KEY",
                "CLAUDE_VISION_MODEL",
                "CLAUDE_NODE_PROBE_LIMIT",
                "CLAUDE_NODE_PROBE_TIMEOUT_SECONDS",
                "CLAUDE_BROWSER_CORE_VERSION",
            }.issubset(keys)
        )
        claude_items = {
            item["key"]: item
            for group in ENV_SCHEMA
            if group["group"] == "Claude 注册与验证"
            for item in group["items"]
        }
        self.assertTrue(claude_items["CLAUDE_VISION_API_BASE"]["required"])
        self.assertTrue(claude_items["CLAUDE_VISION_API_KEY"]["required"])

    def test_claude_graph_reader_receives_client_and_timestamp(self):
        with patch("common.mailbox.get_link_by_token", return_value="https://claude.ai/magic-link#ok") as reader:
            result = register.get_magic_link_by_token(
                "user@example.com",
                "refresh-token",
                client_id="client-id",
                max_wait=12,
                received_after=123.0,
            )
        self.assertEqual(result, "https://claude.ai/magic-link#ok")
        self.assertEqual(reader.call_args.kwargs["client_id"], "client-id")
        self.assertEqual(reader.call_args.kwargs["received_after"], 123.0)


if __name__ == "__main__":
    unittest.main()
