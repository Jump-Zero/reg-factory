import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common import asset_scanner


class AssetScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "REG_FACTORY_DATA_DIR": str(self.root),
                "REG_FACTORY_ENV_FILE": str(self.root / ".env"),
                "TOKEN_OUTPUT_DIR": "tokens",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _write_assets(self):
        (self.root / "emails.txt").write_text(
            "mail@example.com----mail-pass----mail-rt----mail-client\n",
            encoding="utf-8",
        )
        cookie_root = self.root / "cookies"
        for platform, email, name, secret, domain in (
            ("chatgpt", "chat@example.com", "__Secure-next-auth.session-token", "chat-secret", ".chatgpt.com"),
            ("claude", "claude@example.com", "sessionKey", "claude-secret", ".claude.ai"),
        ):
            directory = cookie_root / platform
            directory.mkdir(parents=True)
            (directory / "accounts.txt").write_text(f"{email}|password|{secret}\n", encoding="utf-8")
            (directory / f"full_{platform}.json").write_text(
                json.dumps([{"name": name, "value": secret, "domain": domain, "path": "/"}]),
                encoding="utf-8",
            )
        grok = self.root / "tokens" / "grok"
        grok.mkdir(parents=True)
        (grok / "grok@example.com.sso.json").write_text(
            json.dumps({"email": "grok@example.com", "sso": "grok-secret"}),
            encoding="utf-8",
        )
        kiro = self.root / "tokens" / "kiro"
        kiro.mkdir(parents=True)
        (kiro / "kiro@example.com.account.json").write_text(
            json.dumps({"email": "kiro@example.com", "refreshToken": "kiro-secret"}),
            encoding="utf-8",
        )

    def test_inventory_contains_each_pool_without_secrets(self):
        self._write_assets()
        report = asset_scanner.get_report()

        self.assertEqual(report["summary"]["total"], 5)
        self.assertEqual({item["platform"] for item in report["items"]}, set(asset_scanner.PLATFORMS))
        encoded = json.dumps(report)
        for secret in ("mail-pass", "mail-rt", "chat-secret", "claude-secret", "grok-secret", "kiro-secret"):
            self.assertNotIn(secret, encoded)

    def test_scan_persists_results_and_progress_without_secrets(self):
        self._write_assets()
        outcomes = {
            "outlook": {"status": "normal", "detail": "mail ok", "evidence": "test"},
            "chatgpt": {"status": "banned", "detail": "chat banned", "evidence": "test"},
            "claude": {"status": "expired", "detail": "claude expired", "evidence": "test"},
            "grok": {"status": "restricted", "detail": "grok limited", "evidence": "test"},
            "kiro": {"status": "normal", "detail": "kiro ok", "evidence": "test"},
        }
        progress = []
        patches = [
            patch.object(asset_scanner, f"_scan_{platform}", return_value=outcome)
            for platform, outcome in outcomes.items()
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        with patch.object(asset_scanner, "_platform_preflight", return_value=None):
            with patch.dict(asset_scanner._SCANNERS, {
                platform: getattr(asset_scanner, f"_scan_{platform}") for platform in outcomes
            }, clear=True):
                report = asset_scanner.scan_pool(concurrency=2, progress=progress.append)

        self.assertEqual(report["summary"]["statuses"]["normal"], 2)
        self.assertEqual(report["summary"]["statuses"]["banned"], 1)
        self.assertEqual(progress[-1]["completed"], 5)
        cache_text = (self.root / "runtime" / "state" / "asset_pool_scan.json").read_text(encoding="utf-8")
        self.assertNotIn("chat-secret", cache_text)
        self.assertEqual(asset_scanner.get_report()["summary"]["statuses"]["restricted"], 1)

    def test_partial_scan_preserves_other_cached_platforms(self):
        self._write_assets()
        with patch.object(asset_scanner, "_platform_preflight", return_value=None):
            with patch.dict(asset_scanner._SCANNERS, {
                platform: (lambda _record, _timeout, p=platform: {
                    "status": "normal", "detail": p, "evidence": "test"
                })
                for platform in asset_scanner.PLATFORMS
            }, clear=True):
                asset_scanner.scan_pool()
                with patch.dict(asset_scanner._SCANNERS, {
                    "chatgpt": lambda _record, _timeout: {
                        "status": "expired", "detail": "new", "evidence": "test"
                    }
                }, clear=True):
                    report = asset_scanner.scan_pool(platforms=["chatgpt"])

        by_platform = {item["platform"]: item["status"] for item in report["items"]}
        self.assertEqual(by_platform["chatgpt"], "expired")
        self.assertEqual(by_platform["outlook"], "normal")

    def test_outlook_history_marks_unlock_without_refresh_token(self):
        (self.root / "emails.txt").write_text("locked@example.com----pw\n", encoding="utf-8")
        history = self.root / "unlock_results"
        history.mkdir()
        (history / "needs_phone_20260101.txt").write_text(
            "locked@example.com----pw----needs_phone\n", encoding="utf-8"
        )

        with patch.object(asset_scanner, "_platform_preflight", return_value=None):
            report = asset_scanner.scan_pool(platforms=["outlook"])

        self.assertEqual(report["items"][0]["status"], "unlock")
        self.assertIn("手机验证", report["items"][0]["detail"])

    def test_failed_preflight_short_circuits_platform_accounts(self):
        self._write_assets()
        failure = {"status": "error", "detail": "route timeout", "evidence": "preflight:timeout"}
        with patch.object(asset_scanner, "_platform_preflight", return_value=failure):
            with patch.object(asset_scanner, "_scan_chatgpt") as scanner:
                with patch.dict(asset_scanner._SCANNERS, {"chatgpt": scanner}, clear=True):
                    report = asset_scanner.scan_pool(platforms=["chatgpt"])

        scanner.assert_not_called()
        chatgpt = next(item for item in report["items"] if item["platform"] == "chatgpt")
        self.assertEqual(chatgpt["status"], "error")
        self.assertEqual(chatgpt["evidence"], "preflight:timeout")

    def test_plain_403_is_restricted_not_banned(self):
        response = SimpleNamespace(status_code=403, text="Cloudflare challenge")

        result = asset_scanner._response_status(response, "chatgpt_session")

        self.assertEqual(result["status"], "restricted")

    def test_explicit_account_deactivation_is_banned(self):
        response = SimpleNamespace(status_code=403, text='{"error":"account_deactivated"}')

        result = asset_scanner._response_status(response, "chatgpt_session")

        self.assertEqual(result["status"], "banned")

    def test_chatgpt_plus_trial_eligible_signal_is_labeled(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "state": "eligible",
            "redemption": {"redeemed": False, "redeemed_by_user": False},
        }
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {"email": "trial@example.com", "_token": {"account": {"planType": "free"}}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "eligible")
        self.assertIn("免费试用", result["plus_trial_detail"])
        self.assertEqual(session.get.call_args.kwargs["params"]["coupon"], "plus-1-month-free")

    def test_chatgpt_existing_paid_plan_skips_trial_request(self):
        record = {"email": "plus@example.com", "_token": {"account": {"planType": "plus"}}}
        with patch.object(asset_scanner, "_web_session") as session:
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "active")
        session.assert_not_called()

    def test_outlook_service_abuse_is_reported_as_banned(self):
        response = MagicMock(status_code=400)
        response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "User account is found to be in service abuse mode.",
        }
        session = MagicMock()
        session.post.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {
            "_mailbox": {"refresh_token": "rt", "client_id": "client"},
            "_history": None,
        }
        with patch.object(asset_scanner.requests, "Session", return_value=session):
            result = asset_scanner._scan_outlook(record, timeout=5)
        self.assertEqual(result["status"], "banned")
        self.assertEqual(result["evidence"], "microsoft_oauth:service_abuse")


if __name__ == "__main__":
    unittest.main()
