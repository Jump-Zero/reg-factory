import argparse
import unittest

import register_three_platforms


class GrokSub2ApiFlowTests(unittest.TestCase):
    def test_three_platform_command_forwards_sub2api_options(self):
        args = argparse.Namespace(
            timeout=600,
            node="auto",
            grok_sub2api=True,
            grok_sub2api_group="grok-prod",
        )

        command = register_three_platforms.build_command(
            "grok",
            args,
            ("mail@example.com", "password", "token", "client-id"),
        )

        self.assertIn("--sub2api", command)
        self.assertIn("register_grok.py", command)
        self.assertNotIn("register_grok_http.py", command)
        self.assertIn("mail@example.com", command)
        self.assertEqual(
            command[command.index("--sub2api-group") + 1],
            "grok-prod",
        )
        self.assertEqual(command[command.index("--mailbox-attempts") + 1], "6")


if __name__ == "__main__":
    unittest.main()
