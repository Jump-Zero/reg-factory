import unittest
from unittest.mock import patch

from common import uploaders


class Sub2ApiGrokTests(unittest.TestCase):
    def test_registration_risk_denial_stops_before_sub2api_login(self):
        state = {
            "denied": True,
            "bot_flag_source": 1,
            "bot_flag_details": "policy=deny,risk=1.00,event=$registration",
        }
        with patch(
            "common.grok_oauth.inspect_grok_account_state", return_value=state
        ), patch.object(uploaders, "_sub2api_request") as request:
            ok, message = uploaders.upload_sub2api_grok(
                "https://sub.example.com",
                "admin@example.com",
                "secret",
                "grok",
                "sso-token",
                account_email="denied@example.com",
                local_proxy="http://127.0.0.1:7897",
            )

        self.assertFalse(ok)
        self.assertIn("注册风控拒绝", message)
        request.assert_not_called()

    def test_imports_sso_into_grok_group(self):
        responses = [
            {"access_token": "admin-token"},
            [
                {"id": 4, "name": "grok", "platform": "openai"},
                {"id": 9, "name": "grok", "platform": "grok"},
            ],
            {"created": [{"email": "new@example.com"}], "failed": []},
        ]
        with patch.object(uploaders, "_sub2api_request", side_effect=responses) as request:
            ok, message = uploaders.upload_sub2api_grok(
                "https://sub.example.com",
                "admin@example.com",
                "secret",
                "grok",
                "sso-token",
                account_email="new@example.com",
                proxy_id=12,
            )

        self.assertTrue(ok)
        self.assertIn("new@example.com", message)
        import_call = request.call_args_list[2]
        self.assertEqual(import_call.args[1], "/api/v1/admin/grok/sso-to-oauth")
        self.assertEqual(import_call.kwargs["body"]["sso_tokens"], ["sso-token"])
        self.assertEqual(import_call.kwargs["body"]["group_ids"], [9])
        self.assertEqual(import_call.kwargs["body"]["name"], "new@example.com")
        self.assertEqual(import_call.kwargs["body"]["proxy_id"], 12)
        self.assertEqual(import_call.kwargs["retries"], 1)

    def test_rejects_same_name_openai_group(self):
        responses = [
            {"access_token": "admin-token"},
            [{"id": 4, "name": "grok", "platform": "openai"}],
        ]
        with patch.object(uploaders, "_sub2api_request", side_effect=responses) as request:
            ok, message = uploaders.upload_sub2api_grok(
                "https://sub.example.com",
                "admin@example.com",
                "secret",
                "grok",
                "sso-token",
            )

        self.assertFalse(ok)
        self.assertIn("grok 分组", message)
        self.assertEqual(request.call_count, 2)

    def test_reports_conversion_failure(self):
        responses = [
            {"access_token": "admin-token"},
            [{"id": 9, "name": "grok", "platform": "grok"}],
            {"created": [], "failed": [{"index": 1, "error": "device flow denied"}]},
        ]
        with patch.object(uploaders, "_sub2api_request", side_effect=responses):
            ok, message = uploaders.upload_sub2api_grok(
                "https://sub.example.com",
                "admin@example.com",
                "secret",
                "grok",
                "sso-token",
            )

        self.assertFalse(ok)
        self.assertIn("device flow denied", message)

    def test_prefers_local_oauth_over_remote_conversion(self):
        responses = [
            {"access_token": "admin-token"},
            [{"id": 9, "name": "grok", "platform": "grok"}],
            {"items": []},
            {"id": 99, "platform": "grok", "type": "oauth"},
        ]
        credentials = {
            "access_token": "access",
            "refresh_token": "refresh",
            "email": "new@example.com",
        }
        with patch.object(uploaders, "_sub2api_request", side_effect=responses) as request:
            with patch(
                "common.grok_oauth.convert_grok_sso_local",
                return_value=(credentials, "new@example.com"),
            ) as convert, patch(
                "common.grok_oauth.inspect_grok_account_state",
                return_value={"denied": False},
            ):
                ok, message = uploaders.upload_sub2api_grok(
                    "https://sub.example.com",
                    "admin@example.com",
                    "secret",
                    "grok",
                    "sso-token",
                    account_email="new@example.com",
                    local_proxy="http://127.0.0.1:7897",
                    proxy_id=12,
                )

        self.assertTrue(ok)
        self.assertIn("本机 OAuth", message)
        convert.assert_called_once()
        create_call = request.call_args_list[3]
        self.assertEqual(create_call.args[1], "/api/v1/admin/accounts")
        self.assertEqual(create_call.kwargs["body"]["platform"], "grok")
        self.assertEqual(create_call.kwargs["body"]["group_ids"], [9])
        self.assertEqual(create_call.kwargs["body"]["credentials"], credentials)
        self.assertEqual(create_call.kwargs["body"]["proxy_id"], 12)
        self.assertFalse(create_call.kwargs["use_env_proxy"])

    def test_local_oauth_repairs_existing_401_account(self):
        responses = [
            {"access_token": "admin-token"},
            [{"id": 9, "name": "grok", "platform": "grok"}],
            {"items": [{"id": 41, "name": "new@example.com", "status": "error"}]},
            {"id": 41, "name": "new@example.com", "status": "active"},
        ]
        credentials = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "email": "new@example.com",
        }
        with patch.object(uploaders, "_sub2api_request", side_effect=responses) as request:
            with patch(
                "common.grok_oauth.convert_grok_sso_local",
                return_value=(credentials, "new@example.com"),
            ), patch(
                "common.grok_oauth.inspect_grok_account_state",
                return_value={"denied": False},
            ):
                ok, message = uploaders.upload_sub2api_grok(
                    "https://sub.example.com",
                    "admin@example.com",
                    "secret",
                    "grok",
                    "sso-token",
                    account_email="new@example.com",
                    local_proxy="http://127.0.0.1:7897",
                )

        self.assertTrue(ok)
        self.assertIn("凭据已修复", message)
        update_call = request.call_args_list[3]
        self.assertEqual(update_call.args[1], "/api/v1/admin/accounts/41")
        self.assertEqual(update_call.kwargs["method"], "PUT")
        self.assertEqual(update_call.kwargs["body"]["credentials"], credentials)
        self.assertEqual(update_call.kwargs["body"]["status"], "active")
        self.assertEqual(update_call.kwargs["body"]["group_ids"], [9])

    def test_remote_conversion_is_used_when_local_oauth_fails(self):
        responses = [
            {"access_token": "admin-token"},
            [{"id": 9, "name": "grok", "platform": "grok"}],
            {"created": [{"email": "new@example.com"}], "failed": []},
        ]
        with patch.object(uploaders, "_sub2api_request", side_effect=responses) as request:
            with patch(
                "common.grok_oauth.convert_grok_sso_local",
                side_effect=RuntimeError("local denied"),
            ) as convert, patch(
                "common.grok_oauth.inspect_grok_account_state",
                return_value={"denied": False},
            ), patch.object(uploaders.time, "sleep"):
                ok, message = uploaders.upload_sub2api_grok(
                    "https://sub.example.com",
                    "admin@example.com",
                    "secret",
                    "grok",
                    "sso-token",
                    account_email="new@example.com",
                    local_proxy="http://127.0.0.1:7897",
                )

        self.assertTrue(ok)
        self.assertIn("new@example.com", message)
        self.assertEqual(convert.call_count, 2)
        remote_call = request.call_args_list[2]
        self.assertEqual(remote_call.args[1], "/api/v1/admin/grok/sso-to-oauth")


if __name__ == "__main__":
    unittest.main()
