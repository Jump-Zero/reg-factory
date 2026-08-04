import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import config
from webui import server


class FakeJSONRequest:
    def __init__(self, data=None):
        self._data = data or {}
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")

    async def json(self):
        return self._data


class WebUIEnvReloadTests(unittest.TestCase):
    def _env_file(self, value):
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        tmp.write(f"DYNAMIC_TEST_KEY={value}\n")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        return tmp.name

    def test_child_env_uses_latest_dotenv_value_without_restart(self):
        path = self._env_file("new-value")
        with patch.object(server, "ENV_PATH", path):
            with patch.object(server, "BOOT_ENV", {}):
                with patch.dict(os.environ, {"DYNAMIC_TEST_KEY": "stale-value"}):
                    child = server._child_env()
        self.assertEqual(child["DYNAMIC_TEST_KEY"], "new-value")
        self.assertEqual(child["REG_FACTORY_WEBUI_TASK"], "1")

    def test_global_config_honors_explicit_env_file(self):
        path = self._env_file("portable-value")
        with patch.dict(os.environ, {"REG_FACTORY_ENV_FILE": path}, clear=False):
            os.environ.pop("DYNAMIC_TEST_KEY", None)
            config._load_dotenv()
            self.assertEqual(os.environ.get("DYNAMIC_TEST_KEY"), "portable-value")
            os.environ.pop("DYNAMIC_TEST_KEY", None)

    def test_explicit_startup_environment_keeps_precedence(self):
        path = self._env_file("dotenv-value")
        with patch.object(server, "ENV_PATH", path):
            with patch.object(server, "BOOT_ENV", {"DYNAMIC_TEST_KEY": "system-value"}):
                with patch.dict(os.environ, {"DYNAMIC_TEST_KEY": "system-value"}):
                    child = server._child_env()
        self.assertEqual(child["DYNAMIC_TEST_KEY"], "system-value")

    def test_child_env_uses_clash_proxy_in_auto_mode(self):
        path = self._env_file("unused")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("PROXY_MODE=clash_auto\nCLASH_PROXY=http://127.0.0.1:7897\n")
        with patch.object(server, "ENV_PATH", path), patch.object(server, "BOOT_ENV", {}):
            child = server._child_env()
        self.assertEqual(child["HTTPS_PROXY"], "http://127.0.0.1:7897")

    def test_child_env_uses_residential_proxy(self):
        path = self._env_file("unused")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("PROXY_MODE=residential\nREG_FACTORY_PROXY=http://home.test:9000\n")
        with patch.object(server, "ENV_PATH", path), patch.object(server, "BOOT_ENV", {}):
            child = server._child_env()
        self.assertEqual(child["HTTPS_PROXY"], "http://home.test:9000")

    def test_child_env_applies_platform_proxy_override(self):
        path = self._env_file("unused")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                "PROXY_MODE=clash_auto\n"
                "CLASH_PROXY=http://127.0.0.1:7897\n"
                "OUTLOOK_PROXY_MODE=clash_auto\n"
                "CLAUDE_PROXY_MODE=residential\n"
                "REG_FACTORY_PROXY=http://home.test:9000\n"
            )
        with patch.object(server, "ENV_PATH", path), patch.object(server, "BOOT_ENV", {}):
            outlook = server._child_env("outlook")
            claude = server._child_env("claude")
        self.assertEqual(outlook["HTTPS_PROXY"], "http://127.0.0.1:7897")
        self.assertEqual(claude["HTTPS_PROXY"], "http://home.test:9000")
        self.assertEqual(claude["REG_FACTORY_PLATFORM"], "claude")

    def test_status_exposes_loaded_version_and_process_id(self):
        with patch.object(server, "_fingerprint_provider", return_value="bitbrowser"):
            with patch.object(server, "_read_config_val", side_effect=lambda _key, default="": default):
                with patch.object(server, "_http_alive", return_value=True):
                    with patch.object(server, "_k12_alive", return_value=False):
                        with patch("common.proxy_switch.current_node", return_value="test-node"):
                            status = server.api_status()
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["version"], server.WEBUI_VERSION)
        self.assertEqual(status["root"], server.ROOT)

    def test_residential_proxy_test_retries_with_fresh_connections(self):
        response = MagicMock()
        response.json.return_value = {"ip": "203.0.113.9"}
        response.text = ""
        failures = [RuntimeError("first exit timeout"), RuntimeError("second exit timeout"), response]

        async def run_test():
            with patch("common.proxy_switch.effective_proxy_url", return_value="http://user:pass@home.test:9000"):
                with patch("common.proxy_switch.proxy_mode", return_value="residential"):
                    with patch("common.proxy_switch.current_node", return_value="http://home.test:9000"):
                        with patch("curl_cffi.requests.get", side_effect=failures) as request:
                            result = await server.api_proxy_test()
            return result, request

        result, request = asyncio.run(run_test())
        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "203.0.113.9")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(request.call_count, 3)

    def test_residential_proxy_test_redacts_credentials_from_errors(self):
        async def run_test():
            with patch("common.proxy_switch.effective_proxy_url", return_value="http://user:pass@home.test:9000"):
                with patch("common.proxy_switch.proxy_mode", return_value="residential"):
                    with patch(
                        "curl_cffi.requests.get",
                        side_effect=RuntimeError("proxy user:pass rejected at http://user:pass@home.test:9000"),
                    ):
                        return await server.api_proxy_test()

        response = asyncio.run(run_test())
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("user", payload["error"])
        self.assertNotIn("pass", payload["error"])

    def test_proxy_save_write_failure_returns_json(self):
        path = self._env_file("unchanged")

        async def save():
            with patch.object(server, "ENV_PATH", path):
                with patch.object(server, "_write_env_file", side_effect=OSError("disk full")):
                    return await server.api_proxy_set(FakeJSONRequest({
                        "config": {"PROXY_MODE": "clash_auto"},
                    }))

        response = asyncio.run(save())
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(payload["ok"])
        self.assertIn("disk full", payload["error"])

    def test_proxy_test_invalid_platform_returns_json(self):
        response = asyncio.run(server.api_proxy_test("invalid-platform"))
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])

    def test_asset_api_without_key_is_loopback_only(self):
        local = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        remote = SimpleNamespace(headers={}, client=SimpleNamespace(host="192.0.2.10"))
        with patch.object(server, "_read_config_val", return_value=""):
            self.assertIsNone(server._asset_api_denied(local))
            self.assertEqual(server._asset_api_denied(remote).status_code, 403)

    def test_asset_api_accepts_header_or_bearer_key(self):
        by_header = SimpleNamespace(
            headers={"x-api-key": "asset-secret"}, client=SimpleNamespace(host="192.0.2.10")
        )
        by_bearer = SimpleNamespace(
            headers={"authorization": "Bearer asset-secret"},
            client=SimpleNamespace(host="192.0.2.10"),
        )
        denied = SimpleNamespace(
            headers={"x-api-key": "wrong"}, client=SimpleNamespace(host="127.0.0.1")
        )
        with patch.object(server, "_read_config_val", return_value="asset-secret"):
            self.assertIsNone(server._asset_api_denied(by_header))
            self.assertIsNone(server._asset_api_denied(by_bearer))
            self.assertEqual(server._asset_api_denied(denied).status_code, 401)

    def test_asset_api_scans_before_returning_an_email(self):
        from common import asset_scanner, asset_store

        events = []

        def scan_pool(**_kwargs):
            events.append("scan")
            return {"items": []}

        def get_email(**kwargs):
            events.append("read")
            self.assertTrue(kwargs["verified_only"])
            return {"kind": "email", "verification": {"status": "normal"}}

        with patch.object(asset_scanner, "scan_pool", side_effect=scan_pool):
            with patch.object(asset_store, "get_email", side_effect=get_email):
                result = server.api_asset_email(FakeJSONRequest())

        self.assertEqual(events, ["scan", "read"])
        self.assertEqual(result["verification"]["status"], "normal")


class WebUIRunStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_task_process_matcher_excludes_webui_and_other_python_projects(self):
        self.assertTrue(server._is_managed_task_process(
            'E:\\reg-factory\\dist\\app\\reg-factory.exe --task outlook_reg_loop.py --count 0',
            'E:\\reg-factory\\dist\\app\\reg-factory.exe',
        ))
        self.assertFalse(server._is_managed_task_process(
            'E:\\reg-factory\\dist\\app\\reg-factory.exe --port 8799',
            'E:\\reg-factory\\dist\\app\\reg-factory.exe',
        ))
        self.assertFalse(server._is_managed_task_process(
            'python C:\\other-project\\register.py',
            'C:\\Python312\\python.exe',
        ))

    async def test_stop_all_terminates_tracked_and_orphaned_task_trees(self):
        run_id = "test-stop-all"
        server.RUNS[run_id] = {
            "proc": SimpleNamespace(pid=101),
            "lines": [],
            "done": False,
            "stopped": False,
        }
        self.addCleanup(server.RUNS.pop, run_id, None)
        with patch.object(
            server,
            "_list_orphaned_task_processes",
            return_value=[{"pid": 101}, {"pid": 202}],
        ):
            with patch.object(server, "_terminate_process_tree", return_value=True) as terminate:
                result = await server.api_stop_all()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 2)
        self.assertEqual(result["tracked"], 1)
        self.assertEqual(result["orphaned"], 1)
        self.assertTrue(server.RUNS[run_id]["stopped"])
        self.assertEqual({call.args[0] for call in terminate.call_args_list}, {101, 202})

    async def test_done_event_exposes_exit_code_and_stop_state(self):
        run_id = "test-result-event"
        server.RUNS[run_id] = {
            "lines": ["finished"],
            "done": True,
            "returncode": 7,
            "stopped": False,
        }
        self.addCleanup(server.RUNS.pop, run_id, None)

        response = await server.api_logs(run_id)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        body = "".join(chunks)

        self.assertIn("event: done", body)
        payload = body.split("event: done\ndata: ", 1)[1].split("\n", 1)[0]
        self.assertEqual(
            json.loads(payload),
            {"returncode": 7, "stopped": False},
        )
        self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")


class WebUIAssetScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server.ASSET_SCAN_TASK = None
        server.ASSET_SCAN_STATE.update({
            "running": False,
            "started_at": "",
            "finished_at": "",
            "error": "",
            "progress": {"completed": 0, "total": 0, "current": ""},
        })

    async def test_asset_scan_runs_in_background_and_exposes_progress(self):
        from common import asset_scanner

        report = {
            "schema_version": 1,
            "finished_at": "2026-07-28T09:00:00Z",
            "last_scan_at": "2026-07-28T09:00:00Z",
            "items": [{"id": "one", "platform": "outlook", "status": "normal"}],
            "summary": {"total": 1, "statuses": {"normal": 1}, "platforms": {}},
        }

        def scan_pool(**kwargs):
            kwargs["progress"]({"completed": 1, "total": 1, "current": "mail@example.com"})
            return report

        with patch.object(asset_scanner, "get_report", return_value=report):
            with patch.object(asset_scanner, "scan_pool", side_effect=scan_pool):
                started = await server.api_asset_scan_start(
                    FakeJSONRequest({"platforms": ["outlook"], "concurrency": 2})
                )
                task = server.ASSET_SCAN_TASK
                self.assertTrue(started["ok"])
                self.assertTrue(started["scan"]["running"])
                await task
                current = server.api_asset_scan_get(FakeJSONRequest())

        self.assertFalse(current["scan"]["running"])
        self.assertEqual(current["scan"]["progress"]["completed"], 1)
        self.assertEqual(current["summary"]["statuses"]["normal"], 1)

    async def test_asset_scan_rejects_unknown_platform(self):
        response = await server.api_asset_scan_start(FakeJSONRequest({"platforms": ["unknown"]}))
        self.assertEqual(response.status_code, 400)

    async def test_asset_scan_accepts_kiro(self):
        from common import asset_scanner

        report = {"items": [], "summary": {"total": 0, "statuses": {}, "platforms": {}}}
        with patch.object(asset_scanner, "get_report", return_value=report):
            with patch.object(asset_scanner, "scan_pool", return_value=report):
                started = await server.api_asset_scan_start(
                    FakeJSONRequest({"platforms": ["kiro"], "concurrency": 2})
                )
                task = server.ASSET_SCAN_TASK
                self.assertTrue(started["ok"])
                await task



if __name__ == "__main__":
    unittest.main()
