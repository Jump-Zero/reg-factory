import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import register_three_platforms
import register_kiro
from common import asset_scanner, asset_store
from common.kiro_crypto import FingerprintBuilder, _xxtea_decrypt, _xxtea_encrypt, encrypt_password
from common.session_export import (
    build_kiro_rs_credentials,
    export_kiro_rs_credentials,
    save_kiro_token,
)
from webui.scripts import ENV_SCHEMA, SCRIPTS


class KiroCryptoTests(unittest.TestCase):
    def test_query_reads_fragment_query_parameters(self):
        self.assertEqual(
            register_kiro._query(
                "https://profile.aws.amazon.com/#/signup/start?workflowID=workflow-123",
                "workflowID",
            ),
            "workflow-123",
        )

    def test_xxtea_roundtrip(self):
        builder = FingerprintBuilder()
        raw = "fingerprint payload with unicode-free content"
        encrypted = _xxtea_encrypt(raw, builder.key)
        self.assertEqual(_xxtea_decrypt(encrypted, builder.key), raw)

    def test_fingerprint_has_expected_envelope(self):
        value = FingerprintBuilder().encrypted(
            "https://us-east-1.signin.aws/platform/directory/login",
            "https://view.awsapps.com/", "signin", "first_load",
        )
        identifier, encoded = value.split(":", 1)
        self.assertTrue(identifier)
        self.assertGreater(len(encoded), 100)

    def test_jwe_compact_serialization(self):
        # A 512-bit key is intentionally not accepted by RSA-OAEP-256; use a
        # generated test key and only verify the wire shape here.
        from cryptography.hazmat.primitives.asymmetric import rsa
        import base64
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()
        enc = encrypt_password(
            "Aa1!test-password", {
                "kid": "test", "n": base64.urlsafe_b64encode(key.n.to_bytes((key.n.bit_length() + 7) // 8, "big")).decode().rstrip("="),
                "e": base64.urlsafe_b64encode(key.e.to_bytes((key.e.bit_length() + 7) // 8, "big")).decode().rstrip("="),
            },
        )
        self.assertEqual(len(enc.split(".")), 5)


class KiroIntegrationTests(unittest.TestCase):
    def test_tls_transport_error_falls_back_to_standard_requests(self):
        client = register_kiro.KiroClient(proxy="http://proxy.test:9000")
        primary = client.session
        primary.request = MagicMock(
            side_effect=RuntimeError(
                "curl: (35) TLS connect error: OPENSSL_internal:invalid library"
            )
        )
        response = MagicMock(status_code=200)
        fallback = MagicMock()
        fallback.headers = {}
        fallback.cookies = MagicMock()
        fallback.request.return_value = response

        with patch.object(
            register_kiro.standard_requests, "Session", return_value=fallback
        ):
            result = client.get("https://example.test/resource")

        self.assertIs(result, response)
        fallback.request.assert_called_once()
        self.assertEqual(
            fallback.proxies,
            {"http": "http://proxy.test:9000", "https": "http://proxy.test:9000"},
        )
        self.assertIs(client.session, fallback)

    def test_app_config_is_downloaded_once_per_batch(self):
        first = register_kiro.KiroClient()
        second = register_kiro.KiroClient()
        response = type("Response", (), {"text": ""})()
        with patch.object(register_kiro, "_APP_CONFIG_CACHE", None):
            with patch.object(first, "get", return_value=response) as first_get:
                first.fetch_app_config()
            with patch.object(second, "get", return_value=response) as second_get:
                second.fetch_app_config()
        first_get.assert_called_once()
        second_get.assert_not_called()

    def test_orchestrator_builds_kiro_command(self):
        args = argparse.Namespace(
            timeout=600,
            node="auto",
            kiro_account_password="",
            kiro_full_name="Batch User",
        )
        command = register_three_platforms.build_command("kiro", args, ("user@example.com", "mail-pass", "rt", "cid"))
        self.assertIn("register_kiro.py", command)
        self.assertIn("--refresh-token", command)
        self.assertIn("--client-id", command)
        self.assertEqual(command[command.index("--full-name") + 1], "Batch User")

    def test_schema_and_proxy_route_expose_kiro(self):
        task = next(item for item in SCRIPTS if item["id"] == "register_kiro")
        self.assertEqual(task["file"], "register_kiro.py")
        provider = next(arg for arg in task["args"] if arg["flag"] == "--temp-provider")
        self.assertEqual(provider["type"], "choice")
        self.assertIn("yyds", provider["choices"])
        self.assertIn("remail", provider["choices"])
        export_task = next(item for item in SCRIPTS if item["id"] == "export_kiro_credentials")
        self.assertEqual(export_task["file"], "tools/export_kiro_credentials.py")
        keys = {item["key"] for group in ENV_SCHEMA for item in group["items"]}
        self.assertIn("KIRO_PROXY_MODE", keys)

    def test_save_and_read_kiro_account_asset(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "TOKEN_OUTPUT_DIR": "tokens", "REG_FACTORY_DATA_DIR": root,
        }, clear=False):
            with patch("common.session_export.TOKEN_OUTPUT_DIR", str(Path(root) / "tokens")):
                self.assertTrue(save_kiro_token({
                    "email": "user@example.com", "refreshToken": "rt", "clientId": "cid",
                    "clientSecret": "secret", "provider": "BuilderId", "expiresIn": 3600,
                }, "user@example.com"))
            path = Path(root) / "tokens" / "kiro" / "user@example.com.account.json"
            aggregate = Path(root) / "tokens" / "kiro" / "credentials.json"
            self.assertTrue(path.is_file())
            self.assertTrue(aggregate.is_file())
            credential = __import__("json").loads(path.read_text(encoding="utf-8"))
            self.assertEqual(credential["authMethod"], "idc")
            self.assertEqual(credential["refreshToken"], "rt")
            self.assertIn("expiresAt", credential)
            self.assertEqual(__import__("json").loads(aggregate.read_text(encoding="utf-8")), [credential])
            with patch.object(asset_store, "_data_root", return_value=Path(root)):
                with patch.object(asset_store, "_token_root", return_value=Path(root) / "tokens"):
                    result = asset_store.get_platform_asset("kiro", "session", index=0)
            self.assertEqual(result["data"]["clientId"], "cid")

    def test_kiro_rs_converter_drops_internal_registration_fields(self):
        credential = build_kiro_rs_credentials({
            "email": "user@example.com",
            "password": "account-password",
            "provider": "BuilderId",
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "secret",
            "updatedAt": 123,
        })
        self.assertEqual(credential, {
            "refreshToken": "rt",
            "authMethod": "idc",
            "clientId": "cid",
            "clientSecret": "secret",
            "email": "user@example.com",
        })

    def test_kiro_rs_export_supports_a_custom_output_path(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "TOKEN_OUTPUT_DIR": "tokens", "REG_FACTORY_DATA_DIR": root,
        }, clear=False):
            token_root = Path(root) / "tokens"
            with patch("common.session_export.TOKEN_OUTPUT_DIR", str(token_root)):
                self.assertTrue(save_kiro_token({
                    "email": "user@example.com", "refreshToken": "rt", "clientId": "cid",
                    "clientSecret": "secret", "provider": "BuilderId",
                }, "user@example.com"))
                output = Path(root) / "exports" / "credentials.json"
                path, credentials = export_kiro_rs_credentials(str(output))
                self.assertEqual(Path(path), output)
                self.assertEqual(len(credentials), 1)
                self.assertEqual(credentials[0]["authMethod"], "idc")
                self.assertTrue(output.is_file())

    def test_kiro_rs_social_credentials_do_not_require_idc_client_keys(self):
        self.assertEqual(build_kiro_rs_credentials({
            "refreshToken": "social-rt",
            "authMethod": "social",
            "profileArn": "arn:aws:codewhisperer:us-east-1:123:profile/test",
        }), {
            "refreshToken": "social-rt",
            "authMethod": "social",
            "profileArn": "arn:aws:codewhisperer:us-east-1:123:profile/test",
        })

    def test_scanner_includes_kiro_platform(self):
        self.assertIn("kiro", asset_scanner._SCANNERS)


if __name__ == "__main__":
    unittest.main()
