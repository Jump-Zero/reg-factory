import importlib.util
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reg-factory-server.py"
SPEC = importlib.util.spec_from_file_location("reg_factory_release_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


class ReleaseLauncherTests(unittest.TestCase):
    def test_reuses_service_only_when_version_matches(self):
        availability = {8799: False, 8800: False}

        def status(port):
            versions = {8799: "1.2.0", 8800: "1.2.1"}
            return {"version": versions[port], "browser_provider": "bitbrowser", "running": 0}

        with patch.object(
            launcher,
            "_port_available",
            side_effect=lambda _host, port: availability.get(port, True),
        ):
            with patch.object(launcher, "_existing_reg_factory", side_effect=status):
                self.assertEqual(
                    launcher._select_port("127.0.0.1", 8799, "1.2.1"),
                    (8800, True),
                )

    def test_old_service_moves_new_version_to_first_free_port(self):
        with patch.object(launcher, "_port_available", side_effect=lambda _host, port: port != 8799):
            with patch.object(
                launcher,
                "_existing_reg_factory",
                return_value={"version": "1.2.0", "browser_provider": "bitbrowser", "running": 0},
            ):
                self.assertEqual(
                    launcher._select_port("127.0.0.1", 8799, "1.2.1"),
                    (8800, False),
                )

    def test_free_requested_port_starts_current_version(self):
        with patch.object(launcher, "_port_available", return_value=True):
            with patch.object(launcher, "_existing_reg_factory") as existing:
                self.assertEqual(
                    launcher._select_port("127.0.0.1", 8799, "1.2.1"),
                    (8799, False),
                )
        existing.assert_not_called()

    def test_finds_assets_from_running_source_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "emails.txt").write_text("mail@example.com----password\n", encoding="utf-8")

            def status(port):
                if port == 8799:
                    return {
                        "version": "old-source",
                        "root": str(root),
                        "browser_provider": "bitbrowser",
                        "running": 0,
                    }
                return None

            with patch.object(launcher, "_existing_reg_factory", side_effect=status):
                self.assertEqual(launcher._running_data_root(), root.resolve())

    def test_ignores_running_bundle_root_without_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            status = {
                "version": "old-bundle",
                "root": directory,
                "browser_provider": "bitbrowser",
                "running": 0,
            }
            with patch.object(launcher, "_existing_reg_factory", return_value=status):
                self.assertIsNone(launcher._running_data_root())

    def test_portable_package_adopts_ancestor_project_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "dist" / "reg-factory-windows-x64-1.2.21"
            install.mkdir(parents=True)
            executable = install / "reg-factory.exe"
            (root / ".env").write_text("SUB2API_URL=http://local\n", encoding="utf-8")
            (root / "emails.txt").write_text("mail@example.com\n", encoding="utf-8")

            with patch.object(launcher.sys, "frozen", True, create=True):
                with patch.object(launcher.sys, "executable", str(executable)):
                    self.assertEqual(
                        launcher._portable_ancestor_data_root(), root.resolve()
                    )

    def test_data_root_falls_back_to_portable_ancestor(self):
        root = Path("E:/reg-factory")
        with patch.object(launcher.sys, "frozen", True, create=True):
            with patch.object(launcher, "_running_data_root", return_value=None):
                with patch.object(
                    launcher, "_portable_ancestor_data_root", return_value=root
                ):
                    with patch.dict(launcher.os.environ, {}, clear=True):
                        with patch.object(Path, "is_file", return_value=True):
                            launcher._adopt_running_data_root()
                        self.assertEqual(
                            launcher.os.environ["REG_FACTORY_DATA_DIR"], str(root)
                        )
                        self.assertEqual(
                            launcher.os.environ["REG_FACTORY_ENV_FILE"],
                            str(root / ".env"),
                        )

    def test_proxy_test_applies_current_form_before_request(self):
        source = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        function = source.split("async function testProxy(){", 1)[1].split("\n}", 1)[0]
        self.assertLess(function.index("applyProxyConfig()"), function.index("/api/proxy/test"))

    def test_proxy_requests_handle_non_json_server_errors(self):
        source = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        parser = source.split("async function readJsonResponse(response){", 1)[1].split("\n}", 1)[0]
        self.assertIn("response.text()", parser)
        self.assertIn("JSON.parse(text)", parser)
        for name in ("applyProxyConfig", "rotateProxy", "testProxy"):
            function = source.split(f"async function {name}(", 1)[1].split("\n}", 1)[0]
            self.assertIn("readJsonResponse(response)", function)

    def test_task_exit_code_does_not_trigger_desktop_startup_pause(self):
        with patch.object(launcher, "main", side_effect=SystemExit(2)):
            with patch.object(launcher, "_pause_after_error") as pause:
                with self.assertRaisesRegex(SystemExit, "2"):
                    launcher._entrypoint()
        pause.assert_not_called()

    def test_frozen_task_output_is_line_buffered_and_write_through(self):
        stdout = MagicMock()
        stderr = MagicMock()
        with patch.object(launcher.sys, "stdout", stdout):
            with patch.object(launcher.sys, "stderr", stderr):
                with patch.dict(launcher.os.environ, {}, clear=True):
                    launcher._configure_live_output()

        stdout.reconfigure.assert_called_once_with(line_buffering=True, write_through=True)
        stderr.reconfigure.assert_called_once_with(line_buffering=True, write_through=True)

    def test_frozen_task_failure_never_waits_for_console_input(self):
        with patch.object(launcher.sys, "frozen", True, create=True):
            with patch.object(launcher, "_TASK_DISPATCH", True):
                with patch("builtins.input") as prompt:
                    launcher._pause_after_error()

        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
