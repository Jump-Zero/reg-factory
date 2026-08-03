import unittest

from xconsole_client.client import XConsoleAuthClient


class SignupResponseTests(unittest.TestCase):
    def test_scrapes_action_from_chunk_url_with_deployment_query(self):
        action_id = "7f" + "a" * 40
        html = (
            '<link rel="preload" as="script" '
            'href="/_next/static/chunks/signup.js?dpl=0123456789abcdef">'
        )
        requested = []
        client = object.__new__(XConsoleAuthClient)
        client.debug = False
        client._base_headers = lambda: {}

        def fake_request(method, url, **_kwargs):
            requested.append((method, url))
            javascript = (
                f'createUserAndSessionRequest;createServerReference("{action_id}")'
            )
            return 200, {}, [], javascript

        client._request = fake_request

        self.assertEqual(client._scrape_action_id(html), action_id)
        self.assertEqual(
            requested,
            [
                (
                    "GET",
                    "https://accounts.x.ai/_next/static/chunks/signup.js"
                    "?dpl=0123456789abcdef",
                )
            ],
        )

    def test_unknown_non_error_rsc_shape_continues_to_sso_extraction(self):
        self.assertTrue(
            XConsoleAuthClient._signup_response_looks_ok(
                '2:["new-rsc-shape",{"status":"pending"}]',
                ["next-auth.csrf-token=value; Path=/"],
                {},
            )
        )

    def test_structured_error_still_fails(self):
        self.assertFalse(
            XConsoleAuthClient._signup_response_looks_ok(
                '0:E{"message":"turnstile_failed"}',
                [],
                {},
            )
        )


if __name__ == "__main__":
    unittest.main()
