import unittest
import copy
from unittest.mock import patch

from common import temp_email


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.content = b"x" if data is not None or text else b""

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, copy.deepcopy(kwargs)))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, copy.deepcopy(kwargs)))
        return self.responses.pop(0)


class YydsMailTests(unittest.TestCase):
    def test_normalizes_marketing_and_pasted_endpoint_urls(self):
        self.assertEqual(
            temp_email._norm_yyds_base("vip.215.im/v1/accounts"),
            "https://maliapi.215.im",
        )
        self.assertEqual(
            temp_email._norm_yyds_base("https://maliapi.215.im/v1"),
            "https://maliapi.215.im",
        )

    def test_create_uses_normalized_api_root(self):
        sess = FakeSession([
            FakeResponse(data={"data": {"id": "box-1", "address": "a@example.com", "token": "mail-token"}}),
        ])

        mailbox = temp_email._yyds_create(
            None, "example.com", None, "AC-test", "https://vip.215.im/v1/accounts", sess,
        )

        self.assertEqual(mailbox["id"], "box-1")
        self.assertEqual(sess.calls[0][1], "https://maliapi.215.im/v1/accounts")

    def test_domain_picker_uses_current_health_fields_and_prefers_exact_mx(self):
        sess = FakeSession([
            FakeResponse(data={"data": {"domains": [
                {
                    "domain": "unhealthy.example",
                    "isPublic": True,
                    "isVerified": True,
                    "isMxValid": False,
                    "dnsRecords": {"receivingReady": False},
                },
                {
                    "domain": "wildcard.example",
                    "isPublic": True,
                    "isVerified": True,
                    "isMxValid": True,
                    "dnsRecords": {
                        "receivingReady": True,
                        "wildcardMxValid": True,
                    },
                },
                {
                    "domain": "exact.example",
                    "isPublic": True,
                    "isVerified": True,
                    "isMxValid": True,
                    "dnsRecords": {
                        "receivingReady": True,
                        "wildcardMxValid": False,
                    },
                },
            ]}}),
        ])

        domain = temp_email._yyds_pick_domain(
            "AC-test", "https://maliapi.215.im", sess
        )

        self.assertEqual(domain, "exact.example")

    def test_create_rotates_shared_domain_after_403(self):
        sess = FakeSession([
            FakeResponse(data={"domains": [
                {"domain": "blocked.example", "wildcardMxValid": True},
                {"domain": "usable.example", "wildcardMxValid": True},
            ]}),
            FakeResponse(status_code=403, text="This shared domain is currently restricted"),
            FakeResponse(data={"domains": [
                {"domain": "blocked.example", "wildcardMxValid": True},
                {"domain": "usable.example", "wildcardMxValid": True},
            ]}),
            FakeResponse(data={"data": {"id": "box-2", "address": "b@usable.example", "token": "mail-token"}}),
        ])

        with patch.object(temp_email.random, "choice", side_effect=lambda values: values[0]):
            mailbox = temp_email._yyds_create(None, None, None, "AC-test", None, sess)

        self.assertEqual(mailbox["email"], "b@usable.example")
        self.assertEqual(sess.calls[1][2]["json"]["domain"], "blocked.example")
        self.assertEqual(sess.calls[3][2]["json"]["domain"], "usable.example")

    def test_fetch_prefers_mailbox_token_and_public_messages_route(self):
        sess = FakeSession([
            FakeResponse(data={"data": {"messages": []}}),
        ])

        messages = temp_email._yyds_fetch(
            "box-1", "a@example.com", "mail-token", "AC-test", None, sess,
        )

        self.assertEqual(messages, [])
        _, url, kwargs = sess.calls[0]
        self.assertEqual(url, "https://maliapi.215.im/v1/messages")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer mail-token"})

    def test_fetch_falls_back_to_api_key_after_token_404(self):
        sess = FakeSession([
            FakeResponse(status_code=404, data={"error": "not found"}),
            FakeResponse(data={"data": {"messages": []}}),
        ])

        temp_email._yyds_fetch(
            "box-1", "a@example.com", "mail-token", "AC-test", None, sess,
        )

        self.assertEqual(len(sess.calls), 2)
        self.assertEqual(sess.calls[1][2]["headers"], {"X-API-Key": "AC-test"})

    def test_fetch_reports_404_after_all_routes_fail(self):
        sess = FakeSession([
            FakeResponse(status_code=404, data={"error": "not found"}, text="not found"),
            FakeResponse(status_code=404, data={"error": "not found"}, text="not found"),
            FakeResponse(status_code=404, data={"error": "not found"}, text="not found"),
        ])

        with self.assertRaisesRegex(RuntimeError, "YYDS fetch 404"):
            temp_email._yyds_fetch(
                "box-1", "a@example.com", "mail-token", "AC-test", None, sess,
            )


