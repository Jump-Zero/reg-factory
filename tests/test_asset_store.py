import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import asset_store


class AssetStoreTests(unittest.TestCase):
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

    def test_email_provider_classification_and_filtering(self):
        self.assertEqual(asset_store.classify_email_provider("a@outlook.com"), "outlook")
        self.assertEqual(asset_store.classify_email_provider("a@outlook.jp"), "outlook")
        self.assertEqual(asset_store.classify_email_provider("a@outlook.eu"), "outlook")
        self.assertEqual(asset_store.classify_email_provider("a@hotmail.co.uk"), "outlook")
        self.assertEqual(asset_store.classify_email_provider("a@icloud.com"), "icloud")
        self.assertEqual(asset_store.classify_email_provider("a@mail.tm"), "temporary")
        self.assertEqual(asset_store.classify_email_provider("a@example.com"), "other")
        (self.root / "emails.txt").write_text(
            "outlook@outlook.com----pw\n"
            "icloud@icloud.com----pw\n"
            "temp@mail.tm----pw\n",
            encoding="utf-8",
        )
        self.assertEqual(asset_store.get_email(index=0, email_provider="icloud")["email_provider"], "icloud")
        self.assertEqual(asset_store.get_email(index=0, email_provider="temporary")["email_provider"], "temporary")

    def test_email_sequence_and_explicit_index(self):
        (self.root / "emails.txt").write_text(
            "first@example.com----pw1----rt1----cid1\n"
            "second@example.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )

        first = asset_store.get_email()
        explicit = asset_store.get_email(index=0, output_format="line")
        second = asset_store.get_email()

        self.assertEqual(first["index"], 0)
        self.assertEqual(first["data"]["email"], "first@example.com")
        self.assertFalse(explicit["cursor_advanced"])
        self.assertEqual(explicit["data"], "first@example.com----pw1----rt1----cid1")
        self.assertEqual(second["index"], 1)
        with self.assertRaises(asset_store.AssetExhausted):
            asset_store.get_email()

    def test_pristine_email_claim_excludes_other_platform_usage(self):
        (self.root / "emails.txt").write_text(
            "used@outlook.com----pw1----rt1----cid1\n"
            "clean@outlook.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )
        (self.root / "emails_used_tri.txt").write_text(
            "used@outlook.com----pw1----reserved\n",
            encoding="utf-8",
        )

        result = asset_store.get_email(claim_once=True, pristine_only=True)

        self.assertEqual(result["data"]["email"], "clean@outlook.com")
        self.assertTrue(result["pristine"])
        self.assertEqual(
            (self.root / "runtime" / "state" / "outlook_sale_emails.txt").read_text(encoding="utf-8").strip(),
            "clean@outlook.com",
        )
        self.assertEqual(asset_store.registered_mailbox_usage(), {
            "used@outlook.com": ("tri",),
        })

    def test_default_outlook_sale_excludes_platform_reserved_mailbox(self):
        (self.root / "emails.txt").write_text(
            "used@outlook.com----pw1----rt1----cid1\n"
            "clean@outlook.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )
        (self.root / "emails_used_claude.txt").write_text(
            "used@outlook.com----pw1----reserved\n",
            encoding="utf-8",
        )

        result = asset_store.get_email(claim_once=True)

        self.assertEqual(result["data"]["email"], "clean@outlook.com")

    def test_outlook_sale_stops_when_every_mailbox_was_used_for_registration(self):
        (self.root / "emails.txt").write_text(
            "used@outlook.com----pw1----rt1----cid1\n",
            encoding="utf-8",
        )
        (self.root / "emails_error_chatgpt.txt").write_text(
            "used@outlook.com----pw1----challenge_after_email\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(asset_store.AssetNotFound, "单独售卖"):
            asset_store.get_email(claim_once=True)

    def test_outlook_sale_reads_permanent_registration_exclusion_ledger(self):
        (self.root / "emails.txt").write_text(
            "used@outlook.com----pw1----rt1----cid1\n"
            "clean@outlook.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )
        state = self.root / "runtime" / "state"
        state.mkdir(parents=True)
        (state / "outlook_registration_emails.txt").write_text(
            "\ufeffused@outlook.com\n", encoding="utf-8"
        )

        result = asset_store.get_email(claim_once=True)

        self.assertEqual(result["data"]["email"], "clean@outlook.com")

    def test_no_graph_mailbox_claim_delivers_only_email_and_password(self):
        (self.root / "outlook_no_graph.txt").write_text(
            "registered@outlook.com----password----ignored-extra\n",
            encoding="utf-8",
        )

        result = asset_store.get_email(
            output_format="password", claim_once=True, no_graph_only=True
        )

        self.assertEqual(result["data"], "registered@outlook.com----password")
        self.assertTrue(result["no_graph_only"])
        self.assertTrue(result["claim_recorded"])
        self.assertEqual(result["claim_scope"], "outlook")
        self.assertEqual(
            (self.root / "runtime" / "state" / "outlook_sale_emails.txt").read_text(encoding="utf-8").strip(),
            "registered@outlook.com",
        )

    def test_no_graph_claim_prevents_later_four_field_sale(self):
        (self.root / "outlook_no_graph.txt").write_text(
            "same@outlook.com----password\n", encoding="utf-8"
        )
        (self.root / "emails.txt").write_text(
            "same@outlook.com----password----rt----client\n", encoding="utf-8"
        )
        asset_store.get_email(
            output_format="password", claim_once=True, no_graph_only=True
        )
        with self.assertRaises(asset_store.AssetExhausted):
            asset_store.get_email(output_format="line", claim_once=True)

    def test_pristine_email_claim_conservatively_excludes_failed_usage(self):
        (self.root / "emails.txt").write_text(
            "attempted@outlook.com----pw----rt----cid\n",
            encoding="utf-8",
        )
        (self.root / "emails_error_chatgpt.txt").write_text(
            "attempted@outlook.com----pw----challenge_after_email\n",
            encoding="utf-8",
        )

        with self.assertRaises(asset_store.AssetNotFound):
            asset_store.get_email(claim_once=True, pristine_only=True)

    def test_pristine_email_claim_excludes_stored_platform_credentials(self):
        (self.root / "emails.txt").write_text(
            "registered@outlook.com----pw1----rt1----cid1\n"
            "clean@outlook.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )
        token_dir = self.root / "tokens" / "chatgpt"
        token_dir.mkdir(parents=True)
        (token_dir / "registered.session.json").write_text(json.dumps({
            "email": "registered@outlook.com",
            "accessToken": "secret",
        }), encoding="utf-8")

        result = asset_store.get_email(claim_once=True, pristine_only=True)

        self.assertEqual(result["data"]["email"], "clean@outlook.com")
        self.assertEqual(asset_store.registered_mailbox_usage()["registered@outlook.com"], ("chatgpt",))

    def _write_chatgpt_assets(self):
        cookie_dir = self.root / "cookies" / "chatgpt"
        cookie_dir.mkdir(parents=True)
        cookie_value = "cookie-secret"
        (cookie_dir / "accounts.txt").write_text(
            f"user@example.com|password|{cookie_value}\n", encoding="utf-8"
        )
        (cookie_dir / "full_profile_20260101_000000.json").write_text(
            json.dumps([
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": cookie_value,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "expires": 1893456000,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {"name": "noise", "value": "ignored", "domain": ".example.com", "path": "/"},
            ]),
            encoding="utf-8",
        )
        token_dir = self.root / "tokens" / "chatgpt"
        token_dir.mkdir(parents=True)
        session = {
            "user": {"email": "user@example.com"},
            "account": {"id": "account-1", "planType": "free"},
            "accessToken": "access-token",
            "expires": "2030-01-01T00:00:00Z",
        }
        (token_dir / "user@example.com.session.json").write_text(
            json.dumps(session), encoding="utf-8"
        )

    def test_chatgpt_cookie_and_downstream_formats(self):
        self._write_chatgpt_assets()

        raw = asset_store.get_platform_asset("chatgpt", "raw", index=0)
        cookies = asset_store.get_platform_asset("chatgpt", "cookies", index=0)
        header = asset_store.get_platform_asset("chatgpt", "header", index=0)
        sub2api = asset_store.get_platform_asset("chatgpt", "sub2api", index=0)
        cpa = asset_store.get_platform_asset("chatgpt", "cpa", index=0)
        chatgpt2api = asset_store.get_platform_asset("chatgpt", "chatgpt2api", index=0)

        self.assertEqual(raw["email"], "user@example.com")
        self.assertEqual(len(raw["data"]), 1)
        self.assertEqual(cookies["format"], "cookies")
        self.assertEqual(cookies["data"][0]["sameSite"], "no_restriction")
        self.assertEqual(cookies["data"][0]["expirationDate"], 1893456000.0)
        self.assertFalse(cookies["data"][0]["hostOnly"])
        self.assertFalse(cookies["data"][0]["session"])
        self.assertEqual(cookies["data"][0]["storeId"], "0")
        self.assertIn("__Secure-next-auth.session-token=cookie-secret", header["data"])
        self.assertEqual(json.loads(sub2api["data"]["content"])["accessToken"], "access-token")
        self.assertEqual(cpa["data"]["type"], "codex")
        self.assertEqual(cpa["data"]["access_token"], "access-token")
        self.assertEqual(chatgpt2api["data"]["source_type"], "web")

    def test_verified_only_returns_normal_assets_and_blocks_unhealthy_pool(self):
        self._write_chatgpt_assets()
        from common import asset_scanner

        normal = {
            "items": [{
                "platform": "chatgpt",
                "email": "user@example.com",
                "source": "full_profile_20260101_000000.json",
                "status": "normal",
                "checked_at": "2026-08-04T10:00:00Z",
                "evidence": "chatgpt_session:200",
            }],
        }
        with patch.object(asset_scanner, "get_report", return_value=normal):
            result = asset_store.get_platform_asset("chatgpt", "raw", index=0, verified_only=True)

        self.assertEqual(result["email"], "user@example.com")
        self.assertEqual(result["verification"]["status"], "normal")
        self.assertEqual(result["verification"]["evidence"], "chatgpt_session:200")

        with patch.object(asset_scanner, "get_report", return_value={"items": []}):
            with self.assertRaises(asset_store.AssetUnverified):
                asset_store.get_platform_asset("chatgpt", "raw", index=0, verified_only=True)

    def test_chatgpt_codex_phone_status_filters_verified_oauth_credentials(self):
        token_dir = self.root / "tokens" / "chatgpt"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "verified.session.json").write_text(json.dumps({
            "email": "verified@example.com",
            "access_token": "verified-access",
            "codex_phone_status": "verified",
        }), encoding="utf-8")
        (token_dir / "regular.session.json").write_text(json.dumps({
            "email": "regular@example.com",
            "accessToken": "regular-access",
        }), encoding="utf-8")
        from common import asset_scanner

        report = {"items": [
            {"platform": "chatgpt", "email": "verified@example.com", "status": "normal"},
            {"platform": "chatgpt", "email": "regular@example.com", "status": "normal"},
        ]}
        with patch.object(asset_scanner, "get_report", return_value=report):
            verified = asset_store.get_platform_asset("chatgpt", "session", verified_only=True, codex_phone_status="verified")
            asset_store.reset_cursor("chatgpt")
            regular = asset_store.get_platform_asset("chatgpt", "session", verified_only=True, codex_phone_status="not_verified")

        self.assertEqual(verified["email"], "verified@example.com")
        self.assertEqual(verified["codex_phone_status"], "verified")
        self.assertEqual(regular["email"], "regular@example.com")
        self.assertEqual(regular["codex_phone_status"], "not_verified")

    def test_verified_email_claims_are_one_time_and_resettable(self):
        (self.root / "emails.txt").write_text(
            "first@example.com----pw1----rt1----cid1\n"
            "second@example.com----pw2----rt2----cid2\n"
            "banned@example.com----pw3----rt3----cid3\n",
            encoding="utf-8",
        )
        from common import asset_scanner

        report = {
            "items": [
                {"platform": "outlook", "email": "first@example.com", "status": "normal"},
                {"platform": "outlook", "email": "second@example.com", "status": "normal"},
                {"platform": "outlook", "email": "banned@example.com", "status": "banned"},
            ],
        }
        with patch.object(asset_scanner, "get_report", return_value=report):
            first = asset_store.get_email(verified_only=True)
            second = asset_store.get_email(verified_only=True)
            with self.assertRaises(asset_store.AssetExhausted):
                asset_store.get_email(verified_only=True)
            reset = asset_store.reset_cursor("outlook")
            repeated = asset_store.get_email(verified_only=True)

        self.assertEqual(first["data"]["email"], "first@example.com")
        self.assertEqual(first["remaining"], 1)
        self.assertEqual(second["data"]["email"], "second@example.com")
        self.assertEqual(second["remaining"], 0)
        self.assertEqual(reset["claims_removed"], 2)
        self.assertEqual(repeated["data"]["email"], "first@example.com")

    def test_verified_claim_is_shared_across_platform_output_formats(self):
        self._write_chatgpt_assets()
        from common import asset_scanner

        report = {
            "items": [{
                "platform": "chatgpt",
                "email": "user@example.com",
                "source": "full_profile_20260101_000000.json,user@example.com.session.json",
                "status": "normal",
                "checked_at": "2026-08-09T00:00:00Z",
            }],
        }
        with patch.object(asset_scanner, "get_report", return_value=report):
            raw = asset_store.get_platform_asset("chatgpt", "raw", verified_only=True)
            with self.assertRaises(asset_store.AssetExhausted):
                asset_store.get_platform_asset("chatgpt", "sub2api", verified_only=True)
            reset = asset_store.reset_cursor("verified:cookie:chatgpt:raw")
            converted = asset_store.get_platform_asset(
                "chatgpt", "sub2api", verified_only=True
            )

        self.assertTrue(raw["claim_recorded"])
        self.assertEqual(raw["claim_scope"], "chatgpt")
        self.assertEqual(reset["claim_scopes_removed"], ["chatgpt"])
        self.assertEqual(converted["email"], "user@example.com")

    def test_direct_claim_never_requires_scan_and_is_shared_across_formats(self):
        self._write_chatgpt_assets()
        from common import asset_scanner

        with patch.object(asset_scanner, "get_report", side_effect=AssertionError("scan read")):
            raw = asset_store.get_platform_asset("chatgpt", "raw", claim_once=True)
            with self.assertRaises(asset_store.AssetExhausted):
                asset_store.get_platform_asset("chatgpt", "sub2api", claim_once=True)

        self.assertTrue(raw["claim_recorded"])
        self.assertNotIn("verification", raw)
        self.assertEqual(raw["remaining"], 0)

    def test_grok_sub2api_and_summary(self):
        token_dir = self.root / "tokens" / "grok"
        token_dir.mkdir(parents=True)
        (token_dir / "grok@example.com.sso.json").write_text(
            json.dumps({"email": "grok@example.com", "sso": "sso-token"}), encoding="utf-8"
        )

        result = asset_store.get_platform_asset("grok", "sub2api", index=0)
        summary = asset_store.summary()

        self.assertEqual(result["data"]["sso_tokens"], ["sso-token"])
        self.assertEqual(summary["platforms"]["grok"]["sessions"], 1)

    def test_reset_cursor_and_invalid_format(self):
        (self.root / "emails.txt").write_text("a@example.com----pw\n", encoding="utf-8")
        asset_store.get_email()
        reset = asset_store.reset_cursor("email")
        self.assertEqual(reset["removed"], ["email"])
        self.assertEqual(asset_store.get_email()["index"], 0)
        with self.assertRaises(asset_store.AssetError):
            asset_store.get_platform_asset("claude", "cpa", index=0)

    def test_four_field_email_batch_is_archived_after_export(self):
        (self.root / "emails.txt").write_text(
            "first@example.com----pw1----rt1----cid1\n"
            "second@example.com----pw2\n",
            encoding="utf-8",
        )

        results = asset_store.export_batch("emails", output_format="four", limit=2)
        lifecycle = asset_store.archive_asset_results(
            results, bucket="exported", reason="test_export"
        )

        self.assertEqual(
            [item["data"] for item in results],
            [
                "first@example.com----pw1----rt1----cid1",
                "second@example.com----pw2--------",
            ],
        )
        self.assertEqual(lifecycle["moved_accounts"], 2)
        self.assertEqual((self.root / "emails.txt").read_text(encoding="utf-8"), "")
        archived = list((self.root / "runtime" / "assets" / "exported").rglob("emails.txt"))
        self.assertEqual(len(archived), 2)
        self.assertEqual(asset_store.summary()["lifecycle"]["exported"]["accounts"], 2)

    def test_definitive_bad_platform_asset_moves_to_quarantine(self):
        self._write_chatgpt_assets()
        report = {
            "items": [{
                "platform": "chatgpt",
                "email": "user@example.com",
                "source": "full_profile_20260101_000000.json,user@example.com.session.json",
                "status": "banned",
            }],
        }

        lifecycle = asset_store.quarantine_scan_report(report)

        self.assertEqual(lifecycle["moved_accounts"], 1)
        self.assertEqual(lifecycle["moved_files"], 2)
        self.assertEqual(asset_store.summary()["platforms"]["chatgpt"], {"cookies": 0, "sessions": 0})
        quarantined = list((self.root / "runtime" / "assets" / "quarantine").rglob("*.json"))
        self.assertEqual(len(quarantined), 2)

    def test_chatgpt_can_export_registration_mailbox_by_provider(self):
        (self.root / "emails.txt").write_text(
            "outlook@outlook.com----pw1----rt1----cid1\n"
            "icloud@icloud.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )
        token_dir = self.root / "tokens" / "chatgpt"
        token_dir.mkdir(parents=True)
        for email in ("outlook@outlook.com", "icloud@icloud.com"):
            (token_dir / f"{email}.session.json").write_text(json.dumps({
                "user": {"email": email},
                "accessToken": f"token-{email}",
            }), encoding="utf-8")

        outlook = asset_store.get_platform_asset(
            "chatgpt", "email_four", index=0, email_provider="outlook"
        )
        icloud = asset_store.get_platform_asset(
            "chatgpt", "email_four", index=0, email_provider="icloud"
        )

        self.assertEqual(outlook["data"], "outlook@outlook.com----pw1----rt1----cid1")
        self.assertEqual(outlook["email_provider"], "outlook")
        self.assertEqual(icloud["data"], "icloud@icloud.com----pw2----rt2----cid2")
        self.assertEqual(icloud["email_provider"], "icloud")

    def test_chatgpt_icloud_export_uses_registration_account_ledger(self):
        token_dir = self.root / "tokens" / "chatgpt"
        token_dir.mkdir(parents=True)
        (token_dir / "icloud@icloud.com.session.json").write_text(json.dumps({
            "user": {"email": "icloud@icloud.com"},
            "accessToken": "access-token",
        }), encoding="utf-8")
        cookie_dir = self.root / "cookies" / "chatgpt"
        cookie_dir.mkdir(parents=True)
        (cookie_dir / "accounts.txt").write_text(
            "icloud@icloud.com|chatgpt-password|session-secret\n",
            encoding="utf-8",
        )

        result = asset_store.get_platform_asset(
            "chatgpt", "email_four", index=0, email_provider="icloud"
        )

        self.assertEqual(
            result["data"],
            "icloud@icloud.com----chatgpt-password--------",
        )
        self.assertNotIn("session-secret", result["data"])

    def test_chatgpt_icloud_export_allows_dynamic_mailbox_without_password(self):
        token_dir = self.root / "tokens" / "chatgpt"
        token_dir.mkdir(parents=True)
        (token_dir / "icloud@icloud.com.session.json").write_text(json.dumps({
            "email": "icloud@icloud.com",
            "accessToken": "access-token",
        }), encoding="utf-8")

        result = asset_store.get_platform_asset(
            "chatgpt", "email_four", index=0, email_provider="icloud"
        )

        self.assertEqual(result["data"], "icloud@icloud.com------------")

    def test_consuming_batch_can_include_previously_claimed_active_account(self):
        self._write_chatgpt_assets()
        claimed = asset_store.get_platform_asset("chatgpt", "session", claim_once=True)

        results = asset_store.export_batch(
            "chatgpt", "session", limit=10, include_claimed=True
        )

        self.assertEqual([item["email"] for item in results], [claimed["email"]])

    def test_verified_batch_can_include_previously_claimed_normal_account(self):
        self._write_chatgpt_assets()
        from common import asset_scanner

        report = {"items": [{
            "platform": "chatgpt",
            "email": "user@example.com",
            "source": "user@example.com.session.json",
            "status": "normal",
            "checked_at": "2026-08-16T00:00:00Z",
        }]}
        with patch.object(asset_scanner, "get_report", return_value=report):
            claimed = asset_store.get_platform_asset(
                "chatgpt", "session", verified_only=True
            )
            with self.assertRaises(asset_store.AssetExhausted):
                asset_store.export_batch(
                    "chatgpt", "session", verified_only=True, include_claimed=False
                )
            results = asset_store.export_batch(
                "chatgpt", "session", verified_only=True, include_claimed=True
            )

        self.assertEqual([item["email"] for item in results], [claimed["email"]])

    def test_batch_export_only_selects_cached_normal_accounts(self):
        self._write_chatgpt_assets()
        token_dir = self.root / "tokens" / "chatgpt"
        (token_dir / "bad@example.com.session.json").write_text(json.dumps({
            "user": {"email": "bad@example.com"},
            "accessToken": "bad-token",
        }), encoding="utf-8")
        from common import asset_scanner

        report = {"items": [
            {
                "platform": "chatgpt",
                "email": "user@example.com",
                "source": "user@example.com.session.json",
                "status": "normal",
                "checked_at": "2026-08-16T00:00:00Z",
            },
            {
                "platform": "chatgpt",
                "email": "bad@example.com",
                "source": "bad@example.com.session.json",
                "status": "banned",
                "checked_at": "2026-08-16T00:00:00Z",
            },
        ]}
        with patch.object(asset_scanner, "get_report", return_value=report) as get_report:
            results = asset_store.export_batch(
                "chatgpt",
                "session",
                limit=10,
                verified_only=True,
                include_claimed=True,
            )

        self.assertEqual([item["email"] for item in results], ["user@example.com"])
        get_report.assert_called_once()


if __name__ == "__main__":
    unittest.main()
