import ast
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from common import custom_sms, gopay_service
from webui import server as webui_server


ROOT = Path(__file__).resolve().parents[1]


class GoPayIntegrationTests(unittest.TestCase):
    def tearDown(self):
        gopay_service._COMPONENTS = None
        gopay_service._LOAD_ERROR = ""

    def test_vendored_engine_is_complete_and_syntax_valid(self):
        core = ROOT / "vendor" / "gopay_engine" / "app" / "src" / "opai" / "core"
        required = {
            "gojek_client.py",
            "gopay_payment_protocol.py",
            "gopay_protocol_worker.py",
            "gopay_signer_v2.py",
            "gopay_support_sdk.py",
            "payment_fingerprint.py",
            "payment_inbox.py",
            "sms_helpers.py",
        }
        self.assertTrue(required.issubset({item.name for item in core.glob("*.py")}))
        self.assertTrue((ROOT / "vendor" / "gopay_engine" / "config" / "support_sdk_body_corpus.json").is_file())
        for path in core.glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_runtime_configuration_keeps_credentials_out_of_vendor(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"REG_FACTORY_DATA_DIR": temp},
            clear=False,
        ):
            runtime = gopay_service._configure_runtime()
            self.assertEqual(runtime, Path(temp) / "runtime" / "gopay")
            for key in (
                "OPAI_GOPAY_ACCOUNTS_FILE",
                "OPAI_PAYMENT_INBOX_DB_PATH",
                "OPAI_GOPAY_SMS_ENV_FILE",
                "OPAI_MIDTRANS_SNAP_STATE_FILE",
            ):
                self.assertTrue(Path(os.environ[key]).is_relative_to(runtime))
            self.assertTrue(
                Path(os.environ["OPAI_GOPAY_SUPPORT_BODY_CORPUS"]).is_relative_to(
                    ROOT / "vendor" / "gopay_engine"
                )
            )

    def test_engine_loads_in_current_python_runtime(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"REG_FACTORY_DATA_DIR": temp},
            clear=False,
        ):
            status = gopay_service.status()
        self.assertTrue(status["ready"], status["error"])
        self.assertEqual(status["accounts"], 0)
        self.assertNotIn("vendor", status["data_root"].lower())

    def test_public_jobs_remove_payment_and_account_secrets(self):
        job = gopay_service._public_job({
            "id": "one",
            "phone": "+628123456789",
            "pin": "147258",
            "proxy": "http://user:pass@example.test:8080",
            "midtrans_url": "https://app.midtrans.com/snap/v3/redirection/secret",
            "payment_fingerprint": {"profile_id": "secret"},
            "result": {
                "pin": "147258",
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "phone": "+628123456789",
            },
        })
        serialized = repr(job)
        self.assertNotIn("147258", serialized)
        self.assertNotIn("pass@example", serialized)
        self.assertNotIn("access-secret", serialized)
        self.assertNotIn("refresh-secret", serialized)
        self.assertNotIn("redirection/secret", serialized)
        self.assertEqual(job["result"]["phone"], "+628123456789")

        account = gopay_service._public_account({
            "phone": "+628123456789",
            "balance": 1,
            "payment_fingerprint": {"profile_id": "secret"},
            "activation_id": "sms-secret",
            "midtrans_binding_email": "private@example.com",
        })
        self.assertEqual(account, {"phone": "+628123456789", "balance": 1})

    def test_real_payment_requires_explicit_confirmation_before_engine_load(self):
        with patch.object(gopay_service, "_load_components") as loader:
            with self.assertRaisesRegex(ValueError, "显式确认"):
                gopay_service.start_payment({
                    "phone": "+628123456789",
                    "pin": "147258",
                    "midtrans_url": "https://app.midtrans.com/snap/v3/redirection/example",
                })
        loader.assert_not_called()

    def test_integrated_payment_auto_selects_available_wallet_account(self):
        inbox = MagicMock()
        inbox._load_gopay_accounts.return_value = [
            {"phone": "+628000000001", "balance": 0, "use_status": "no_balance"},
            {"phone": "+628000000002", "balance": 5, "use_status": "available"},
        ]
        inbox._find_gopay_account.return_value = ({
            "phone": "+628000000002",
            "pin": "147258",
            "proxy": "http://wallet-proxy.test:8000",
        }, 1)
        payment = MagicMock()
        payment.start.return_value = {"id": "pay-job", "status": "running", "pin": "147258"}
        with patch.object(
            gopay_service,
            "_load_components",
            return_value={"inbox": inbox, "payment": payment},
        ):
            job = gopay_service.start_payment({
                "midtrans_url": "https://app.midtrans.com/snap/v3/redirection/example",
                "confirm_payment": True,
            })
        self.assertEqual(job, {"id": "pay-job", "status": "running"})
        payment.start.assert_called_once_with(
            phone="+628000000002",
            pin="147258",
            midtrans_url="https://app.midtrans.com/snap/v3/redirection/example",
            proxy="http://wallet-proxy.test:8000",
        )

    def test_protocol_worker_waits_for_integrated_gopay_payment(self):
        from tools import run_protocol_payment_batch as worker

        responses = [
            {"id": "payment-job"},
            {"id": "payment-job", "status": "waiting_otp", "message": "等待输入支付 OTP"},
            {
                "id": "payment-job",
                "status": "success",
                "message": "支付完成",
                "result": {"success": True, "transaction_status": "settlement"},
            },
        ]
        with patch.object(worker, "_gopay_api", side_effect=responses) as api, patch.object(
            worker.time, "sleep"
        ):
            row = worker._execute_gopay_payment(
                {"email": "plus@example.com", "access_token": "secret", "account_id": "account"},
                payment_link={"ok": True, "url": "https://app.midtrans.com/snap/v3/redirection/example"},
                timeout=30,
            )
        self.assertTrue(row["ok"])
        self.assertEqual(row["payment_status"], "settlement")
        self.assertEqual(api.call_args_list[0].args[0], "/api/gopay/payments")
        self.assertEqual(api.call_args_list[1].args[0], "/api/gopay/payments/payment-job")
        self.assertNotIn("secret", repr(row))

    def test_stopping_managed_run_marks_matching_gopay_payment_for_cancel(self):
        lock = threading.RLock()
        condition = threading.Condition(lock)
        manager = SimpleNamespace(
            _lock=lock,
            _jobs={
                "one": {"status": "waiting_otp", "context_run_id": "owner-1"},
                "two": {"status": "running", "context_run_id": "owner-2"},
            },
            _conds={"one": condition},
            _save_state_locked=MagicMock(),
        )
        with patch.object(
            gopay_service,
            "_load_components",
            return_value={"payment": manager},
        ):
            self.assertEqual(gopay_service.cancel_payment_context("owner-1"), 1)
        self.assertTrue(manager._jobs["one"]["_cancel_requested"])
        self.assertNotIn("_cancel_requested", manager._jobs["two"])
        manager._save_state_locked.assert_called_once()

    def test_custom_sms_supports_multiple_codes_before_completion(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"CUSTOM_SMS_POOL_FILE": str(Path(temp) / "pool.json")},
            clear=False,
        ):
            custom_sms.import_text(
                "+628123456789----https://sms.example.test/record?token=secret"
            )
            rental = custom_sms.claim()
            self.assertIsNotNone(rental)
            pkey = rental[2]
            seen = set()
            with patch.object(custom_sms, "_require_public_url"), patch.object(
                custom_sms,
                "_fetch_record",
                side_effect=["GoPay OTP 111111", "GoPay PIN OTP 222222"],
            ):
                first = custom_sms.get_code(pkey, max_wait=1, interval=0, consume=False, seen=seen)
                second = custom_sms.get_code(pkey, max_wait=1, interval=0, consume=False, seen=seen)
            self.assertEqual((first, second), ("111111", "222222"))
            self.assertEqual(custom_sms.summary()["leased"], 1)
            self.assertTrue(custom_sms.complete(pkey))
            self.assertEqual(custom_sms.summary()["used"], 1)

    def test_gopay_wallet_is_embedded_in_existing_protocol_payment_ui(self):
        paths = {route.path for route in webui_server.app.routes}
        for path in (
            "/api/gopay/status",
            "/api/gopay/accounts",
            "/api/gopay/phones",
            "/api/gopay/register",
            "/api/gopay/register/batch",
            "/api/gopay/register/jobs/{job_id}/otp",
            "/api/gopay/payments",
            "/api/gopay/payments/{job_id}",
            "/api/gopay/payments/{job_id}/otp",
        ):
            self.assertIn(path, paths)
        index = (ROOT / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        frontend = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('data-view="gopay"', index)
        self.assertNotIn('id="view-gopay"', index)
        self.assertIn('id="gopay-wallet-dialog"', index)
        self.assertIn('id="btn-plus-gopay-config"', index)
        self.assertIn('id="plus-payment-confirm"', index)
        self.assertNotIn('id="btn-gopay-pay"', index)
        self.assertIn("gopay_wallet", frontend)
        self.assertIn("/api/gopay", frontend)

    def test_existing_protocol_api_starts_gopay_wallet_worker_in_same_run(self):
        from starlette.requests import Request

        payload = json.dumps({
            "method": "gopay",
            "operation": "pay",
            "confirm_payment": True,
            "source": "saved",
            "emails": ["plus@example.com"],
            "workers": 1,
        }).encode()
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chatgpt-plus/protocol-batch",
            "raw_path": b"/api/chatgpt-plus/protocol-batch",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8800")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8800),
        }, receive)
        method = {
            "id": "gopay",
            "label": "GoPay",
            "country": "ID",
            "currency": "IDR",
            "batch_enabled": True,
            "payment_execution": "gopay_wallet",
        }
        candidate = {"email": "plus@example.com", "access_token": "secret", "account_id": "account"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"REG_FACTORY_DATA_DIR": temp}, clear=False
        ), patch(
            "common.protocol_payment.payment_method", return_value=method
        ), patch(
            "common.protocol_payment.resolve_protocol_engine_root", return_value=Path(temp)
        ), patch.object(
            webui_server, "_chatgpt_protocol_accounts", return_value=[candidate]
        ), patch.object(
            webui_server,
            "_plus_trial_gate_sync",
            return_value={"email": "plus@example.com", "plus_trial": "zero_price"},
        ), patch(
            "common.gopay_service.status",
            return_value={"ready": True, "available_accounts": 1},
        ), patch.object(
            webui_server, "_child_env", return_value={}
        ), patch.object(
            webui_server,
            "_plus_runtime_environment",
            return_value={"REG_FACTORY_PLUS_LINK_PROXY": "", "REG_FACTORY_PLUS_BIND_PROXY": ""},
        ), patch(
            "common.proxy_switch.ensure_proxy_mode"
        ), patch.object(
            webui_server, "_build_cmd", return_value=["python", "worker"]
        ), patch.object(
            webui_server,
            "_start_managed_run",
            new=AsyncMock(return_value={"run_id": "fixture-run", "cmd": "python worker"}),
        ) as starter, patch.object(
            webui_server, "RUNS", {"fixture-run": {}}
        ):
            result = asyncio.run(webui_server.api_chatgpt_plus_protocol_batch(request))

        self.assertEqual(result["payment_method"], "gopay")
        self.assertEqual(result["operation"], "pay")
        task_env = starter.await_args.args[2]
        self.assertEqual(task_env["REG_FACTORY_GOPAY_API_BASE"], "http://127.0.0.1:8800")
        self.assertTrue(task_env["REG_FACTORY_RUN_ID"].startswith("webui-"))

    def test_release_collects_tls_client_and_vendored_engine(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "reg-factory.spec").read_text(encoding="utf-8")
        self.assertIn("tls-client", requirements)
        self.assertIn('collect_all("tls_client")', spec)
        self.assertIn('tls_runtime_name = "tls-client-64.dll"', spec)
        self.assertIn('"sqlite3"', spec)
        self.assertIn('"vendor" / "gopay_engine"', spec)

    def test_protocol_engine_discovery_follows_portable_data_root(self):
        from common.protocol_payment import resolve_protocol_engine_root

        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            data_root = parent / "reg-factory-data"
            engine = parent / "GPT-Register-Tool"
            (engine / "sms_tool").mkdir(parents=True)
            (engine / "sms_tool" / "payment_link_manager.py").write_text("", encoding="utf-8")
            (engine / "payment_methods.json").write_text('{"methods":[]}', encoding="utf-8")
            with patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": str(data_root)}, clear=False):
                self.assertEqual(resolve_protocol_engine_root(), engine.resolve())


if __name__ == "__main__":
    unittest.main()
