import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import asset_store, asset_scanner


class AssetStatusFilterTests(unittest.TestCase):
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
        (self.root / "emails.txt").write_text(
            "normal@outlook.com----pw1----rt1----cid1\n"
            "error@outlook.com----pw2----rt2----cid2\n",
            encoding="utf-8",
        )

    def test_email_claim_can_select_explicit_scan_statuses(self):
        report = {
            "items": [
                {"platform": "outlook", "email": "normal@outlook.com", "source": "emails.txt:1", "status": "normal"},
                {"platform": "outlook", "email": "error@outlook.com", "source": "emails.txt:2", "status": "error"},
            ]
        }
        with patch.object(asset_scanner, "get_report", return_value=report):
            normal = asset_store.get_email(status="normal")
            self.assertEqual(normal["email"], "normal@outlook.com")
            self.assertEqual(normal["verification"]["status"], "normal")

            error = asset_store.get_email(status="error")
            self.assertEqual(error["email"], "error@outlook.com")
            self.assertEqual(error["verification"]["status"], "error")

    def test_status_filter_accepts_multiple_values_and_rejects_unknown(self):
        with self.assertRaises(asset_store.AssetError):
            asset_store.get_email(status="not-a-status")

        self.assertEqual(
            asset_store._normalize_status_filter("normal,error"),
            ("normal", "error"),
        )


if __name__ == "__main__":
    unittest.main()
