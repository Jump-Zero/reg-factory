import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import custom_sms


class CustomSmsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pool = str(Path(self.temp.name) / "custom-sms.json")
        self.env = patch.dict("os.environ", {"CUSTOM_SMS_POOL_FILE": self.pool}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_import_claim_release_and_use(self):
        result = custom_sms.import_text(
            "+13435775857----https://example.test/sms?token=secret\n"
            "+17054798747----https://example.test/sms?token=other\n"
        )
        self.assertEqual(result["added"], 2)
        first = custom_sms.claim()
        second = custom_sms.claim()
        self.assertNotEqual(first[0], second[0])
        self.assertIsNone(custom_sms.claim())
        self.assertTrue(custom_sms.release(first[2]))
        claimed_again = custom_sms.claim()
        self.assertEqual(claimed_again[0], first[0])
        self.assertTrue(custom_sms._mark_used(claimed_again[2]))
        self.assertEqual(custom_sms.summary()["used"], 1)

    def test_duplicate_import_does_not_reset_used_number(self):
        line = "+13435775857----https://example.test/sms?token=secret"
        custom_sms.import_text(line)
        rental = custom_sms.claim()
        custom_sms._mark_used(rental[2])
        result = custom_sms.import_text(line)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["used"], 1)

    def test_extract_code_ignores_waiting_page_expiry_date(self):
        self.assertIsNone(
            custom_sms.extract_code(
                "no|This number expires on 2026-09-21 00:00:00. No messages yet."
            )
        )
        self.assertEqual(
            custom_sms.extract_code("yes|Your OpenAI verification code is 606325"),
            "606325",
        )

    def test_summary_redacts_record_url_token(self):
        custom_sms.import_text(
            "+13435775857----https://example.test/sms?token=secret&account=one"
        )
        record = custom_sms.summary()["records"][0]
        self.assertNotIn("secret", record["record_url"])
        self.assertIn("token=%2A%2A%2A", record["record_url"])

    def test_explicit_host_allowlist_can_accept_non_public_dns(self):
        with patch.dict(
            "os.environ",
            {"CUSTOM_SMS_ALLOWED_HOSTS": "xsd20vip.com"},
            clear=False,
        ), patch.object(
            custom_sms.socket,
            "getaddrinfo",
            return_value=[(None, None, None, None, ("198.18.1.130", 0))],
        ):
            custom_sms._require_public_url(
                "https://xsd20vip.com/smsrecord?token=secret"
            )

    def test_unlisted_non_public_dns_is_still_rejected(self):
        with patch.dict("os.environ", {"CUSTOM_SMS_ALLOWED_HOSTS": ""}, clear=False), patch.object(
            custom_sms.socket,
            "getaddrinfo",
            return_value=[(None, None, None, None, ("198.18.1.130", 0))],
        ):
            with self.assertRaisesRegex(ValueError, "public addresses"):
                custom_sms._require_public_url(
                    "https://xsd20vip.com/smsrecord?token=secret"
                )


if __name__ == "__main__":
    unittest.main()
