import json
import tempfile
import unittest
from unittest.mock import patch

import register_claude_http as protocol


class _Response:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


class _Session:
    def __init__(self):
        self.headers = {}
        self.proxies = {}
        self.cookies = []
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response()

    def close(self):
        pass


class ClaudeProtocolTests(unittest.TestCase):
    def test_headers_are_stable_and_include_claudex_metadata(self):
        first = protocol.build_headers("same@example.com")
        second = protocol.build_headers("same@example.com")
        self.assertEqual(first, second)
        self.assertEqual(first["anthropic-client-platform"], "web_claude_ai")
        self.assertEqual(first["anthropic-client-version"], "1.0.0")
        self.assertRegex(first["anthropic-client-sha"], r"^[0-9a-f]{40}$")
        self.assertTrue(first["anthropic-anonymous-id"].startswith("claudeai.v1."))
        self.assertNotEqual(
            first["anthropic-device-id"], protocol.build_headers("other@example.com")["anthropic-device-id"]
        )

    def test_client_uses_first_party_login_payload(self):
        session = _Session()
        client = protocol.ClaudeProtocolClient("user@example.com", session=session)
        client.get_login_methods("user@example.com")
        client.send_magic_link("user@example.com")
        self.assertEqual(session.calls[0][0], "GET")
        self.assertEqual(session.calls[0][1], "https://claude.ai/api/auth/login_methods")
        self.assertEqual(session.calls[0][2]["params"], {"email": "user@example.com", "source": "claude"})
        payload = session.calls[1][2]["json"]
        self.assertEqual(payload["email_address"], "user@example.com")
        self.assertEqual(payload["source"], "claude")
        self.assertEqual(payload["locale"], "en-US")
        client.close()

    def test_protocol_version_can_be_overridden_per_client(self):
        session = _Session()
        client = protocol.ClaudeProtocolClient(
            "user@example.com", session=session, protocol_version="2.1.0"
        )
        self.assertEqual(session.headers["anthropic-client-version"], "2.1.0")
        client.close()

    def test_magic_link_extractor_ignores_unrelated_text(self):
        self.assertEqual(
            protocol.extract_magic_link(
                "verify: https://claude.ai/magic-link#nonce:ZW1haWw="
            ),
            "https://claude.ai/magic-link#nonce:ZW1haWw=",
        )
        self.assertIsNone(protocol.extract_magic_link("no link"))

    def test_magic_link_extractor_handles_html_and_encoded_fragment(self):
        self.assertEqual(
            protocol.extract_magic_link(
                '<a href="https://claude.ai/magic-link#abc%3AZW1haWw%3D">verify</a>'
            ),
            "https://claude.ai/magic-link#abc%3AZW1haWw%3D",
        )

    def test_account_loader_accepts_json_records(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump([{"email": "user@example.com", "password": "pw", "refresh_token": "rt", "client_id": "cid"}], handle)
            path = handle.name
        self.assertEqual(
            protocol._load_accounts(path),
            [("user@example.com", "pw", "rt", "cid")],
        )


if __name__ == "__main__":
    unittest.main()
