import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import outlook_reg_loop
from common import proxy_switch
from common.concurrency import build_worker_plan
from common.file_lock import append_line
from common.fingerprint import browser_fingerprint
from common.task_context import activate_worker
from tools import extract_graph_tokens
from webui.scripts import SCRIPTS


class RegistrationConcurrencyTests(unittest.TestCase):
    def test_default_maximum_allows_ten_workers(self):
        plan = build_worker_plan(
            "outlook",
            10,
            10,
            {
                "PROXY_MODE": "clash_fixed",
                "CLASH_FIXED_NODE": "fixed-us",
            },
        )
        self.assertEqual(plan.effective_concurrency, 10)

    def test_residential_pool_allocates_disjoint_worker_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "PROXY_MODE": "residential",
                "REG_FACTORY_PROXY_POOL": ",".join(
                    f"http://p{index}.test:80{index}" for index in range(1, 5)
                ),
                "REG_FACTORY_PROXY_STATE_FILE": os.path.join(directory, "state.txt"),
            }
            plan = build_worker_plan("chatgpt", 4, 2, env)

        self.assertEqual(plan.effective_concurrency, 2)
        self.assertEqual(
            plan.worker(1).proxy_candidates,
            ("http://p1.test:801", "http://p3.test:803"),
        )
        self.assertEqual(
            plan.worker(2).proxy_candidates,
            ("http://p2.test:802", "http://p4.test:804"),
        )
        with patch.dict(os.environ, env, clear=True):
            with activate_worker(plan.worker(1)):
                self.assertEqual(proxy_switch.effective_proxy_url(), "http://p1.test:801")
                rotation = proxy_switch.rotate_proxy()
                self.assertTrue(rotation["requires_new_session"])
                self.assertEqual(proxy_switch.effective_proxy_url(), "http://p3.test:803")
            with activate_worker(plan.worker(2)):
                self.assertEqual(proxy_switch.effective_proxy_url(), "http://p2.test:802")

    def test_proxy_pool_caps_concurrency_when_endpoints_are_insufficient(self):
        env = {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY_POOL": "http://one.test:8001,http://two.test:8002",
        }
        plan = build_worker_plan("outlook", 5, 5, env)
        self.assertEqual(plan.effective_concurrency, 2)
        self.assertIn("代理池仅 2 个端点", " ".join(plan.warnings))

    def test_clash_auto_is_always_serialized(self):
        plan = build_worker_plan(
            "grok",
            4,
            4,
            {
                "PROXY_MODE": "clash_auto",
                "REG_FACTORY_ALLOW_SHARED_EGRESS": "1",
            },
        )
        self.assertEqual(plan.effective_concurrency, 1)
        self.assertEqual(plan.isolation, "global-clash")

    def test_fixed_clash_keeps_concurrency_but_reports_shared_ip(self):
        plan = build_worker_plan(
            "claude",
            3,
            3,
            {"PROXY_MODE": "clash_fixed", "CLASH_FIXED_NODE": "fixed-us"},
        )
        self.assertEqual(plan.effective_concurrency, 3)
        self.assertEqual(plan.isolation, "shared-fixed-clash")
        self.assertTrue(plan.warnings)

    def test_explicit_clash_node_can_be_pinned_for_shared_ip_concurrency(self):
        env = {
            "PROXY_MODE": "clash_auto",
            "REG_FACTORY_PLATFORM": "chatgpt",
            "CHATGPT_PROXY_MODE": "inherit",
        }
        with patch.object(proxy_switch, "set_node") as set_node:
            self.assertTrue(proxy_switch.pin_fixed_node("node-us", "chatgpt", env))
        set_node.assert_called_once_with("node-us", force=True, environ=env)
        self.assertEqual(env["CHATGPT_PROXY_MODE"], "clash_fixed")
        self.assertEqual(env["CHATGPT_CLASH_FIXED_NODE"], "node-us")
        plan = build_worker_plan("chatgpt", 3, 3, env)
        self.assertEqual(plan.effective_concurrency, 3)

    def test_fingerprints_are_sticky_per_worker_and_distinct_between_workers(self):
        env = {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY_POOL": "http://one.test:8001,http://two.test:8002",
        }
        plan = build_worker_plan("outlook", 2, 2, env)
        with activate_worker(plan.worker(1)):
            first = browser_fingerprint("outlook", "146")
            repeated = browser_fingerprint("outlook", "146")
        with activate_worker(plan.worker(2)):
            second = browser_fingerprint("outlook", "146")
        self.assertEqual(first, repeated)
        self.assertNotEqual(
            plan.worker(1).fingerprint_seed,
            plan.worker(2).fingerprint_seed,
        )
        self.assertIn(second["hardwareConcurrency"], {4, 8, 12, 16})
        self.assertTrue(first["isIpCreateTimeZone"])
        self.assertTrue(first["isIpCreateLanguage"])

    def test_shared_append_is_not_corrupted_by_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "accounts.txt")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda index: append_line(path, f"row-{index}"), range(40)))
            with open(path, encoding="utf-8") as handle:
                rows = {line.strip() for line in handle if line.strip()}
        self.assertEqual(rows, {f"row-{index}" for index in range(40)})

    def test_outlook_graph_http_uses_explicit_worker_proxy(self):
        session = MagicMock()
        session.get.side_effect = RuntimeError("stop after session setup")
        with patch.object(extract_graph_tokens.requests, "Session", return_value=session):
            extract_graph_tokens.get_graph_token(
                "user@example.com",
                "password",
                proxy="http://worker.test:9000",
            )
        self.assertFalse(session.trust_env)
        self.assertEqual(
            session.proxies,
            {
                "http": "http://worker.test:9000",
                "https": "http://worker.test:9000",
            },
        )

    def test_outlook_and_kiro_expose_concurrency_in_webui(self):
        scripts = {item["id"]: item for item in SCRIPTS}
        for script_id in ("outlook_reg_loop", "register_kiro"):
            flags = {item["flag"] for item in scripts[script_id]["args"]}
            self.assertIn("--concurrency", flags)

    def test_outlook_profile_uses_worker_native_fingerprint(self):
        env = {
            "PROXY_MODE": "residential",
            "REG_FACTORY_PROXY": "http://worker.test:9000",
            "FINGERPRINT_BROWSER": "bitbrowser",
        }
        plan = build_worker_plan("outlook", 1, 1, env)
        response = {"success": True, "data": {"id": "profile-1"}}
        with patch.dict(os.environ, env, clear=True), activate_worker(plan.worker(1)):
            with patch.object(outlook_reg_loop, "_bb_call", return_value=response) as request:
                outlook_reg_loop.bb_create_for_outlook_reg(
                    "outlook-worker",
                    proxy_str="http://worker.test:9000",
                )
        fingerprint = request.call_args.args[1]["browserFingerPrint"]
        payload = request.call_args.args[1]
        self.assertNotIn("platform", payload)
        self.assertNotIn("platformIcon", payload)
        self.assertEqual(fingerprint["coreVersion"], "146")
        self.assertIn(fingerprint["hardwareConcurrency"], {4, 8, 12, 16})


if __name__ == "__main__":
    unittest.main()
