import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common import proxy_switch
import outlook_reg_loop
import run_full_flow


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeCurlRequests:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self.responses)


class ProxySwitchTests(unittest.TestCase):
    def test_concrete_nodes_excludes_subscription_metadata(self):
        with patch.dict(os.environ, {"PROXY_MODE": "clash_auto"}, clear=True):
            with patch.object(proxy_switch, "list_nodes", return_value=[
                "https://example.com",
                "level4-japan01",
            ]):
                self.assertEqual(proxy_switch.concrete_nodes(), ["level4-japan01"])

    def test_proxy_mode_keeps_legacy_residential_configuration(self):
        with patch.dict(os.environ, {"REG_FACTORY_PROXY": "http://proxy.test:8080"}, clear=True):
            self.assertEqual(proxy_switch.proxy_mode(), "residential")
            self.assertEqual(proxy_switch.effective_proxy_url(), "http://proxy.test:8080")

    def test_bitbrowser_fields_include_residential_credentials(self):
        env = {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "http://home-user:home-pass@home.test:9000",
        }
        fields = proxy_switch.browser_proxy_fields(env)
        self.assertEqual(fields, {
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "home.test",
            "port": "9000",
            "proxyUserName": "home-user",
            "proxyPassword": "home-pass",
        })

    def test_platform_modes_can_split_outlook_and_other_registrations(self):
        env = {
            "PROXY_MODE": "clash_auto",
            "CLASH_PROXY": "http://127.0.0.1:7897",
            "OUTLOOK_PROXY_MODE": "clash_auto",
            "CLAUDE_PROXY_MODE": "residential",
            "CHATGPT_PROXY_MODE": "residential",
            "GROK_PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "http://resident:secret@home.test:9000",
        }

        outlook = proxy_switch.platform_environment(env, "outlook")
        claude = proxy_switch.platform_environment(env, "claude")

        self.assertEqual(proxy_switch.proxy_mode(outlook), "clash_auto")
        self.assertEqual(outlook["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(proxy_switch.proxy_mode(claude), "residential")
        self.assertEqual(claude["HTTPS_PROXY"], "http://resident:secret@home.test:9000")
        self.assertEqual(proxy_switch.browser_proxy_fields(claude)["host"], "home.test")

    def test_outlook_loop_writes_residential_proxy_to_bitbrowser_profile(self):
        response = {"success": True, "data": {"id": "profile-1"}}
        with patch.dict(
            outlook_reg_loop.os.environ,
            {"FINGERPRINT_BROWSER": "bitbrowser"},
            clear=False,
        ):
            with patch.object(outlook_reg_loop, "_bb_call", return_value=response) as request:
                profile_id = outlook_reg_loop.bb_create_for_outlook_reg(
                    "outlook-test",
                    "http://resident:secret@home.test:9000",
                )

        self.assertEqual(profile_id, "profile-1")
        payload = request.call_args.args[1]
        self.assertEqual(payload["proxyType"], "http")
        self.assertEqual(payload["host"], "home.test")
        self.assertEqual(payload["port"], "9000")
        self.assertEqual(payload["proxyUserName"], "resident")
        self.assertEqual(payload["proxyPassword"], "secret")

    def test_outlook_loop_keeps_noproxy_profile_for_clash_tun(self):
        self.assertEqual(
            outlook_reg_loop._bitbrowser_proxy_fields(),
            {"proxyMethod": 2, "proxyType": "noproxy"},
        )

    def test_bitbrowser_fields_follow_residential_pool_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "PROXY_MODE": "residential",
                "REG_FACTORY_PROXY_POOL": "http://one.test:8001,http://two.test:8002",
                "REG_FACTORY_PROXY_STATE_FILE": os.path.join(directory, "index.txt"),
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(proxy_switch.browser_proxy_fields()["host"], "one.test")
                result = proxy_switch.rotate_proxy()
                self.assertTrue(result["ok"])
                self.assertEqual(proxy_switch.browser_proxy_fields()["host"], "two.test")

    def test_fixed_mode_overrides_requested_node(self):
        response = MagicMock()
        response.read.return_value = b""
        with patch.dict(os.environ, {
            "PROXY_MODE": "clash_fixed",
            "CLASH_FIXED_NODE": "fixed-us",
            "CLASH_API": "http://127.0.0.1:9097",
        }, clear=True):
            with patch("urllib.request.urlopen", return_value=response) as urlopen:
                proxy_switch.set_node("other-node")
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"name": "fixed-us"})

    def test_fixed_mode_repairs_repeated_utf8_mojibake(self):
        actual = "🇯🇵 日本 | 05"
        broken = actual.encode("utf-8").decode("latin-1")
        broken = broken.encode("utf-8").decode("latin-1")
        env = {
            "PROXY_MODE": "clash_fixed",
            "CLASH_FIXED_NODE": broken,
        }
        with patch.object(
            proxy_switch, "available_clash_nodes", return_value=[actual]
        ):
            self.assertEqual(proxy_switch.resolve_fixed_node(env), actual)

    def test_fixed_mode_reports_removed_node_before_controller_put(self):
        env = {
            "PROXY_MODE": "clash_fixed",
            "CLASH_FIXED_NODE": "removed-node",
        }
        with patch.object(
            proxy_switch, "available_clash_nodes", return_value=["active-node"]
        ):
            with self.assertRaisesRegex(ValueError, "不存在或已下线"):
                proxy_switch.ensure_proxy_mode(env)

    def test_auto_rotation_selects_next_responsive_node(self):
        with patch.dict(os.environ, {"PROXY_MODE": "clash_auto"}, clear=True):
            with patch.object(proxy_switch, "concrete_nodes", return_value=["a", "b", "c"]):
                with patch.object(proxy_switch, "current_node", return_value="a"):
                    with patch.object(proxy_switch, "node_delay", side_effect=lambda node, **_kwargs: None if node == "b" else 42):
                        with patch.object(proxy_switch, "set_node") as set_node:
                            result = proxy_switch.rotate_proxy()
        self.assertTrue(result["ok"])
        self.assertEqual(result["node"], "c")
        set_node.assert_called_once_with("c", None, force=True, environ=None)

    def test_full_flow_does_not_relabel_clash_port_as_residential(self):
        args = SimpleNamespace(
            proxy="http://127.0.0.1:7897",
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        env = {"PROXY_MODE": "clash_auto", "CLASH_PROXY": args.proxy}
        with patch.dict(os.environ, env, clear=True):
            child = run_full_flow.build_child_env(args)
        self.assertEqual(child["PROXY_MODE"], "clash_auto")
        self.assertNotIn("REG_FACTORY_PROXY", child)

    def test_full_flow_custom_proxy_override_selects_residential_mode(self):
        args = SimpleNamespace(
            proxy="http://home.test:9000",
            clash_api="http://127.0.0.1:9097",
            clash_secret="",
            clash_group="GLOBAL",
        )
        env = {"PROXY_MODE": "clash_auto", "CLASH_PROXY": "http://127.0.0.1:7897"}
        with patch.dict(os.environ, env, clear=True):
            child = run_full_flow.build_child_env(args)
        self.assertEqual(child["PROXY_MODE"], "residential")
        self.assertEqual(child["REG_FACTORY_PROXY"], "http://home.test:9000")

    def test_required_markers_reject_incomplete_page(self):
        fake = FakeCurlRequests([
            FakeResponse(200, "<html>generic page</html>"),
            FakeResponse(200, '<script src="/_next/static/chunks/a.js"></script>self.__next_f.push'),
        ])
        env = {"PROXY_MODE": "clash_auto", "CLASH_PROXY": "http://127.0.0.1:7897"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(proxy_switch, "concrete_nodes", return_value=["node1", "node2"]):
                with patch.object(proxy_switch, "set_node"):
                    with patch("curl_cffi.requests.get", side_effect=fake.get):
                        with patch("time.sleep"):
                            with patch("random.shuffle", side_effect=lambda items: None):
                                node = proxy_switch.find_working_node(
                                    test_url="https://accounts.x.ai/sign-up",
                                    required_markers=("/_next/static/chunks/", "self.__next_f.push"),
                                    verbose=False,
                                )
        self.assertEqual(node, "node2")


if __name__ == "__main__":
    unittest.main()
