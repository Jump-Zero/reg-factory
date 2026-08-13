import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from common import chatgpt_plus, session_export
import register_three_platforms
from webui import server as webui_server
from webui.scripts import SCRIPTS


class ChatGPTPlusTests(unittest.TestCase):
    def test_registration_queue_references_session_without_copying_token(self):
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            token_root = data_root / "tokens"
            session = {
                "accessToken": "fixture-secret-access-token",
                "user": {"email": "one@example.com"},
                "registration_country": "JP",
                "network_node": "Japan 01",
            }
            with patch.object(session_export, "TOKEN_OUTPUT_DIR", str(token_root)), patch.object(
                chatgpt_plus, "chatgpt_session_path", session_export.chatgpt_session_path
            ), patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": str(data_root)}, clear=False):
                self.assertTrue(session_export.save_chatgpt_tokens(session, "one@example.com"))
                queued = chatgpt_plus.queue_registered_account("one@example.com")
                payload = json.loads(chatgpt_plus.plus_queue_path().read_text(encoding="utf-8"))

            self.assertEqual(queued["max_concurrency"], 27)
            self.assertEqual(payload["max_concurrency"], 27)
            self.assertNotIn("fixture-secret-access-token", json.dumps(payload))
            self.assertTrue(Path(payload["items"][0]["session_path"]).is_file())
            saved_session = json.loads(
                Path(payload["items"][0]["session_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(saved_session["registration_country"], "JP")
            self.assertEqual(saved_session["network_node"], "Japan 01")

    def test_codex_oauth_credentials_persist_phone_status(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(session_export, "TOKEN_OUTPUT_DIR", str(Path(temp) / "tokens")):
                self.assertTrue(session_export.save_codex_oauth_credentials({
                    "email": "verified@example.com",
                    "access_token": "fixture-access-token",
                    "refresh_token": "fixture-refresh-token",
                    "codex_phone_status": "verified",
                }))
            saved = list((Path(temp) / "tokens" / "chatgpt").glob("oauth-*.session.json"))
            self.assertEqual(len(saved), 1)
            payload = json.loads(saved[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["codex_phone_status"], "verified")
            self.assertNotIn("fixture-access-token", payload["email"])

    def test_webui_exposes_existing_plus_codex_import_task(self):
        script = next(item for item in SCRIPTS if item["id"] == "plus_codex_import")
        flags = {item["flag"] for item in script["args"]}
        self.assertIn("--accounts-file", flags)
        self.assertIn("--sms-provider", flags)
        self.assertIn("--phone-attempts", flags)
        self.assertNotIn("--plus-subscription", flags)

    def test_existing_email_flow_propagates_subscription_mode(self):
        args = argparse.Namespace(
            timeout=600,
            node="auto",
            keep_on_fail=False,
            import_c2a=False,
            plus_subscription=True,
            codex=False,
        )
        command = register_three_platforms.build_command(
            "chatgpt", args, ("one@example.com", "password", "refresh", "client")
        )
        self.assertIn("--plus-subscription", command)

    def test_vendored_workbench_supports_aligned_batch_and_auto_card(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        server = (root / "server.py").read_text(encoding="utf-8")
        index = (root / "index.html").read_text(encoding="utf-8")
        frontend = (root / "static" / "direct-bind.js").read_text(encoding="utf-8")
        self.assertIn("ALIGNED_BATCH_LIMIT = 27", server)
        self.assertIn("QUEUE_IMPORT_LIMIT = 100", server)
        self.assertIn("MAX_BATCH_CONCURRENCY = 27", frontend)
        self.assertIn("/api/runtime-defaults", server)
        self.assertIn("applyRuntimeDefaults", frontend)
        self.assertIn("_with_runtime_proxy", server)
        self.assertIn("REG_FACTORY_PLUS_PROXY", server)
        self.assertIn('id="billingName"', index)
        self.assertIn('id="applyBillingButton"', index)
        self.assertIn('readonly', index)
        self.assertIn('RANDOM_BILLING_FIXTURES', frontend)
        self.assertIn('generateBillingAddress', frontend)
        self.assertIn("account.billingMode = 'random'", frontend)
        self.assertIn('accountEnvironmentReady', frontend)
        self.assertIn('await mountCard();', frontend)
        self.assertIn('preflightFailures', frontend)
        self.assertIn('account.selected = false', frontend)
        self.assertIn('Checkout 返回非零金额', frontend)
        self.assertIn('Checkout 返回非零金额', server)
        self.assertIn('id="cardPasteInput"', index)
        self.assertIn("parsePastedCard", frontend)
        self.assertIn("card: browserCard.card", frontend)
        self.assertIn("id=\"importAtFileButton\"", index)
        self.assertIn('id="batchMode"', index)
        self.assertIn('id="nextBatchButton"', index)
        self.assertIn('id="batchRotationStatus"', index)
        self.assertIn('manualBatchWaiting', frontend)
        self.assertIn('waitForNextBatch', frontend)
        self.assertIn('account_id: account.accountId', frontend)
        self.assertIn('手动轮换', index)
        self.assertNotIn("proxy-pool-section", index)
        self.assertNotIn("PROXY_DRAFT_KEY", frontend)
        self.assertNotIn("fetch('/api/address'", frontend)
        self.assertIn("billingMode: ''", frontend)
        self.assertIn("billing: null", frontend)

    def test_batch_rotation_skips_terminal_accounts_and_advances_remaining_rows(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        frontend = (root / "static" / "direct-bind.js").read_text(encoding="utf-8")

        self.assertIn("function runnableBatchAccounts", frontend)
        self.assertIn("!isBatchTerminalAccount(account)", frontend)
        self.assertIn("const accounts = [...data.runnable]", frontend)
        self.assertIn("if (isBatchTerminalAccount(account)) account.selected = false", frontend)
        self.assertNotIn("const accounts = [...data.accounts]", frontend)

    def test_shortlink_flow_prefers_nested_oaics_and_updates_a_clean_baseline(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        extractor = root / "standalone_core" / "ph_shortlink_extractor.py"
        completed = subprocess.run(
            [sys.executable, str(extractor), "--self-test"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SELF-TEST PASS", completed.stdout)
        self.assertIn("oaics_fixture123456789", completed.stdout)

    def test_vendored_checkout_requires_oaics_while_payment_accepts_stripe_ids(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        flow = (root / "standalone_flow.py").read_text(encoding="utf-8")
        payment = (root / "standalone_core" / "card_payment.py").read_text(encoding="utf-8")
        server = (root / "server.py").read_text(encoding="utf-8")
        frontend = (root / "static" / "direct-bind.js").read_text(encoding="utf-8")
        self.assertIn('SUPPORTED_CHECKOUT_PREFIXES = ("oaics_", "cs_live_", "cs_test_", "cs_")', flow)
        self.assertIn("require_oaics=True", flow)
        self.assertIn('strong_bind_direct=_text(first.get("checkout_id")).startswith("oaics_")', flow)
        self.assertIn('checkout refresh: missing supported checkout id', payment)
        self.assertIn('(?:oaics_|cs_)', server)
        self.assertIn('(?:oaics_|cs_)', frontend)

    def test_main_webui_exposes_plus_codex_importer_and_removes_old_workbench(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / "webui" / "server.py").read_text(encoding="utf-8")
        index = (root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        frontend = (root / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/chatgpt-plus/import-codex")', server)
        self.assertIn("plus_codex_import", server)
        self.assertNotIn("pay.nyanya.love", server)
        self.assertIn('id="plus-account-input"', index)
        self.assertIn('id="plus-sms-provider"', index)
        self.assertIn('id="plus-phone-attempts"', index)
        self.assertIn('id="btn-plus-import"', index)
        self.assertIn("/api/chatgpt-plus/import-codex", frontend)
        self.assertIn("phone_attempts", frontend)
        self.assertNotIn("btn-import-ats", index)
        self.assertNotIn("plusUrl", frontend)
        self.assertFalse((root / "webui" / "static" / "card-link-batch.js").exists())

    def test_network_panel_does_not_expose_removed_plus_payment_controls(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        frontend = (root / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "webui" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('id="proxy-plus-link-route"', index)
        self.assertNotIn('id="proxy-plus-bind-route"', index)
        self.assertNotIn("REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE", frontend)
        self.assertNotIn("REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE", frontend)
        self.assertIn('status_code=410', server)

    def test_batch_selects_latest_27_unexpired_free_accounts(self):
        def jwt(index, expires):
            def segment(value):
                raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
                return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            return f"{segment({'alg': 'none'})}.{segment({'exp': expires, 'email': f'user{index}@example.com'})}.sig{index}"

        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            token_root = data_root / "tokens" / "chatgpt"
            token_root.mkdir(parents=True)
            expires = int(time.time()) + 3600
            expected = []
            for index in range(30):
                token = jwt(index, expires)
                path = token_root / f"free-{index:02d}.session.json"
                path.write_text(json.dumps({
                    "accessToken": token,
                    "account": {"planType": "free"},
                    "user": {"email": f"user{index}@example.com"},
                }), encoding="utf-8")
                os.utime(path, (index + 1, index + 1))
                expected.append(token)
            (token_root / "plus.session.json").write_text(json.dumps({
                "accessToken": jwt(99, expires),
                "account": {"planType": "plus"},
            }), encoding="utf-8")
            (token_root / "expired.session.json").write_text(json.dumps({
                "accessToken": jwt(100, int(time.time()) - 1),
                "account": {"planType": "free"},
            }), encoding="utf-8")

            with patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": str(data_root)}, clear=False):
                selected, available = webui_server._chatgpt_plus_free_ats(27)

        self.assertEqual(available, 30)
        self.assertEqual([item["access_token"] for item in selected], list(reversed(expected[-27:])))

    def test_local_plus_runtime_has_no_separate_proxy_controls(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        index = (root / "index.html").read_text(encoding="utf-8")
        frontend = (root / "static" / "direct-bind.js").read_text(encoding="utf-8")
        self.assertIn("REG_FACTORY_PLUS_PROXY", (root / "server.py").read_text(encoding="utf-8"))
        self.assertNotIn("dual-proxy-grid", index)
        self.assertNotIn("localStorage.setItem(PROXY", frontend)
        self.assertIn("主程序网络出口", frontend)

    def test_plus_proxy_prefers_residential_over_chatgpt_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text(
                "PROXY_MODE=clash_auto\n"
                "CHATGPT_PROXY_MODE=clash_auto\n"
                "CLASH_PROXY=http://127.0.0.1:7897\n"
                "REG_FACTORY_PROXY=http://home.test:9000\n"
                "REG_FACTORY_PROXY_POOL=\n",
                encoding="utf-8",
            )
            with patch.object(webui_server, "ENV_PATH", str(env_path)), patch.object(webui_server, "BOOT_ENV", {}), patch.dict(
                os.environ,
                {
                    "REG_FACTORY_DATA_DIR": temp,
                    "REG_FACTORY_PLUS_LINK_ROUTE": "",
                    "REG_FACTORY_PLUS_BIND_ROUTE": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    webui_server._plus_runtime_environment()["REG_FACTORY_PLUS_PROXY"],
                    "http://home.test:9000",
                )

    def test_plus_proxy_falls_back_to_clash_without_residential_config(self):
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text(
                "PROXY_MODE=residential\n"
                "CLASH_PROXY=http://127.0.0.1:8897\n"
                "REG_FACTORY_PROXY=\n"
                "REG_FACTORY_PROXY_POOL=\n",
                encoding="utf-8",
            )
            with patch.object(webui_server, "ENV_PATH", str(env_path)), patch.object(webui_server, "BOOT_ENV", {}), patch.dict(
                os.environ,
                {
                    "REG_FACTORY_DATA_DIR": temp,
                    "REG_FACTORY_PLUS_LINK_ROUTE": "",
                    "REG_FACTORY_PLUS_BIND_ROUTE": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    webui_server._plus_runtime_environment()["REG_FACTORY_PLUS_PROXY"],
                    "http://127.0.0.1:8897",
                )

    def test_plus_runtime_splits_checkout_and_card_egress(self):
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text(
                "PROXY_MODE=clash_auto\n"
                "CLASH_PROXY=http://127.0.0.1:7897\n"
                "REG_FACTORY_PROXY=http://home.test:9000\n"
                "REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE=http://127.0.0.1:7901\n"
                "REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE=http://127.0.0.1:7902\n"
                "REG_FACTORY_PROXY_POOL=\n",
                encoding="utf-8",
            )
            with patch.object(webui_server, "ENV_PATH", str(env_path)), patch.object(webui_server, "BOOT_ENV", {}), patch.dict(
                os.environ,
                {
                    "REG_FACTORY_DATA_DIR": temp,
                    "REG_FACTORY_PLUS_LINK_ROUTE": "",
                    "REG_FACTORY_PLUS_BIND_ROUTE": "",
                },
                clear=False,
            ):
                values = webui_server._plus_runtime_environment()
            self.assertEqual(values["REG_FACTORY_PLUS_LINK_PROXY"], "http://127.0.0.1:7901")
            self.assertEqual(values["REG_FACTORY_PLUS_BIND_PROXY"], "http://127.0.0.1:7902")

    def test_plus_runtime_uses_residential_for_link_and_clash_for_bind_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text(
                "PROXY_MODE=clash_auto\n"
                "CLASH_PROXY=http://127.0.0.1:7897\n"
                "REG_FACTORY_PROXY=http://home.test:9000\n"
                "REG_FACTORY_PROXY_POOL=\n",
                encoding="utf-8",
            )
            with patch.object(webui_server, "ENV_PATH", str(env_path)), patch.object(webui_server, "BOOT_ENV", {}), patch.dict(
                os.environ,
                {
                    "REG_FACTORY_DATA_DIR": temp,
                    "REG_FACTORY_PLUS_LINK_ROUTE": "",
                    "REG_FACTORY_PLUS_BIND_ROUTE": "",
                },
                clear=False,
            ):
                values = webui_server._plus_runtime_environment()
            self.assertEqual(values["REG_FACTORY_PLUS_LINK_PROXY"], "http://home.test:9000")
            self.assertEqual(values["REG_FACTORY_PLUS_BIND_PROXY"], "http://127.0.0.1:7897")

    def test_plus_server_injects_stage_specific_proxy_pools(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        import importlib.util

        spec = importlib.util.spec_from_file_location("test_plus_server_stage_proxy", root / "server.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.object(sys, "path", [str(root), *sys.path]):
            spec.loader.exec_module(module)
        with patch.dict(
            os.environ,
            {
                "REG_FACTORY_PLUS_LINK_PROXY": "http://link.test:9000",
                "REG_FACTORY_PLUS_BIND_PROXY": "http://bind.test:9001",
                "REG_FACTORY_PLUS_PROXY": "http://legacy.test:9002",
            },
            clear=False,
        ):
            payload = module._with_runtime_proxy({"access_token": "token"})
            self.assertEqual(payload["promo_proxy_pool"], ["http://link.test:9000"])
            self.assertEqual(payload["bind_proxy_pool"], ["http://bind.test:9001"])

    def test_plus_runtime_defaults_warn_when_fixed_nodes_share_listener(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        import importlib.util

        spec = importlib.util.spec_from_file_location("test_plus_server_route_notice", root / "server.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.object(sys, "path", [str(root), *sys.path]):
            spec.loader.exec_module(module)
        with patch.dict(
            os.environ,
            {
                "REG_FACTORY_PLUS_LINK_PROXY": "http://127.0.0.1:7897",
                "REG_FACTORY_PLUS_BIND_PROXY": "http://127.0.0.1:7897",
                "REG_FACTORY_PLUS_LINK_CLASH_NODE": "node-a",
                "REG_FACTORY_PLUS_BIND_CLASH_NODE": "node-b",
            },
            clear=False,
        ):
            values = module._runtime_defaults()
        self.assertTrue(values["fixed_node_serialized"])
        self.assertIn("共用同一 Clash 监听端口", values["route_notice"])

    def test_workbench_http_contract_rejects_over_27_account_batch(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "chatgpt_plus"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            env = dict(os.environ)
            env.update({
                "REG_FACTORY_PLUS_PORT": str(port),
                "REG_FACTORY_PLUS_FINGERPRINT_STORE": str(runtime / "fingerprints.json"),
                "REG_FACTORY_PLUS_QUEUE_FILE": str(runtime / "queue.json"),
                "REG_FACTORY_PLUS_CONFIG": str(root / "standalone_config.json"),
            })
            process = subprocess.Popen(
                [sys.executable, "-u", str(root / "serve_direct.py")],
                cwd=root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                health = None
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except OSError:
                        time.sleep(0.05)
                self.assertEqual(health["service"], "reg-factory-chatgpt-plus")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/standalone-flow/quick-checkout-batch",
                    data=json.dumps({"tasks": [{"payload": {}} for _ in range(28)]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 400)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
