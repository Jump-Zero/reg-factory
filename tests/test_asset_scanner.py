import json
import os
import tempfile
import threading
import time
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
                "ASSET_SCAN_CACHE_SECONDS": "0",
                "ASSET_SCAN_MIN_INTERVAL": "0",
                "ASSET_SCAN_MAX_INTERVAL": "0",
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
                    report = asset_scanner.scan_pool(platforms=["chatgpt"], force=True)

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

    def test_outlook_scan_reports_cross_platform_registration_usage(self):
        (self.root / "emails.txt").write_text(
            "used@outlook.com----pw1\nclean@outlook.com----pw2\n",
            encoding="utf-8",
        )
        (self.root / "emails_used_chatgpt.txt").write_text(
            "used@outlook.com----pw1----ok\n",
            encoding="utf-8",
        )

        report = asset_scanner.get_report()
        by_email = {item["email"]: item for item in report["items"]}

        self.assertFalse(by_email["used@outlook.com"]["pristine"])
        self.assertEqual(by_email["used@outlook.com"]["registered_platforms"], ["chatgpt"])
        self.assertTrue(by_email["clean@outlook.com"]["pristine"])

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

    def test_recent_scan_result_is_reused_without_live_request(self):
        self._write_assets()
        scanner = MagicMock(return_value={
            "status": "normal", "detail": "mail ok", "evidence": "test:200",
        })
        with patch.dict(os.environ, {"ASSET_SCAN_CACHE_SECONDS": "21600"}):
            with patch.object(asset_scanner, "_platform_preflight", return_value=None):
                with patch.dict(asset_scanner._SCANNERS, {"outlook": scanner}, clear=True):
                    first = asset_scanner.scan_pool(platforms=["outlook"], force=True)
                    scanner.reset_mock()
                    second = asset_scanner.scan_pool(platforms=["outlook"])

        scanner.assert_not_called()
        self.assertEqual(second["summary"]["statuses"]["normal"], 1)
        self.assertEqual(second["items"][0]["checked_at"], first["items"][0]["checked_at"])

    def test_rate_limit_pauses_remaining_platform_records(self):
        records = [
            {"id": f"mail-{index}", "platform": "outlook", "email": f"mail{index}@example.com"}
            for index in range(3)
        ]
        limited = {
            **records[0],
            "status": "restricted",
            "detail": "rate limited",
            "evidence": "microsoft_graph:429",
            "checked_at": "2026-08-11T00:00:00Z",
        }
        with patch.object(asset_scanner, "_scan_record", return_value=limited) as scan_record:
            results = asset_scanner._scan_platform_safely("outlook", records, 15, 0, 0)

        scan_record.assert_called_once_with(records[0], 15)
        self.assertEqual([item["status"] for item in results], ["restricted", "unknown", "unknown"])
        self.assertIn("circuit_breaker:rate_limited", results[1]["evidence"])

    def test_account_concurrency_scans_one_platform_in_bounded_batches(self):
        records = [
            {"id": f"mail-{index}", "platform": "outlook", "email": f"mail{index}@example.com"}
            for index in range(4)
        ]
        lock = threading.Lock()
        active = 0
        peak = 0

        def scan(record, _timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {
                **record,
                "status": "normal",
                "detail": "ok",
                "evidence": "test:200",
            }

        with patch.object(asset_scanner, "_scan_record", side_effect=scan):
            results = asset_scanner._scan_platform_safely(
                "outlook", records, 15, 0, 0, account_concurrency=2
            )

        self.assertEqual(len(results), 4)
        self.assertEqual(peak, 2)

    def test_report_inventory_is_cached_between_webui_polls(self):
        record = {
            "id": "cached-one",
            "platform": "outlook",
            "kind": "mailbox",
            "email": "cached@example.com",
            "email_provider": "other",
            "source": "emails.txt:1",
        }
        asset_scanner.invalidate_report_cache()
        with patch.object(asset_scanner, "_inventory_records", return_value=[record]) as inventory:
            first = asset_scanner.get_report()
            second = asset_scanner.get_report()

        self.assertEqual(first, second)
        inventory.assert_called_once()

    def test_fast_health_scan_skips_optional_plus_request(self):
        record = {"id": "chat", "platform": "chatgpt", "email": "chat@example.com"}
        health = {
            "status": "normal",
            "detail": "ok",
            "evidence": "test:200",
            "_access_token": "access-token",
        }
        with patch.dict(asset_scanner._SCANNERS, {"chatgpt": lambda *_args: health}, clear=True):
            with patch.object(asset_scanner, "_scan_chatgpt_plus_trial") as plus:
                result = asset_scanner._scan_record(record, 5, include_plus_trial=False)

        plus.assert_not_called()
        self.assertEqual(result["plus_trial"], "disabled")

    def test_plain_403_is_restricted_not_banned(self):
        response = SimpleNamespace(status_code=403, text="Cloudflare challenge")

        result = asset_scanner._response_status(response, "chatgpt_session")

        self.assertEqual(result["status"], "restricted")

    def test_explicit_account_deactivation_is_banned(self):
        response = SimpleNamespace(status_code=403, text='{"error":"account_deactivated"}')

        result = asset_scanner._response_status(response, "chatgpt_session")

        self.assertEqual(result["status"], "banned")

    def test_claude_free_account_is_normal_and_labeled(self):
        payload = {
            "uuid": "account-id",
            "email_address": "free@example.com",
            "is_verified": True,
            "memberships": [{
                "role": "admin",
                "seat_tier": None,
                "organization": {
                    "uuid": "organization-id",
                    "billing_type": None,
                    "rate_limit_tier": "default_claude_ai",
                    "capabilities": ["chat"],
                },
            }],
        }
        response = SimpleNamespace(
            status_code=200,
            url="https://claude.ai/api/account",
            text="",
            json=lambda: payload,
        )
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_claude(
                {"email": "free@example.com", "_token": {"sessionKey": "secret"}},
                10,
            )

        self.assertEqual(result["status"], "normal")
        self.assertEqual(result["plan_type"], "free")
        self.assertIn("Claude Free", result["detail"])

    def test_claude_account_without_membership_is_not_normal(self):
        response = SimpleNamespace(
            status_code=200,
            url="https://claude.ai/api/account",
            text="",
            json=lambda: {"uuid": "account-id", "memberships": []},
        )
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_claude(
                {"email": "incomplete@example.com", "_token": {"sessionKey": "secret"}},
                10,
            )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["evidence"], "claude_account:no_membership")

    def test_grok_sso_does_not_require_optional_oauth_import(self):
        response = SimpleNamespace(
            status_code=200,
            url="https://accounts.x.ai/",
            text="",
        )
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        base = {"email": "grok@example.com", "_token": {"sso": "secret"}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            unverified = asset_scanner._scan_grok(base, 10)
            failed = asset_scanner._scan_grok(
                {
                    **base,
                    "_token": {
                        "sso": "secret",
                        "authorization_status": "failed",
                    },
                },
                10,
            )

        self.assertEqual(unverified["status"], "normal")
        self.assertEqual(unverified["evidence"], "xai_account:200:sso_only")
        self.assertEqual(failed["status"], "restricted")
        self.assertEqual(failed["evidence"], "local:grok_oauth_failed")

        marker = self.root / "tokens" / "grok" / "uploaded_sub2api.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text("grok@example.com\n", encoding="utf-8")
        with patch.object(asset_scanner, "_web_session", return_value=session):
            authorized = asset_scanner._scan_grok(base, 10)

        self.assertEqual(authorized["status"], "normal")
        self.assertIn("oauth_authorized", authorized["evidence"])

    def test_chatgpt_plus_campaign_without_price_is_not_called_zero_price(self):
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

        self.assertEqual(result["plus_trial"], "unknown")
        self.assertIn("0 元", result["plus_trial_detail"])
        self.assertEqual(session.get.call_args.kwargs["params"]["coupon"], "plus-1-month-free")

    def test_chatgpt_coupon_100_percent_discount_is_zero_price(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "state": "eligible",
            "discount": {"percentage": 100},
            "redemption": {"redeemed_by_user": False},
        }
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {"email": "zero-discount@example.com", "_token": {"planType": "free"}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "zero_price")
        self.assertIn("100%", result["plus_trial_detail"])

    def test_chatgpt_accounts_check_100_percent_campaign_is_zero_price(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-trial",
                            "metadata": {
                                "title": "Plus trial",
                                "discount": {"percentage": 100},
                                "duration": {"num_periods": 1, "period": "month"},
                            },
                        }
                    },
                }
            }
        }
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {"email": "trial@example.com", "_token": {"planType": "free"}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "zero_price")
        self.assertEqual(result["plus_trial_campaign_id"], "plus-trial")
        self.assertEqual(result["plus_trial_evidence"], "accounts_check:200:zero_price")
        self.assertEqual(
            session.get.call_args.kwargs["params"], {"timezone_offset_min": "-"}
        )

    def test_chatgpt_accounts_check_discount_is_not_zero_price(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {
                        "plus": {
                            "id": "plus-discount",
                            "metadata": {
                                "title": "Half price",
                                "discount": {"percentage": 50},
                            },
                        }
                    },
                }
            }
        }
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {"email": "discount@example.com", "_token": {"planType": "free"}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "discount")
        self.assertIn("50%", result["plus_trial_detail"])

    def test_chatgpt_accounts_check_without_campaign_is_ineligible(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free"},
                    "entitlement": {"subscription_plan": "chatgptfreeplan"},
                    "eligible_promo_campaigns": {},
                }
            }
        }
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {"email": "free@example.com", "_token": {"planType": "free"}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "ineligible")
        self.assertEqual(result["plus_trial_evidence"], "accounts_check:200:no_plus_campaign")
        session.get.assert_called_once()

    def test_chatgpt_explicit_zero_price_offer_is_labeled(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "state": "available",
            "offer": {"checkout": {"amount_due": 0, "currency": "USD"}},
        }
        session = MagicMock()
        session.get.return_value = response
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        record = {"email": "zero@example.com", "_token": {"planType": "free"}}

        with patch.object(asset_scanner, "_web_session", return_value=session):
            result = asset_scanner._scan_chatgpt_plus_trial(
                record, "access-token", 10
            )

        self.assertEqual(result["plus_trial"], "zero_price")
        self.assertIn("0 元", result["plus_trial_detail"])
        self.assertIn("offer.checkout.amount_due", result["plus_trial_evidence"])

    def test_zero_discount_does_not_count_as_zero_price_offer(self):
        payload = {
            "state": "available",
            "discount": {"total": 0},
            "discount_amount": 0,
        }

        self.assertEqual(asset_scanner._zero_price_offer(payload), "")

    def test_chatgpt_inventory_exposes_registration_country_and_network(self):
        token_root = self.root / "tokens" / "chatgpt"
        token_root.mkdir(parents=True)
        (token_root / "country.session.json").write_text(
            json.dumps({
                "accessToken": "asset-token",
                "user": {"email": "country@example.com"},
                "registration_country": "jp",
                "network_node": "Japan 01",
            }),
            encoding="utf-8",
        )

        report = asset_scanner.get_report()
        item = next(entry for entry in report["items"] if entry["email"] == "country@example.com")

        self.assertEqual(item["registration_country"], "JP")
        self.assertEqual(item["network_node"], "Japan 01")

    def test_chatgpt_existing_paid_plan_skips_trial_request(self):
        record = {"email": "plus@example.com", "_token": {"account": {"planType": "plus"}}}
        with patch.object(asset_scanner, "_web_session") as session:
            result = asset_scanner._scan_chatgpt_plus_trial(record, "access-token", 10)

        self.assertEqual(result["plus_trial"], "active")
        session.assert_not_called()

    def test_targeted_chatgpt_plus_check_updates_public_cache_without_token(self):
        token_root = self.root / "tokens" / "chatgpt"
        token_root.mkdir(parents=True)
        session = {
            "accessToken": "secret-access-token",
            "user": {"email": "new-trial@example.com"},
            "account": {"planType": "free"},
            "registration_country": "SG",
            "network_node": "Singapore 01",
        }
        (token_root / "new-trial@example.com.session.json").write_text(
            json.dumps(session), encoding="utf-8"
        )
        with patch.object(
            asset_scanner,
            "_scan_chatgpt_plus_trial",
            return_value={
                "plus_trial": "zero_price",
                "plus_trial_detail": "明确 0 元",
                "plus_trial_evidence": "promo_campaign:200:zero:offer.amount_due",
            },
        ) as check:
            result = asset_scanner.check_chatgpt_plus_trial_for_session(
                session, "new-trial@example.com", timeout=15
            )

        self.assertEqual(result["plus_trial"], "zero_price")
        check.assert_called_once()
        report = asset_scanner.get_report()
        item = next(
            entry for entry in report["items"] if entry["email"] == "new-trial@example.com"
        )
        self.assertEqual(item["plus_trial"], "zero_price")
        self.assertEqual(item["registration_country"], "SG")
        cache_text = (self.root / "runtime" / "state" / "asset_pool_scan.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("secret-access-token", cache_text)

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
