import unittest
from unittest.mock import patch

from register_github import (
    CLIENT_INTEGRITY, PAGE_BLANK, RESTRICTED, classify_github_entry, parse_github_restriction,
    prepare_github_egress, should_close_github_profile,
)
from webui.scripts import SCRIPTS


class GitHubRegistrationTests(unittest.TestCase):
    def test_restriction_page_extracts_id_and_ip(self):
        body = """Access is temporarily restricted
        We detected unusual activity from your device or network.
        Automated (bot) activity on your network (IP 47.164.113.31)
        ID: c3cd9f1c-4072-80e8-78f0-1397e8577bad
        """
        result = parse_github_restriction(body, "https://github.com/signup")
        self.assertEqual(result["restriction_id"], "c3cd9f1c-4072-80e8-78f0-1397e8577bad")
        self.assertEqual(result["ip"], "47.164.113.31")
        self.assertEqual(result["url"], "https://github.com/signup")

    def test_normal_signup_page_is_not_marked_restricted(self):
        self.assertIsNone(parse_github_restriction("Create account Email Password Username"))

    def test_secondary_rate_limit_is_restricted(self):
        state, detail = classify_github_entry(
            "Too many requests. You have exceeded a secondary rate limit.",
            title="github.com",
            url="https://github.com/signup",
        )
        self.assertEqual(state, RESTRICTED)
        self.assertEqual(detail["url"], "https://github.com/signup")

    def test_blank_entry_page_is_separate_from_layout_change(self):
        state, detail = classify_github_entry(
            "", title="", html_length=39, url="https://github.com/signup"
        )
        self.assertEqual(state, PAGE_BLANK)
        self.assertEqual(detail["html_length"], 39)

    def test_http_403_empty_page_is_restricted_not_blank(self):
        state, detail = classify_github_entry(
            "", title="github.com", html_length=1468,
            url="https://github.com/signup", http_status=403,
        )
        self.assertEqual(state, RESTRICTED)
        self.assertEqual(detail["http_status"], 403)
        self.assertEqual(detail["response_excerpt"], "")

    def test_http_403_response_excerpt_is_safe_and_bounded(self):
        state, detail = classify_github_entry(
            "Access denied " + "x" * 1000,
            title="github.com",
            html_length=1468,
            url="https://github.com/signup",
            http_status=403,
        )
        self.assertEqual(state, RESTRICTED)
        self.assertLessEqual(len(detail["response_excerpt"]), 240)

    def test_http_403_with_visible_signup_form_is_ready(self):
        state, detail = classify_github_entry(
            "Please enable JS and disable any ad blocker",
            title="github.com",
            html_length=50000,
            url="https://github.com/signup",
            http_status=403,
            has_signup_form=True,
        )
        self.assertEqual(state, "READY")
        self.assertTrue(detail["recovered_after_js"])

    def test_403_shell_does_not_imply_form_is_ready_without_form(self):
        state, _detail = classify_github_entry(
            "Skip to content",
            title="github.com",
            html_length=1468,
            url="https://github.com/signup",
            http_status=403,
            has_signup_form=False,
        )
        self.assertEqual(state, RESTRICTED)

    def test_js_or_adblock_shell_is_client_integrity_failure(self):
        state, detail = classify_github_entry(
            "Please enable JS and disable any ad blocker",
            title="github.com",
            html_length=1468,
            url="https://github.com/signup",
            http_status=403,
        )
        self.assertEqual(state, CLIENT_INTEGRITY)
        self.assertEqual(detail["http_status"], 403)

    def test_auto_node_keeps_responsive_current_clash_node(self):
        with patch("register_github.proxy_switch.proxy_mode", return_value="clash_auto"), patch(
            "register_github.proxy_switch.current_node", return_value="node-a"
        ), patch(
            "register_github.proxy_switch.node_delay", return_value=42
        ), patch("register_github.proxy_switch.rotate_proxy") as rotate:
            self.assertEqual(prepare_github_egress("auto"), "node-a")
        rotate.assert_not_called()

    def test_auto_node_rotates_only_when_current_node_is_unreachable(self):
        with patch("register_github.proxy_switch.proxy_mode", return_value="clash_auto"), patch(
            "register_github.proxy_switch.current_node", return_value="node-a"
        ), patch(
            "register_github.proxy_switch.node_delay", return_value=None
        ), patch(
            "register_github.proxy_switch.rotate_proxy",
            return_value={"ok": True, "node": "node-b"},
        ) as rotate:
            self.assertEqual(prepare_github_egress("auto"), "node-b")
        rotate.assert_called_once()

    def test_rendered_signup_page_is_ready(self):
        state, detail = classify_github_entry("Create account Email Password Username")
        self.assertEqual(state, "READY")
        self.assertEqual(detail, {})

    def test_client_integrity_can_keep_manual_handoff_profile(self):
        self.assertFalse(
            should_close_github_profile("bitbrowser", keep=True)
        )
        self.assertFalse(
            should_close_github_profile("bundled", keep=True)
        )
        self.assertTrue(
            should_close_github_profile("bundled", keep=False)
        )

    def test_cloak_profile_closes_when_task_event_loop_exits(self):
        self.assertTrue(should_close_github_profile("cloak", keep=True))

    def test_github_script_documents_restriction_stop_behavior(self):
        script = next(item for item in SCRIPTS if item["id"] == "register_github")
        self.assertIn("Access is temporarily restricted", script["warning"])
        self.assertIn("PAGE_BLANK", script["warning"])
        self.assertIn("CLIENT_INTEGRITY", script["warning"])
        self.assertIn("停止重试", script["warning"])
        self.assertEqual(RESTRICTED, "RESTRICTED")


if __name__ == "__main__":
    unittest.main()
