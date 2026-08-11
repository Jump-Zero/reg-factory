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


if __name__ == "__main__":
    unittest.main()
