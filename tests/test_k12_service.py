import unittest
from unittest.mock import patch

from k12 import server


class K12ServiceTests(unittest.TestCase):
    def test_normalize_records_uses_project_account_parser(self):
        records, errors = server._normalize_records(
            "user@example.com----password----client-id----refresh-token"
        )
        self.assertFalse(errors)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "user@example.com")
        self.assertEqual(records[0]["password"], "password")

    def test_public_views_do_not_expose_credentials(self):
        record = {
            "id": "id-1",
            "email": "user@example.com",
            "password": "secret",
            "refresh_token": "refresh",
            "client_id": "client",
            "raw": "raw",
            "status": "free",
        }
        task = {"id": "task-1", "email": record["email"], "access_token": "at", "status": "success"}
        self.assertNotIn("password", server._public_email(record))
        self.assertNotIn("refresh_token", server._public_task(task))
        self.assertNotIn("access_token", server._public_task(task))

    @patch.object(server, "_write_json")
    def test_default_paths_are_independent_from_main_runtime(self, write_json):
        self.assertIn("k12", str(server.DATA_ROOT).lower())
        self.assertNotEqual(server.EMAILS_PATH, server.ROOT / "emails.txt")
        write_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
