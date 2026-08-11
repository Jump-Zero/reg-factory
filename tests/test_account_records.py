import unittest

from common.account_records import (
    DEFAULT_GRAPH_CLIENT_ID,
    canonical_account_line,
    masked_email,
    parse_account_line,
    parse_account_text,
)
from tools.import_plus_codex import _session_cookies, require_phone_verification


class AccountRecordTests(unittest.TestCase):
    def test_plus_import_requires_phone_verification(self):
        self.assertEqual(
            require_phone_verification({"codex_phone_status": "verified"}), "verified"
        )
        with self.assertRaisesRegex(RuntimeError, "手机号接码验证"):
            require_phone_verification({"codex_phone_status": "not_verified"})

    def test_parses_refresh_token_before_client_id(self):
        record = parse_account_line(
            "user@outlook.com----secret----M.C502.token----"
            "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
        )
        self.assertEqual(record["refresh_token"], "M.C502.token")
        self.assertEqual(record["client_id"], "9e5f94bc-e8a4-4e73-b8be-63364c29d753")

    def test_parses_client_id_before_refresh_token(self):
        record = parse_account_line(
            "user@outlook.jp|secret|9e5f94bc-e8a4-4e73-b8be-63364c29d753|M.C502.token"
        )
        self.assertEqual(record["refresh_token"], "M.C502.token")
        self.assertEqual(record["client_id"], "9e5f94bc-e8a4-4e73-b8be-63364c29d753")

    def test_parses_json_and_canonicalizes(self):
        record = parse_account_line(
            '{"email":"user@live.com","password":"secret",'
            '"client_id":"9e5f94bc-e8a4-4e73-b8be-63364c29d753",'
            '"refresh_token":"M.C502.token"}'
        )
        self.assertEqual(
            canonical_account_line(record),
            "user@live.com----secret----M.C502.token----"
            "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        )

    def test_batch_rejects_duplicates_without_leaking_values(self):
        records, errors = parse_account_text(
            "first@hotmail.com----one\n"
            "first@hotmail.com----two\n"
            "bad-line"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 2)
        self.assertNotIn("two", str(errors))

    def test_masks_email(self):
        self.assertEqual(masked_email("someone@outlook.com"), "so***@outlook.com")

    def test_parses_outlook_variants_and_defaults_client_id(self):
        for value in (
            "user@hotmail.co.uk:secret",
            "user@live.jp----secret----M.C502.token",
            "user@msn.com|secret|M.C502.token",
            "user@outlook.de----M.C502.token----9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        ):
            record = parse_account_line(value)
            self.assertEqual(record["provider"], "outlook")
        self.assertEqual(
            parse_account_line("user@live.jp----secret----M.C502.token")["client_id"],
            DEFAULT_GRAPH_CLIENT_ID,
        )

    def test_parses_icloud_mailbox_without_password(self):
        record = parse_account_line("plus.user@icloud.com")
        self.assertEqual(record["source_type"], "mailbox")
        self.assertEqual(record["provider"], "icloud")
        self.assertFalse(record["password"])

    def test_parses_session_cookie_and_cookie_json(self):
        raw = "__Secure-next-auth.session-token=opaque.session.value"
        record = parse_account_line(raw)
        self.assertEqual(record["source_type"], "session_token")
        self.assertEqual(record["session_token"], "opaque.session.value")

        records, errors = parse_account_text(
            '[{"name":"__Secure-next-auth.session-token","value":"opaque","domain":".chatgpt.com"}]'
        )
        self.assertFalse(errors)
        self.assertEqual(records[0]["cookies"][0]["domain"], ".chatgpt.com")

    def test_preserves_chunked_session_cookie_header(self):
        record = parse_account_line(
            "__Secure-next-auth.session-token.0=first-part; "
            "__Secure-next-auth.session-token.1=second-part"
        )
        cookies = _session_cookies(record)
        self.assertEqual(
            [item["name"] for item in cookies],
            [
                "__Secure-next-auth.session-token.0",
                "__Secure-next-auth.session-token.1",
            ],
        )
        self.assertNotIn("__Secure-next-auth.session-token", [item["name"] for item in cookies])

    def test_parses_oauth_json_and_requires_phone_marker_at_import(self):
        record = parse_account_line(
            '{"email":"plus@example.com","access_token":"access",'
            '"refresh_token":"refresh","plan_type":"plus",'
            '"codex_phone_status":"verified"}'
        )
        self.assertEqual(record["source_type"], "oauth_token")
        self.assertEqual(record["oauth_credentials"]["refresh_token"], "refresh")

    def test_raw_jwt_is_classified_as_access_token(self):
        record = parse_account_line("eyJhbGciOiJub25lIn0.eyJlbWFpbCI6InVAZS5jb20ifQ.signature")
        self.assertEqual(record["source_type"], "access_token")

    def test_whole_json_validation_returns_sanitized_error(self):
        records, errors = parse_account_text('{"email":"not-an-email","password":"secret"}')
        self.assertFalse(records)
        self.assertEqual(errors, [{"line": 1, "error": "invalid account email"}])
        self.assertNotIn("secret", str(errors))


if __name__ == "__main__":
    unittest.main()
