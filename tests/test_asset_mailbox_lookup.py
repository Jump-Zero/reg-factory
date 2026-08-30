import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import asset_store


class MailboxLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _write_emails(self, content: str) -> None:
        (self.root / "emails.txt").write_text(content, encoding="utf-8")

    def _write_no_graph(self, content: str) -> None:
        (self.root / "outlook_no_graph.txt").write_text(content, encoding="utf-8")

    def test_exact_match_with_password(self):
        self._write_emails("user1@outlook.com----pw1----rt1----cid1\n")
        record = asset_store.find_mailbox_credentials("user1@outlook.com")
        self.assertIsNotNone(record)
        self.assertEqual(record["email"], "user1@outlook.com")
        self.assertEqual(record["password"], "pw1")
        self.assertEqual(record["refresh_token"], "rt1")
        self.assertEqual(record["client_id"], "cid1")

    def test_alias_falls_back_to_root_email(self):
        self._write_emails("FountJoynt8945@Outlook.com----pw9\n")
        record = asset_store.find_mailbox_credentials("FountJoynt8945+qg4iok@outlook.com")
        self.assertIsNotNone(record)
        self.assertEqual(record["email"].lower(), "fountjoynt8945@outlook.com")
        self.assertEqual(record["password"], "pw9")

    def test_alias_exact_entry_wins_over_root(self):
        self._write_emails(
            "root@outlook.com----pwroot\n"
            "root+tag@outlook.com----pwtag\n"
        )
        record = asset_store.find_mailbox_credentials("root+tag@outlook.com")
        self.assertIsNotNone(record)
        self.assertEqual(record["email"].lower(), "root+tag@outlook.com")
        self.assertEqual(record["password"], "pwtag")

    def test_graph_only_record_without_password(self):
        self._write_emails("graph@outlook.com--------rtg----cidg\n")
        record = asset_store.find_mailbox_credentials("graph+xx@outlook.com")
        self.assertIsNotNone(record)
        self.assertEqual(record["password"], "")
        self.assertEqual(record["refresh_token"], "rtg")
        self.assertEqual(record["client_id"], "cidg")

    def test_no_graph_file_fallback(self):
        self._write_no_graph("plain@outlook.com----pwplain\n")
        record = asset_store.find_mailbox_credentials("plain+q1@outlook.com")
        self.assertIsNotNone(record)
        self.assertEqual(record["password"], "pwplain")
        self.assertEqual(record["refresh_token"], "")

    def test_record_without_usable_credentials_returns_none(self):
        self._write_emails("empty@outlook.com\n")
        self.assertIsNone(asset_store.find_mailbox_credentials("empty@outlook.com"))

    def test_unknown_email_returns_none(self):
        self._write_emails("known@outlook.com----pw\n")
        self.assertIsNone(asset_store.find_mailbox_credentials("other@outlook.com"))

    def test_invalid_input_returns_none(self):
        self._write_emails("known@outlook.com----pw\n")
        self.assertIsNone(asset_store.find_mailbox_credentials(""))
        self.assertIsNone(asset_store.find_mailbox_credentials(None))
        self.assertIsNone(asset_store.find_mailbox_credentials("not-an-email"))


if __name__ == "__main__":
    unittest.main()
