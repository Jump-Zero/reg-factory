import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from common import mailbox


class MailboxTests(unittest.TestCase):
    def test_parse_outlook_recovery_mailbox_record(self):
        result = mailbox.parse_outlook_recovery_mailbox(
            "helper@outlook.com----password----refresh-token----client-id"
        )

        self.assertEqual(result["email"], "helper@outlook.com")
        self.assertEqual(result["password"], "password")
        self.assertEqual(result["refresh_token"], "refresh-token")
        self.assertEqual(result["client_id"], "client-id")
        self.assertEqual(result["provider"], "outlook")

    def test_create_outlook_recovery_mailbox_validates_graph_api(self):
        with patch.object(
            mailbox,
            "check_refresh_token",
            return_value={"ok": True, "access_token": "access"},
        ) as validator:
            result = mailbox.create_graph_recovery_mailbox(
                "outlook",
                "helper@outlook.com----password----refresh-token----client-id",
            )

        validator.assert_called_once_with("refresh-token", "client-id")
        self.assertEqual(result["email"], "helper@outlook.com")
        self.assertEqual(result["refresh_token"], "refresh-token")

    def test_create_outlook_recovery_mailbox_rejects_invalid_graph_api(self):
        with patch.object(
            mailbox,
            "check_refresh_token",
            return_value={"ok": False, "reason": "invalid_grant"},
        ):
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                mailbox.create_graph_recovery_mailbox(
                    "outlook",
                    "helper@outlook.com----password----refresh-token----client-id",
                )

    def test_poll_outlook_recovery_code_uses_microsoft_graph_filters(self):
        with patch.object(
            mailbox,
            "get_code_by_token",
            return_value="123456",
        ) as reader:
            result = mailbox.poll_graph_recovery_code(
                {
                    "provider": "outlook",
                    "email": "helper@outlook.com",
                    "refresh_token": "refresh-token",
                    "client_id": "client-id",
                },
                max_wait=30,
                poll_interval=2,
                received_after=123.0,
            )

        self.assertEqual(result, "123456")
        self.assertEqual(reader.call_args.args[:2], ("helper@outlook.com", "refresh-token"))
        self.assertEqual(reader.call_args.kwargs["client_id"], "client-id")
        self.assertEqual(reader.call_args.kwargs["max_wait"], 30)
        self.assertEqual(reader.call_args.kwargs["received_after"], 123.0)
        self.assertIn("microsoft.com", reader.call_args.kwargs["sender_contains"])

    def test_refresh_token_classifies_service_abuse_without_echoing_response(self):
        response = MagicMock(status_code=400)
        response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "User account is found to be in service abuse mode.",
        }
        session = MagicMock()
        session.post.return_value = response
        with patch.object(mailbox, "_ms_session", return_value=session):
            result = mailbox.check_refresh_token("secret-rt", "client")
        self.assertFalse(result["ok"])
        self.assertTrue(result["permanent"])
        self.assertEqual(result["reason"], "service_abuse")

    def test_link_reader_skips_old_fractional_graph_timestamp(self):
        messages = [
            {
                "subject": "Claude magic link",
                "from": "login@anthropic.com",
                "body": "https://claude.ai/magic-link#old",
                "received": "2026-07-17T10:00:00.1234567Z",
            },
            {
                "subject": "Claude magic link",
                "from": "login@anthropic.com",
                "body": "https://claude.ai/magic-link#fresh",
                "received": "2026-07-17T11:00:00.7654321Z",
            },
        ]
        cutoff = datetime(2026, 7, 17, 10, 30, tzinfo=timezone.utc).timestamp()
        with patch.object(mailbox, "_get_access_token", return_value="access"):
            with patch.object(mailbox, "fetch_messages", return_value=messages):
                link = mailbox.get_link_by_token(
                    "user@example.com",
                    "refresh-token",
                    link_regex=r"https://claude\.ai/magic-link#[a-z]+",
                    sender_contains=("anthropic",),
                    subject_contains=(),
                    must_contain="claude.ai/magic-link",
                    max_wait=1,
                    received_after=cutoff,
                )
        self.assertEqual(link, "https://claude.ai/magic-link#fresh")


if __name__ == "__main__":
    unittest.main()
