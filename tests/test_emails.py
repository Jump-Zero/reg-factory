import os
import tempfile
import unittest
from unittest.mock import patch

from common import emails


class EmailPoolTests(unittest.TestCase):
    def test_platform_reservation_permanently_excludes_outlook_sale(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            registration = os.path.join(tmp, "outlook_registration_emails.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("used@outlook.com----pw----rt----cid\n")
            with patch.object(emails, "EMAILS_FILE", pool), \
                    patch.object(emails, "_used_file", return_value=os.path.join(tmp, "used.txt")), \
                    patch.object(emails, "_error_file", return_value=os.path.join(tmp, "errors.txt")), \
                    patch.object(emails, "_outlook_sale_file", return_value=os.path.join(tmp, "sold.txt")), \
                    patch.object(emails, "_outlook_registration_file", return_value=registration):
                selected = emails.next_email("claude")

            self.assertEqual(selected[0], "used@outlook.com")
            with open(registration, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "used@outlook.com")

    def test_platform_pool_reads_permanent_registration_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            registration = os.path.join(tmp, "outlook_registration_emails.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("used@outlook.com----pw----rt----cid\n")
                f.write("clean@outlook.com----pw2----rt2----cid2\n")
            with open(registration, "w", encoding="utf-8") as f:
                f.write("used@outlook.com\n")
            with patch.object(emails, "EMAILS_FILE", pool), \
                    patch.object(emails, "_used_file", return_value=os.path.join(tmp, "used.txt")), \
                    patch.object(emails, "_error_file", return_value=os.path.join(tmp, "errors.txt")), \
                    patch.object(emails, "_outlook_sale_file", return_value=os.path.join(tmp, "sold.txt")), \
                    patch.object(emails, "_outlook_registration_file", return_value=registration):
                selected = emails.next_email("chatgpt")

            self.assertEqual(selected[0], "clean@outlook.com")
    def test_platform_pool_allows_outlook_mailboxes_already_claimed_for_sale(self):
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
                            with patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")):
                                selected = emails.next_email("chatgpt")

            self.assertEqual(selected[0], "sold@outlook.com")

    def test_retryable_chatgpt_mailbox_allows_outlook_mailbox_claimed_for_sale(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            used = os.path.join(tmp, "used.txt")
            errors = os.path.join(tmp, "errors.txt")
            sold = os.path.join(tmp, "outlook_sale_emails.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("sold@outlook.com----pw----rt----client\n")
            with open(errors, "w", encoding="utf-8") as f:
                f.write("sold@outlook.com----pw----email_submit_stuck\n")
            with open(sold, "w", encoding="utf-8") as f:
                f.write("sold@outlook.com\n")
            with (
                patch.object(emails, "EMAILS_FILE", pool),
                patch.object(emails, "_used_file", return_value=used),
                patch.object(emails, "_error_file", return_value=errors),
                patch.object(emails, "_outlook_sale_file", return_value=sold),
                patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")),
            ):
                selected = emails.retryable_email("chatgpt")

            self.assertEqual(selected, ("sold@outlook.com", "pw", "rt", "client"))

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
                        with patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")):
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
                        with patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")):
                            with patch(
                                "common.mailbox.check_mailbox_access",
                                side_effect=lambda _email, token, _client: {
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
                        with patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")):
                            with patch(
                                "common.mailbox.check_mailbox_access",
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
                        with patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")):
                            with patch(
                                "common.mailbox.check_mailbox_access",
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

    def test_retryable_chatgpt_mailbox_ignores_cross_platform_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            used = os.path.join(tmp, "used.txt")
            errors = os.path.join(tmp, "errors.txt")
            registration = os.path.join(tmp, "registration.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("retry@example.com----pw----rt----client\n")
            with open(used, "w", encoding="utf-8") as f:
                f.write("retry@example.com----pw----reserved\n")
            with open(errors, "w", encoding="utf-8") as f:
                f.write("retry@example.com----pw----email_verification_not_completed\n")
            with open(registration, "w", encoding="utf-8") as f:
                f.write("retry@example.com\n")
            with (
                patch.object(emails, "EMAILS_FILE", pool),
                patch.object(emails, "_used_file", return_value=used),
                patch.object(emails, "_error_file", return_value=errors),
                patch.object(emails, "_outlook_sale_file", return_value=os.path.join(tmp, "sold.txt")),
                patch.object(emails, "_outlook_registration_file", return_value=registration),
                patch(
                    "common.mailbox.check_mailbox_access",
                    return_value={"ok": True, "permanent": False},
                ),
            ):
                selected = emails.retryable_email(
                    "chatgpt", require_token=True, validate_token=True
                )

        self.assertEqual(selected, ("retry@example.com", "pw", "rt", "client"))

    def test_retryable_chatgpt_mailbox_honors_latest_terminal_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            errors = os.path.join(tmp, "errors.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("existing@example.com----pw----rt----client\n")
            with open(errors, "w", encoding="utf-8") as f:
                f.write("existing@example.com----pw----email_submit_stuck\n")
                f.write("existing@example.com----pw----mfa_required\n")
            with (
                patch.object(emails, "EMAILS_FILE", pool),
                patch.object(emails, "_used_file", return_value=os.path.join(tmp, "used.txt")),
                patch.object(emails, "_error_file", return_value=errors),
                patch.object(emails, "_outlook_sale_file", return_value=os.path.join(tmp, "sold.txt")),
            ):
                selected = emails.retryable_email("chatgpt")

        self.assertIsNone(selected)

    def test_retryable_chatgpt_mailbox_is_claimed_once_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = os.path.join(tmp, "emails.txt")
            used = os.path.join(tmp, "used.txt")
            errors = os.path.join(tmp, "errors.txt")
            with open(pool, "w", encoding="utf-8") as f:
                f.write("retry@example.com----pw----rt----client\n")
            with open(errors, "w", encoding="utf-8") as f:
                f.write("retry@example.com----pw----email_submit_stuck\n")
            with (
                patch.object(emails, "EMAILS_FILE", pool),
                patch.object(emails, "_used_file", return_value=used),
                patch.object(emails, "_error_file", return_value=errors),
                patch.object(emails, "_outlook_sale_file", return_value=os.path.join(tmp, "sold.txt")),
                patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")),
                patch.dict(os.environ, {"REG_FACTORY_RUN_ID": "batch-one"}, clear=False),
            ):
                first = emails.retryable_email("chatgpt")
                duplicate = emails.retryable_email("chatgpt")

            with (
                patch.object(emails, "EMAILS_FILE", pool),
                patch.object(emails, "_used_file", return_value=used),
                patch.object(emails, "_error_file", return_value=errors),
                patch.object(emails, "_outlook_sale_file", return_value=os.path.join(tmp, "sold.txt")),
                patch.object(emails, "_outlook_registration_file", return_value=os.path.join(tmp, "registration.txt")),
                patch.dict(os.environ, {"REG_FACTORY_RUN_ID": "batch-two"}, clear=False),
            ):
                next_batch = emails.retryable_email("chatgpt")

        expected = ("retry@example.com", "pw", "rt", "client")
        self.assertEqual(first, expected)
        self.assertIsNone(duplicate)
        self.assertEqual(next_batch, expected)


if __name__ == "__main__":
    unittest.main()
