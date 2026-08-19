import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from common import node_quarantine


class NodeQuarantineTests(unittest.TestCase):
    def test_taint_is_persistent_and_filters_until_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": temp}, clear=False):
                start = time.time()
                record = node_quarantine.taint(
                    "node-a", "cloudflare_turnstile", ttl=120, now=start
                )
                self.assertEqual(record["node"], "node-a")
                self.assertEqual(
                    node_quarantine.filter_nodes(["node-a", "node-b"], now=start + 50),
                    ["node-b"],
                )
                self.assertTrue(node_quarantine.is_tainted("node-a", now=start + 119))
                payload = json.loads(node_quarantine.state_path().read_text(encoding="utf-8"))
                self.assertIn("node-a", payload["nodes"])
                self.assertFalse(node_quarantine.is_tainted("node-a", now=start + 120))

    def test_retaint_increments_count_and_clear_removes_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": temp}, clear=False):
                start = time.time()
                node_quarantine.taint("node-a", "risk", ttl=600, now=start)
                record = node_quarantine.taint("node-a", "risk-again", ttl=600, now=start + 100)
                self.assertEqual(record["count"], 2)
                self.assertEqual(node_quarantine.clear("node-a"), 1)
                self.assertFalse(node_quarantine.is_tainted("node-a", now=start + 101))


if __name__ == "__main__":
    unittest.main()