class ICloudMailTests(unittest.TestCase):
    def test_provider_config_redacts_key_from_full_endpoint(self):
        with patch.object(
            temp_email, "ICLOUD_MAIL_API_BASE",
            "https://mail.no-replyca.xyz/api/user/email?type=icloud&apikey=secret-key",
        ), patch.object(temp_email, "ICLOUD_MAIL_API_KEY", ""):
            base, ready, source = temp_email._provider_config("icloud")

        self.assertEqual(base, "https://mail.no-replyca.xyz")
        self.assertTrue(ready)
        self.assertNotIn("secret-key", base)
        self.assertIn("ICLOUD_MAIL_API_BASE", source)

    def test_create_accepts_full_icloud_submail_endpoint(self):
        sess = FakeSession([
            FakeResponse(data={"code": 0, "message": "success", "data": {
                "type": "icloud", "email": "alias@example.com"
            }}),
        ])
        with patch.object(temp_email, "ICLOUD_MAIL_TYPE", "icloud-code"):
            mailbox = temp_email._icloud_create(
                None, None, None, None,
                "https://mail.no-replyca.xyz/api/user/email?type=icloud&apikey=alias-key",
                sess,
            )

        self.assertEqual(mailbox["email"], "alias@example.com")
        self.assertEqual(sess.calls[0][1], "https://mail.no-replyca.xyz/api/user/email")
        self.assertEqual(
            sess.calls[0][2]["params"],
            {"type": "icloud", "apikey": "alias-key"},
        )

    def test_create_uses_icloud_code_service_query(self):
        sess = FakeSession([
            FakeResponse(data={"code": 0, "message": "success", "data": {
                "type": "icloud-code", "email": "icloud@example.com"
            }}),
        ])
        with patch.object(temp_email, "ICLOUD_MAIL_TYPE", "icloud-code"), patch.object(
            temp_email, "ICLOUD_MAIL_SERVICE", "openai"
        ):
            mailbox = temp_email._icloud_create(
                None, None, None, "test-key", "https://mail.no-replyca.xyz", sess
            )

        self.assertEqual(mailbox["email"], "icloud@example.com")
        self.assertEqual(sess.calls[0][1], "https://mail.no-replyca.xyz/api/user/email")
        self.assertEqual(
            sess.calls[0][2]["params"],
            {"type": "icloud-code", "service": "openai", "apikey": "test-key"},
        )

    def test_fetch_maps_provider_code_and_empty_success(self):
        sess = FakeSession([
            FakeResponse(data={"code": 0, "message": "success"}),
            FakeResponse(data={"code": 0, "message": "success", "data": {
                "from": "no-reply@openai.com", "subject": "Your code", "code": "123456"
            }}),
        ])

        self.assertEqual(
            temp_email._icloud_fetch(
                "icloud@example.com", "icloud@example.com", "", "test-key",
                "https://mail.no-replyca.xyz", sess
            ),
            [],
        )
        with patch.object(temp_email, "_session", return_value=sess):
            messages = temp_email.fetch_messages(
                "icloud@example.com", "icloud", email="icloud@example.com", api_key="test-key",
                base_url="https://mail.no-replyca.xyz"
            )

        self.assertEqual(messages[0]["extracted"]["codes"], ["123456"])
        self.assertEqual(sess.calls[1][2]["params"]["email"], "icloud@example.com")



if __name__ == "__main__":
    unittest.main()
