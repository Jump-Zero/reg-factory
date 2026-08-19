import argparse
import asyncio
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
                "mail_api_url": "https://mail.no-replyca.xyz/api/share/share-token",
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
            self.assertEqual(
                saved_session["mail_api_url"],
                "https://mail.no-replyca.xyz/api/share/share-token",
            )

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
        args = {item["flag"]: item for item in script["args"]}
        flags = set(args)
        self.assertIn("--accounts-file", flags)
        self.assertIn("--sms-provider", flags)
        self.assertIn("--phone-attempts", flags)
        self.assertIn("--skip-phone", flags)
        self.assertIn("--no-import", flags)
        self.assertIn("--output-format", flags)
        self.assertIn("sub2api", args["--output-format"]["choices"])
        self.assertNotIn("--plus-subscription", flags)
        self.assertIn("custom", args["--sms-provider"]["choices"])

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

    def test_main_webui_integrates_protocol_links_into_plus_importer(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / "webui" / "server.py").read_text(encoding="utf-8")
        index = (root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        frontend = (root / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/chatgpt-plus/import-codex")', server)
        self.assertIn("plus_codex_import", server)
        self.assertIn('@app.api_route("/chatgpt-plus/{path:path}"', server)
        self.assertIn('_PLUS_CHECKOUT_GATE_PATHS', server)
        self.assertNotIn("pay.nyanya.love", server)
        self.assertIn('id="plus-account-input"', index)
        self.assertIn('id="plus-sms-provider"', index)
        self.assertIn('value="custom"', index)
        self.assertIn('id="custom-sms-input"', index)
        self.assertIn('/api/sms/custom', server)
        self.assertIn('id="plus-phone-attempts"', index)
        self.assertIn('id="plus-skip-phone"', index)
        self.assertIn('id="plus-no-import"', index)
        self.assertIn('id="plus-output-format"', index)
        self.assertIn('id="btn-plus-import"', index)
        self.assertIn("/api/chatgpt-plus/import-codex", frontend)
        self.assertIn("phone_attempts", frontend)
        self.assertIn('id="plus-protocol-action"', index)
        self.assertIn('id="plus-protocol-source"', index)
        self.assertIn('id="plus-protocol-method"', index)
        self.assertIn('id="plus-protocol-concurrency"', index)
        self.assertIn('id="btn-plus-protocol-run"', index)
        self.assertIn('value="paypal"', index)
        self.assertIn('value="momo"', index)
        self.assertIn('value="blik" disabled', index)
        self.assertIn('value="pay"', index)
        self.assertIn('id="plus-payment-dialog"', index)
        self.assertIn('id="plus-payment-cards"', index)
        self.assertIn('id="plus-payment-addresses"', index)
        self.assertIn('id="plus-payment-phones"', index)
        self.assertNotIn('id="plus-workbench"', index)
        self.assertNotIn('id="plus-workbench-template"', index)
        self.assertNotIn('id="plus-tab-workbench"', index)
        self.assertNotIn('/chatgpt-plus/static/direct-bind.js', index)
        self.assertIn("/api/chatgpt-plus/protocol-status", frontend)
        self.assertIn("/api/chatgpt-plus/protocol-batch", frontend)
        self.assertIn('"/api/chatgpt-plus/protocol-batch"', server)
        self.assertIn("run_protocol_payment_batch.py", server)
        self.assertIn("payment_details", server)
        self.assertIn("payment_config", server)
        self.assertNotIn('href="/chatgpt-plus/"', index)
        self.assertNotIn("btn-import-ats", index)
        self.assertNotIn("plusUrl", frontend)
        self.assertFalse((root / "webui" / "static" / "card-link-batch.js").exists())

    def test_network_panel_exposes_protocol_proxy_endpoints(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        frontend = (root / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        server = (root / "webui" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('id="proxy-plus-link-route"', index)
        self.assertNotIn('id="proxy-plus-bind-route"', index)
        self.assertIn('id="proxy-plus-link-override"', index)
        self.assertIn('id="proxy-plus-bind-override"', index)
        self.assertIn("REG_FACTORY_PLUS_LINK_PROXY_OVERRIDE", frontend)
        self.assertIn("REG_FACTORY_PLUS_BIND_PROXY_OVERRIDE", frontend)
        self.assertIn('id="plus-protocol-method"', index)
        self.assertNotIn('id="plus-workbench"', index)
        self.assertIn('"/standalone-flow/quick-checkout"', server)
        self.assertNotIn("Plus 提链和绑卡工作台已移除", server)

    def test_protocol_catalog_exposes_all_reference_channels_and_blocks_blik_batch(self):
        from common.protocol_payment import protocol_catalog

        methods = {item["id"]: item for item in protocol_catalog("does-not-exist")}
        self.assertEqual(len(methods), 12)
        self.assertTrue(methods["paypal"]["batch_enabled"])
        self.assertTrue(methods["paypal"]["batch_payment_enabled"])
        self.assertEqual(methods["paypal"]["payment_execution"], "paypal_auto")
        self.assertTrue(methods["momo"]["batch_enabled"])
        self.assertFalse(methods["blik"]["batch_enabled"])
        self.assertEqual(methods["blik"]["payment_execution"], "single_code")

    def test_paypal_per_run_details_are_validated_and_normalized(self):
        future_year = time.localtime().tm_year + 2
        config = webui_server._parse_paypal_payment_details({
            "cards": f"4242 4242 4242 4242|12/{future_year}|123",
            "addresses": "1 Market St|San Francisco|CA|94105",
            "phones": "+12025550123----https://sms.example.test/messages?token=fixture",
        })
        self.assertEqual(config["cards"][0]["number"], "4242424242424242")
        self.assertEqual(config["cards"][0]["exp_year"], str(future_year))
        self.assertEqual(config["addresses"][0]["state"], "CA")
        self.assertEqual(config["phone_numbers"][0]["phone"], "+12025550123")
        with self.assertRaisesRegex(ValueError, "同时填写"):
            webui_server._parse_paypal_payment_details({"cards": "4242424242424242|12/30|123"})

    def test_protocol_pool_source_uses_only_cached_zero_price_chatgpt_accounts(self):
        report = {"items": [
            {"platform": "chatgpt", "email": "zero@example.com", "plus_trial": "zero_price"},
            {"platform": "chatgpt", "email": "discount@example.com", "plus_trial": "discount"},
            {"platform": "chatgpt", "email": "free@example.com", "plus_trial": "ineligible"},
            {"platform": "outlook", "email": "mail@example.com", "plus_trial": "eligible"},
            {"platform": "chatgpt", "email": "unknown@example.com", "plus_trial": "unknown"},
        ]}
        with patch("common.asset_scanner.get_report", return_value=report):
            self.assertEqual(
                webui_server._protocol_pool_eligible_emails(),
                ["zero@example.com"],
            )

    def test_explicit_empty_protocol_pool_does_not_fall_back_to_all_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            token_dir = Path(temp) / "tokens" / "chatgpt"
            token_dir.mkdir(parents=True)
            payload = base64.urlsafe_b64encode(json.dumps({
                "email": "saved@example.com",
                "exp": time.time() + 3600,
            }).encode()).decode().rstrip("=")
            (token_dir / "saved.session.json").write_text(json.dumps({
                "accessToken": f"header.{payload}.signature",
                "user": {"email": "saved@example.com"},
            }), encoding="utf-8")
            with patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": temp}, clear=False):
                self.assertEqual(len(webui_server._chatgpt_protocol_accounts()), 1)
                self.assertEqual(webui_server._chatgpt_protocol_accounts([]), [])

    def test_protocol_worker_accepts_ephemeral_payment_config_in_task_file(self):
        from tools import run_protocol_payment_batch as worker

        with tempfile.TemporaryDirectory() as temp:
            task_path = Path(temp) / "task.json"
            payment_config = {
                "cards": [{"number": "4242424242424242", "exp_month": "12", "exp_year": "2030", "cvv": "123"}],
                "addresses": [{"line1": "1 Market St", "city": "San Francisco", "state": "CA", "postal_code": "94105"}],
                "phone_numbers": [{"phone": "+12025550123", "sms_api_url": "https://sms.example.test/messages"}],
            }
            task_path.write_text(json.dumps({
                "accounts": [{"email": "one@example.com", "access_token": "fixture-token"}],
                "payment_config": payment_config,
            }), encoding="utf-8")
            accounts, loaded_config = worker._load_task(task_path)
            runtime_config, index_paths = worker._paypal_runtime_config(Path(temp), loaded_config, task_path)

            self.assertEqual(accounts[0]["email"], "one@example.com")
            self.assertEqual(runtime_config["paypal_auto"]["cards"], payment_config["cards"])
            self.assertEqual(len(index_paths), 2)
            self.assertNotIn("access_token", json.dumps(runtime_config))

    def test_protocol_worker_uses_channel_countries_and_independent_proxy_pools(self):
        from tools import run_protocol_payment_batch as worker

        paypal = {"id": "paypal", "country": "US"}
        paypal_route = worker._route_options(
            paypal,
            "http://checkout-1.test:8000,http://checkout-2.test:8000",
            "http://approve.test:9000",
            300,
        )
        self.assertEqual(paypal_route["target_country"], "US")
        self.assertEqual(paypal_route["stage_proxy_countries"]["checkout"], "US")
        self.assertEqual(len(paypal_route["checkout_proxy_pool"]), 2)
        self.assertEqual(paypal_route["approve_proxy_pool"], ["http://approve.test:9000"])

        gopay = worker._route_options(
            {"id": "gopay", "country": "ID"}, "http://seed.test:8000", "", 300
        )
        self.assertEqual(gopay["stage_proxy_countries"]["promotion"], "TH")
        self.assertEqual(gopay["stage_proxy_countries"]["approve"], "JP")
        self.assertEqual(gopay["approve_proxy_pool"], gopay["checkout_proxy_pool"])

    def test_protocol_worker_formats_exception_rows_without_optional_fields(self):
        from tools import run_protocol_payment_batch as worker

        row = worker._public_result("one@example.com", {
            "ok": False,
            "error": RuntimeError("route unavailable"),
            "error_code": "worker_exception",
        })
        self.assertEqual(row["payment_status"], "")
        self.assertEqual(worker._result_detail(row), "route unavailable")
        self.assertEqual(
            worker._result_detail({"error": "proxy mismatch"}),
            "proxy mismatch",
        )

    def test_protocol_batch_rejects_malformed_json_before_execution(self):
        from starlette.requests import Request

        async def receive():
            return {"type": "http.request", "body": b"{", "more_body": False}

        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chatgpt-plus/protocol-batch",
            "raw_path": b"/api/chatgpt-plus/protocol-batch",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8800),
        }, receive)
        response = asyncio.run(webui_server.api_chatgpt_plus_protocol_batch(request))
        self.assertEqual(response.status_code, 400)
        self.assertIn("有效的 JSON", response.body.decode("utf-8"))

    def test_plus_checkout_gate_allows_only_zero_price_accounts(self):
        with patch.object(
            webui_server,
            "_plus_trial_gate_sync",
            return_value={
                "email": "zero@example.com",
                "plus_trial": "zero_price",
                "detail": "zero price",
                "evidence": "accounts_check:200:zero_price",
            },
        ):
            allowed = asyncio.run(
                webui_server._plus_trial_gate(
                    "/standalone-flow/quick-checkout",
                    "POST",
                    json.dumps({"access_token": "secret"}).encode(),
                )
            )
        self.assertIsNone(allowed)

        with patch.object(
            webui_server,
            "_plus_trial_gate_sync",
            return_value={
                "email": "free@example.com",
                "plus_trial": "ineligible",
                "detail": "no campaign",
                "evidence": "accounts_check:200:no_plus_campaign",
            },
        ):
            blocked = asyncio.run(
                webui_server._plus_trial_gate(
                    "/standalone-flow/quick-checkout",
                    "POST",
                    json.dumps({"access_token": "secret"}).encode(),
                )
            )
        self.assertEqual(blocked.status_code, 422)
        body = json.loads(blocked.body.decode("utf-8"))
        self.assertEqual(body["accounts"][0]["plus_trial"], "ineligible")
        self.assertNotIn("secret", blocked.body.decode("utf-8"))

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

    def test_plus_proxy_defaults_to_clash_even_with_residential_pool(self):
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
                    "http://127.0.0.1:7897",
                )

    def test_plus_proxy_explicit_residential_route_remains_supported(self):
        with patch.object(webui_server, "_plus_residential_proxy_url", return_value="http://home.test:9000"):
            self.assertEqual(
                webui_server._plus_route_proxy_url(
                    {"CLASH_PROXY": "http://127.0.0.1:7897"},
                    "residential",
                ),
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

    def test_plus_runtime_uses_clash_for_link_and_bind_by_default(self):
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
            self.assertEqual(values["REG_FACTORY_PLUS_LINK_PROXY"], "http://127.0.0.1:7897")
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
