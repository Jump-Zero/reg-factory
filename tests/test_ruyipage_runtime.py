import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from common import ruyipage_runtime
from common.ruyipage_browser import RuyiPageBrowser
from tools import install_ruyipage
from webui import server as webui_server


class RuyiPageRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.state = patch.object(
            ruyipage_runtime,
            "_STATE",
            {"state": "idle", "message": "尚未检测", "path": ""},
        )
        self.state.start()
        self.addCleanup(self.state.stop)

    def test_existing_runtime_is_reused_without_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "firefox.exe"
            executable.write_bytes(b"fixture")
            with (
                patch("ruyipage.resolve_firefox_path", return_value=str(executable)),
                patch("ruyipage._runtime.install") as install,
            ):
                result = ruyipage_runtime.ensure_runtime()

        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["cached"])
        install.assert_not_called()

    def test_missing_runtime_is_installed_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "firefox.exe"
            executable.write_bytes(b"fixture")
            with (
                patch("ruyipage.resolve_firefox_path", return_value=None),
                patch(
                    "ruyipage._runtime.install",
                    return_value={"executable_path": str(executable), "cached": False},
                ) as install,
            ):
                result = ruyipage_runtime.ensure_runtime()

        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["cached"])
        install.assert_called_once_with(force=False)

    def test_invalid_explicit_path_is_not_silently_replaced(self):
        missing = os.path.join(tempfile.gettempdir(), "missing-ruyipage-firefox.exe")
        with (
            patch("ruyipage.resolve_firefox_path", return_value=missing),
            patch("ruyipage._runtime.install") as install,
        ):
            with self.assertRaisesRegex(RuntimeError, "RUYIPAGE_BROWSER_PATH 不存在"):
                ruyipage_runtime.ensure_runtime(missing)

        install.assert_not_called()

    def test_browser_first_use_installs_before_launch(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": directory}, clear=False):
                    browser = RuyiPageBrowser()
                    profile_id = browser.create_browser("auto-install", proxyType="noproxy")
                    with (
                        patch(
                            "common.ruyipage_runtime.runtime_status",
                            return_value={"state": "missing", "message": "missing", "path": ""},
                        ),
                        patch(
                            "common.ruyipage_runtime.ensure_runtime",
                            return_value={"state": "ready", "path": "C:/runtime/firefox.exe"},
                        ) as ensure,
                        patch(
                            "ruyipage.aio.launch",
                            AsyncMock(side_effect=RuntimeError("launch-sentinel")),
                        ) as launch,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "launch-sentinel"):
                            await browser.open_browser_async(profile_id)

                    ensure.assert_called_once_with("")
                    self.assertEqual(launch.await_args.kwargs["browser_path"], "C:/runtime/firefox.exe")

        asyncio.run(exercise())

    def test_webui_startup_schedules_background_install(self):
        async def exercise():
            def config_value(key, default=""):
                if key == "RUYIPAGE_BROWSER_PATH":
                    return ""
                if key == "K12_AUTO_START":
                    return "0"
                return default

            webui_server.RUYIPAGE_INSTALL_TASK = None
            with (
                patch.object(webui_server, "_fingerprint_provider", return_value="ruyipage"),
                patch.object(webui_server, "_read_config_val", side_effect=config_value),
                patch.object(webui_server, "_start_plus_service_sync"),
                patch.object(
                    ruyipage_runtime,
                    "runtime_status",
                    return_value={"state": "missing", "message": "missing", "path": ""},
                ),
                patch.object(
                    ruyipage_runtime,
                    "ensure_runtime",
                    return_value={"state": "ready", "path": "C:/runtime/firefox.exe"},
                ) as ensure,
            ):
                await webui_server.startup_local_services()
                await webui_server.RUYIPAGE_INSTALL_TASK

            ensure.assert_called_once_with("")
            webui_server.RUYIPAGE_INSTALL_TASK = None

        asyncio.run(exercise())

    def test_manual_retry_reports_cached_runtime_path(self):
        path = "C:/runtime/firefox.exe"
        with (
            patch.object(
                install_ruyipage,
                "runtime_status",
                return_value={"state": "ready", "path": path},
            ),
            patch.object(install_ruyipage, "ensure_runtime") as ensure,
            patch("builtins.print") as output,
        ):
            install_ruyipage.main()

        ensure.assert_not_called()
        output.assert_called_once_with(f"[ruyipage] already installed: {path}")

    def test_manual_installer_runs_outside_project_directory(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "install_ruyipage.py"
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "firefox.exe"
            executable.write_bytes(b"fixture")
            env = dict(os.environ)
            env["RUYIPAGE_BROWSER_PATH"] = str(executable)
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("[ruyipage] already installed:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
