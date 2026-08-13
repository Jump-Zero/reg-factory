import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webui import server


class WebUIUpdateTests(unittest.TestCase):
    def setUp(self):
        self.runs = server.RUNS.copy()
        self.update_process = server.UPDATE_PROCESS
        self.update_log = server.UPDATE_LOG_HANDLE
        self.update_result_path = server.UPDATE_RESULT_PATH
        self.update_state = dict(server.UPDATE_STATE)
        server.RUNS.clear()
        server.UPDATE_PROCESS = None
        server.UPDATE_LOG_HANDLE = None
        server.UPDATE_RESULT_PATH = ""
        server.UPDATE_STATE.update(status="idle", message="", started_at="")

    def tearDown(self):
        if server.UPDATE_LOG_HANDLE:
            server.UPDATE_LOG_HANDLE.close()
        server.RUNS.clear()
        server.RUNS.update(self.runs)
        server.UPDATE_PROCESS = self.update_process
        server.UPDATE_LOG_HANDLE = self.update_log
        server.UPDATE_RESULT_PATH = self.update_result_path
        server.UPDATE_STATE.clear()
        server.UPDATE_STATE.update(self.update_state)

    def test_refuses_update_while_task_is_running(self):
        server.RUNS["active"] = {"done": False}

        with patch.object(server, "_update_script", return_value=["updater"]):
            response = server.api_update()

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.body)
        self.assertIn("运行中", payload["error"])

    def test_starts_detached_windows_updater(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = MagicMock()
            process.poll.return_value = None
            command = ["powershell.exe", "-File", "update.ps1"]
            with patch.object(server, "ROOT", tmp), patch.object(
                server, "_update_script", return_value=command
            ), patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": tmp}, clear=False), patch.object(
                server.subprocess, "Popen", return_value=process
            ) as popen:
                response = server.api_update()

            self.assertEqual(response.status_code, 202)
            self.assertTrue(json.loads(response.body)["ok"])
            self.assertEqual(popen.call_args.args[0], command)
            creationflags = popen.call_args.kwargs.get("creationflags", 0)
            self.assertFalse(creationflags & getattr(server.subprocess, "DETACHED_PROCESS", 0))
            self.assertTrue(creationflags & getattr(server.subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(server.UPDATE_STATE["status"], "running")
            server.UPDATE_LOG_HANDLE.close()
            server.UPDATE_LOG_HANDLE = None

    def test_update_process_does_not_inherit_registration_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = MagicMock()
            process.poll.return_value = None
            command = ["powershell.exe", "-File", "update.ps1"]
            proxy = "http://registration-user:registration-pass@residential.test:9000"
            with patch.object(server, "ROOT", tmp), patch.object(
                server, "_update_script", return_value=command
            ), patch.dict(
                os.environ,
                {
                    "REG_FACTORY_DATA_DIR": tmp,
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "ALL_PROXY": proxy,
                    "http_proxy": proxy,
                    "https_proxy": proxy,
                    "all_proxy": proxy,
                },
                clear=False,
            ), patch.object(server.subprocess, "Popen", return_value=process) as popen:
                response = server.api_update()

            self.assertEqual(response.status_code, 202)
            child_env = popen.call_args.kwargs["env"]
            for name in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy",
            ):
                self.assertNotIn(name, child_env)
            self.assertIn("github.com", child_env["NO_PROXY"])
            self.assertEqual(child_env["REG_FACTORY_NONINTERACTIVE"], "1")
            server.UPDATE_LOG_HANDLE.close()
            server.UPDATE_LOG_HANDLE = None

    def test_reports_missing_updater(self):
        with patch.object(server, "_update_script", return_value=None):
            response = server.api_update()

        self.assertEqual(response.status_code, 501)
        self.assertIn("自动更新程序", json.loads(response.body)["error"])

    def test_status_exposes_update_capability(self):
        with patch.object(server, "_fingerprint_provider", return_value="bitbrowser"), patch.object(
            server, "_read_config_val", side_effect=lambda _key, default="": default
        ), patch.object(server, "_http_alive", return_value=True), patch.object(
            server, "_k12_alive", return_value=False
        ), patch("common.proxy_switch.current_node", return_value="test-node"), patch.object(
            server, "_update_script", return_value=["updater"]
        ):
            status = server.api_status()

        self.assertTrue(status["update"]["available"])
        self.assertEqual(status["update"]["status"], "idle")

    def test_frozen_runtime_uses_portable_updater(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "update-portable.ps1"
            script.write_text("param()", encoding="utf-8")
            executable = Path(tmp) / "reg-factory.exe"
            executable.write_bytes(b"")
            with patch.object(server, "ROOT", tmp), patch.object(
                server.sys, "frozen", True, create=True
            ), patch.object(server.sys, "executable", str(executable)):
                command = server._update_script()

        self.assertIsNotNone(command)
        self.assertIn("-InstallDir", command)
        self.assertIn(tmp, command)

    def test_frozen_macos_runtime_requires_release_update(self):
        with patch.object(server.sys, "frozen", True, create=True), patch.object(
            server.os, "name", "posix"
        ):
            self.assertIsNone(server._update_script())

    def test_completed_status_requires_structured_updater_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "update-result.json"
            result_path.write_text(
                json.dumps({
                    "status": "up_to_date",
                    "message": "Already up to date: v1.2.31",
                    "current_version": "1.2.31",
                    "target_version": "1.2.31",
                }),
                encoding="utf-8",
            )
            process = MagicMock()
            process.poll.return_value = 0
            server.UPDATE_PROCESS = process
            server.UPDATE_RESULT_PATH = str(result_path)
            server.UPDATE_STATE["status"] = "running"

            with patch.object(server, "_update_script", return_value=["updater"]):
                status = server._update_status()

        self.assertEqual(status["status"], "completed")
        self.assertIn("v1.2.31", status["message"])

    def test_zero_exit_without_result_is_not_reported_as_success(self):
        process = MagicMock()
        process.poll.return_value = 0
        server.UPDATE_PROCESS = process
        server.UPDATE_STATE["status"] = "running"

        with patch.object(server, "_update_script", return_value=["updater"]):
            status = server._update_status()

        self.assertEqual(status["status"], "failed")

    def test_ui_binds_one_click_update(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "webui/static/index.html").read_text(encoding="utf-8")
        app = (root / "webui/static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="btn-update"', html)
        self.assertIn("fetch('/api/update'", app)
        self.assertIn("location.reload()", app)

    def test_source_updater_does_not_kill_its_own_process_tree(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "update.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Stop-Process -Id $panelPid -Force", script)
        self.assertNotIn("taskkill.exe /PID $stopPid /T /F", script)


if __name__ == "__main__":
    unittest.main()
