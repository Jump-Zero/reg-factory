import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class UpdateEntrypointTests(unittest.TestCase):
    def test_windows_updater_checks_tasks_and_restarts_verified_webui(self):
        script = (ROOT / "update.ps1").read_text(encoding="utf-8")
        self.assertIn("Assert-NoRunningTasks", script)
        self.assertIn("git -C $Root pull --ff-only", script)
        self.assertIn("uvicorn\\s+webui\\.server:app", script)
        self.assertIn("ParentProcessId", script)
        self.assertIn("belongs to another reg-factory installation", script)
        self.assertIn("Wait-ForUpdatedPanel", script)

    def test_unix_updater_checks_tasks_and_restarts_verified_webui(self):
        script = (ROOT / "update.sh").read_text(encoding="utf-8")
        self.assertIn("assert_no_running_tasks", script)
        self.assertIn('git -C "$ROOT" pull --ff-only', script)
        self.assertIn("wait_for_panel", script)
        self.assertIn('bash "$ROOT/start.sh"', script)

    def test_bootstrap_scripts_expose_update_action(self):
        powershell = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8")
        shell = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn('"update"', powershell)
        self.assertIn('REG_FACTORY_ACTION must be install, start, or update', powershell)
        self.assertIn('running.root', powershell)
        self.assertIn("update)", shell)
        self.assertIn("Action must be install, start, or update", shell)
        self.assertIn('get("root", "")', shell)

    def test_portable_updater_retries_and_verifies_release_package(self):
        script = (ROOT / "update-portable.ps1").read_text(encoding="utf-8")
        self.assertIn("Invoke-Download", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("update-result.json", (ROOT / "webui/server.py").read_text(encoding="utf-8"))
        self.assertIn("Downloaded package version", script)
        self.assertIn("Updated WebUI did not report version", script)


if __name__ == "__main__":
    unittest.main()
