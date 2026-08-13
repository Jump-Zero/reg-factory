import os
import tempfile
import unittest
from unittest.mock import patch

from common import emails


class EmailPoolTests(unittest.TestCase):
    def test_platform_pool_skips_outlook_mailboxes_already_sold_standalone(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            sold = os.path.join(tmp, "outlook_sale_emails.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("sold@outlook.com----pw1----rt1----cid1\n")
                f.write("clean@outlook.com----pw2----rt2----cid2\n")
            with open(sold, "w", encoding="utf-8") as f:
                f.write("sold@outlook.com\n")
            with patch.object(emails, "EMAILS_FILE", pool):
                with patch.object(emails, "_used_file", return_value=os.path.join(tmp, "used.txt")):
                    with patch.object(emails, "_error_file", return_value=os.path.join(tmp, "errors.txt")):
                        with patch.object(emails, "_outlook_sale_file", return_value=sold):
                            selected = emails.next_email("chatgpt")

            self.assertEqual(selected[0], "clean@outlook.com")

    def test_latest_email_requires_token_and_reserves_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            used = os.path.join(tmp, "used.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("old@example.com----pw----old-rt----old-client\n")
                f.write("new-no-rt@example.com----pw\n")
                f.write("new@example.com----pw----new-rt----new-client\n")
            with patch.object(emails, "EMAILS_FILE", pool):
                with patch.object(emails, "_used_file", return_value=used):
                    with patch.object(emails, "_error_file", return_value=os.path.join(tmp, "errors.txt")):
                        selected = emails.latest_email("grok", require_token=True)
            self.assertEqual(selected[0], "new@example.com")
            with open(used, encoding="utf-8") as f:
                self.assertIn("new@example.com", f.read())

    def test_latest_email_skips_unusable_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            used = os.path.join(tmp, "used.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("working@example.com----pw----good-rt----client\n")
                f.write("blocked@example.com----pw----bad-rt----client\n")
            with patch.object(emails, "EMAILS_FILE", pool):
                with patch.object(emails, "_used_file", return_value=used):
                    with patch.object(emails, "_error_file", return_value=os.path.join(tmp, "errors.txt")):
                        with patch(
                            "common.mailbox.check_refresh_token",
                            side_effect=lambda token, _client: {
                                "ok": token == "good-rt",
                                "access_token": "access" if token == "good-rt" else "",
                                "permanent": token != "good-rt",
                                "reason": "invalid_grant" if token != "good-rt" else "",
                            },
                        ):
                            selected = emails.latest_email(
                                "grok", require_token=True, validate_token=True
                            )
            self.assertEqual(selected[0], "working@example.com")

    def test_latest_email_quarantines_permanently_invalid_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            errors = os.path.join(tmp, "errors.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("blocked@example.com----pw----bad-rt----client\n")
            with patch.object(emails, "EMAILS_FILE", pool):
                with patch.object(emails, "_used_file", return_value=os.path.join(tmp, "used.txt")):
                    with patch.object(emails, "_error_file", return_value=errors):
                        with patch(
                            "common.mailbox.check_refresh_token",
                            return_value={
                                "ok": False,
                                "access_token": "",
                                "permanent": True,
                                "reason": "service_abuse",
                            },
                        ):
                            selected = emails.latest_email(
                                "claude", require_token=True, validate_token=True
                            )
            self.assertIsNone(selected)
            with open(errors, encoding="utf-8") as f:
                self.assertEqual(
                    f.read().strip(),
                    "blocked@example.com----pw----service_abuse",
                )

    def test_latest_email_does_not_quarantine_transient_token_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            errors = os.path.join(tmp, "errors.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("retry@example.com----pw----rt----client\n")
            with patch.object(emails, "EMAILS_FILE", pool):
                with patch.object(emails, "_used_file", return_value=os.path.join(tmp, "used.txt")):
                    with patch.object(emails, "_error_file", return_value=errors):
                        with patch(
                            "common.mailbox.check_refresh_token",
                            return_value={
                                "ok": False,
                                "access_token": "",
                                "permanent": False,
                                "reason": "network_error",
                            },
                        ):
                            selected = emails.latest_email(
                                "claude", require_token=True, validate_token=True
                            )
            self.assertIsNone(selected)
            self.assertFalse(os.path.exists(errors))


if __name__ == "__main__":
    unittest.main()
