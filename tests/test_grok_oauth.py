import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from common import grok_oauth
from xconsole_client.xai_oauth import build_authorization_url


def _jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


class _Response:
    def __init__(self, status=200, url="", payload=None, text=""):
        self.status_code = status
        self.url = url
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _DeviceFlowSession:
    def __init__(self, sso, token):
        self.sso = sso
        self.token = token
        self.requests = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url == "https://accounts.x.ai/":
            return _Response(url="https://accounts.x.ai/home")
        if url.endswith("/oauth2/device/code"):
            return _Response(
                url=url,
                payload={
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": (
                        "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH"
                    ),
                    "interval": 2,
                },
            )
        if method == "GET" and "/oauth2/device?" in url:
            return _Response(url=url)
        if url.endswith("/oauth2/device/verify"):
            return _Response(url="https://auth.x.ai/oauth2/device/consent")
        if url.endswith("/oauth2/device/approve"):
            return _Response(url="https://auth.x.ai/oauth2/device/done")
        raise AssertionError(f"unexpected request: {method} {url}")

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        if url == grok_oauth.XAI_TOKEN_ENDPOINT:
            return _Response(url=url, payload=self.token)
        raise AssertionError(f"unexpected post: {url}")

    def close(self):
        self.closed = True


class GrokOAuthTests(unittest.TestCase):
    def test_browser_device_url_uses_live_grok_route(self):
        self.assertEqual(
            grok_oauth._browser_device_verification_url(
                "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH"
            ),
            "https://grok.com/oauth2/device?user_code=ABCD-EFGH",
        )

    def test_authorization_url_uses_current_grok_referrer(self):
        url = build_authorization_url(
            client_id=grok_oauth.XAI_CLIENT_ID,
            redirect_uri="http://127.0.0.1:56121/callback",
            state="state",
            nonce="nonce",
            code_challenge="challenge",
            scopes=grok_oauth.XAI_SCOPE.split(),
        )
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["plan"], ["generic"])
        self.assertEqual(query["referrer"], ["grok-build"])

    def test_parses_registration_risk_denial_from_escaped_rsc(self):
        state = grok_oauth._parse_grok_account_state(
            r'{\"botFlagSource\":1,\"botFlagDetails\":'
            r'\"policy=deny,risk=1.00,event=$registration\"}'
        )

        self.assertTrue(state["found"])
        self.assertTrue(state["denied"])
        self.assertEqual(state["bot_flag_source"], 1)
        self.assertEqual(state["risk"], 1.0)

    def test_non_registration_denial_does_not_block_oauth(self):
        state = grok_oauth._parse_grok_account_state(
            '"botFlagSource":2,"botFlagDetails":'
            '"policy=deny,risk=0.50,event=$login"'
        )

        self.assertTrue(state["found"])
        self.assertFalse(state["denied"])

    def test_scope_matches_supported_grok_cli_scope(self):
        self.assertEqual(
            grok_oauth.XAI_SCOPE,
            "openid profile email offline_access grok-cli:access api:access",
        )
        self.assertNotIn("conversations:", grok_oauth.XAI_SCOPE)

    def test_credentials_include_refresh_and_cli_metadata(self):
        access = _jwt({"sub": "user-1", "email": "user@example.com", "exp": 2000000000})
        credentials, email = grok_oauth._build_credentials(
            {
                "access_token": access,
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 21600,
                "scope": grok_oauth.XAI_SCOPE,
            }
        )

        self.assertEqual(email, "user@example.com")
        self.assertEqual(credentials["refresh_token"], "refresh-token")
        self.assertEqual(credentials["client_id"], grok_oauth.XAI_CLIENT_ID)
        self.assertEqual(credentials["token_endpoint"], grok_oauth.XAI_TOKEN_ENDPOINT)
        self.assertEqual(credentials["base_url"], grok_oauth.XAI_CLI_BASE_URL)
        self.assertEqual(credentials["headers"]["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertEqual(
            credentials["headers"]["x-grok-client-version"],
            grok_oauth.XAI_CLI_VERSION,
        )

    def test_device_flow_uses_sso_principal_and_supported_scope(self):
        sso = _jwt({"sub": "principal-123"})
        access = _jwt({"sub": "principal-123", "email": "user@example.com", "exp": 2000000000})
        session = _DeviceFlowSession(
            sso,
            {
                "access_token": access,
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 21600,
            },
        )

        with patch.object(grok_oauth, "_new_sso_session", return_value=session):
            credentials, email = grok_oauth.convert_grok_sso_local(
                sso,
                "http://127.0.0.1:7897",
            )

        device_request = next(item for item in session.requests if item[1].endswith("/device/code"))
        approve_request = next(item for item in session.requests if item[1].endswith("/device/approve"))
        self.assertEqual(device_request[2]["data"]["scope"], grok_oauth.XAI_SCOPE)
        self.assertEqual(approve_request[2]["data"]["principal_id"], "principal-123")
        self.assertEqual(credentials["refresh_token"], "refresh-token")
        self.assertEqual(email, "user@example.com")
        self.assertTrue(session.closed)

    def test_invalid_grant_is_retried_during_authorization_grace(self):
        class PollSession:
            def __init__(self):
                self.responses = [
                    _Response(400, payload={"error": "invalid_grant"}),
                    _Response(200, payload={"access_token": "access"}),
                ]

            def post(self, *_args, **_kwargs):
                return self.responses.pop(0)

        with patch.object(grok_oauth.time, "sleep"):
            token = grok_oauth._poll_device_token(PollSession(), "device", 1, 15)

        self.assertEqual(token["access_token"], "access")


if __name__ == "__main__":
    unittest.main()
