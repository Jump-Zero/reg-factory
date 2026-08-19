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
            {"type": "icloud", "apikey": "alias-key", "share": "1"},
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
            {
                "type": "icloud-code",
                "service": "openai",
                "apikey": "test-key",
                "share": "1",
            },
        )
        self.assertEqual(mailbox["mail_type"], "icloud-code")
        self.assertEqual(mailbox["service"], "openai")

    def test_create_builds_keyless_share_url_from_share_token(self):
        sess = FakeSession([
            FakeResponse(data={"code": 0, "data": {
                "email": "icloud@example.com", "share_token": "opaque/token"
            }}),
        ])
        mailbox = temp_email._icloud_create(
            None, None, None, "test-key", "https://mail.no-replyca.xyz", sess,
            mail_type="icloud-code", service="openai",
        )

        expected = "https://mail.no-replyca.xyz/api/share/opaque%2Ftoken"
        self.assertEqual(mailbox["share_token"], "opaque/token")
        self.assertEqual(mailbox["share_url"], expected)
        self.assertEqual(mailbox["mail_api_url"], expected)
        self.assertEqual(sess.calls[0][2]["params"]["share"], "1")

    def test_create_explicit_chatgpt_purpose_overrides_generic_alias_config(self):
        sess = FakeSession([
            FakeResponse(data={"code": 0, "message": "success", "data": {
                "type": "icloud-code", "email": "openai-code@example.com"
            }}),
        ])
        with patch.object(temp_email, "ICLOUD_MAIL_TYPE", "icloud"), patch.object(
            temp_email, "_session", return_value=sess
        ):
            mailbox = temp_email.create_mailbox(
                provider="icloud",
                api_key="test-key",
                base_url="https://mail.no-replyca.xyz/api/user/email?type=icloud",
                mail_type="icloud-code",
                service="openai",
            )

        self.assertEqual(mailbox["email"], "openai-code@example.com")
        self.assertEqual(mailbox["mail_type"], "icloud-code")

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

    def test_fetch_accepts_per_account_direct_endpoint(self):
        sess = FakeSession([
            FakeResponse(data={"data": {
                "from": "no-reply@openai.com", "subject": "Your code", "code": "654321"
            }}),
        ])
        messages = temp_email._icloud_fetch(
            "icloud@example.com",
            "icloud@example.com",
            "TWO_FACTOR_SECRET",
            "",
            "https://icloud-api.example/s/opaque/icloud@example.com",
            sess,
        )
        self.assertEqual(messages[0]["code"], "654321")
        self.assertEqual(sess.calls[0][1], "https://icloud-api.example/s/opaque/icloud@example.com")
        self.assertNotIn("params", sess.calls[0][2])

    def test_fetch_accepts_keyless_share_endpoint(self):
        sess = FakeSession([
            FakeResponse(data={"data": {
                "subject": "Your code", "code": "789012"
            }}),
        ])
        messages = temp_email._icloud_fetch(
            "icloud@example.com",
            "icloud@example.com",
            "",
            "",
            "https://icloud-api.example/api/share/share-token",
            sess,
        )
        self.assertEqual(messages[0]["code"], "789012")
        self.assertEqual(
            sess.calls[0][1],
            "https://icloud-api.example/api/share/share-token",
        )
        self.assertNotIn("params", sess.calls[0][2])

    def test_fetch_accepts_nested_messages_and_plain_text_endpoint(self):
        nested = FakeSession([
            FakeResponse(data={"data": {"messages": [
                {"subject": "Your code", "text": "Use 246810"},
            ]}}),
        ])
        messages = temp_email._icloud_fetch(
            "icloud@example.com", "icloud@example.com", "", "",
            "https://icloud-api.example/s/opaque/icloud@example.com", nested,
        )
        self.assertEqual(messages[0]["text"], "Use 246810")

        plain = FakeSession([
            FakeResponse(data=None, text="OpenAI verification code: 135790"),
        ])
        messages = temp_email._icloud_fetch(
            "icloud@example.com", "icloud@example.com", "", "",
            "https://icloud-api.example/s/opaque/icloud@example.com", plain,
        )
        self.assertIn("135790", messages[0]["text"])

    def test_custom_numeric_pattern_never_falls_back_to_dashed_code(self):
        dashed = {
            "from": "no-reply@openai.com",
            "subject": "Your ChatGPT login code",
            "html": "<style>.code{color:#2B-2F}</style><p>Enter the code.</p>",
            "code": "2B-2F",
            "extracted": {"codes": ["2B-2F"], "links": []},
        }
        numeric = {
            **dashed,
            "html": "<p>Enter the code.</p>",
            "code": "654321",
            "extracted": {"codes": ["654321"], "links": []},
        }
        args = (
            "icloud@example.com", "icloud", "icloud@example.com", "", "",
            "https://icloud-api.example/s/opaque/icloud@example.com",
            ("openai",), ("code",), r"\b(\d{6})\b",
        )

        with patch.object(temp_email, "fetch_messages", return_value=[dashed]):
            self.assertIsNone(temp_email._scan_once(*args))
        with patch.object(temp_email, "fetch_messages", return_value=[numeric]):
            self.assertEqual(temp_email._scan_once(*args), "654321")


class RemailMailTests(unittest.TestCase):
    def test_create_uses_code_order_contract_and_normalizes_token(self):
        sess = FakeSession([
            FakeResponse(data={"id": 3365715, "deliveryEmail": "a@outlook.com", "serviceToken": "service-token"}),
        ])
        with patch.object(temp_email, "REMAIL_PROJECT_ID", 58), patch.object(
            temp_email, "REMAIL_EMAIL_SUFFIX", "outlook.com"
        ):
            mailbox = temp_email._remail_create(
                None, None, None, "rk-test", "https://remail.aishop6.com", sess
            )

        self.assertEqual(mailbox["email"], "a@outlook.com")
        self.assertEqual(mailbox["token"], "service-token")
        method, url, kwargs = sess.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url,
            "https://remail.aishop6.com/v1/open/orders?serviceMode=code&supply=private_first",
        )
        self.assertEqual(kwargs["json"], {"projectId": 58, "emailSuffix": "outlook.com"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer rk-test")
        self.assertTrue(kwargs["headers"]["Idempotency-Key"])

    def test_fetch_maps_items_and_verification_code(self):
        sess = FakeSession([
            FakeResponse(data={"items": [{
                "sender": "noreply@openai.com",
                "subject": "Your ChatGPT code",
                "bodyPreview": "Use 829104 to continue",
                "verificationCode": "829104",
            }]}),
        ])
        with patch.object(temp_email, "_session", return_value=sess):
            messages = temp_email.fetch_messages(
                "3365715", "remail", email="a@outlook.com", token="service-token",
                api_key="rk-test", base_url="https://remail.aishop6.com",
            )
        self.assertEqual(messages[0]["verificationCode"], "829104")
        self.assertEqual(messages[0]["extracted"]["codes"], ["829104"])
        self.assertEqual(sess.calls[0][1], "https://remail.aishop6.com/v1/pickup")
        self.assertEqual(sess.calls[0][2]["params"], {
            "email": "a@outlook.com", "token": "service-token"
        })



if __name__ == "__main__":
    unittest.main()
