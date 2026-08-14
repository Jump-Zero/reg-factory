import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import register_outlook_standalone
import unlock_outlook
from common import asset_scanner
from webui.scripts import SCRIPTS


class OutlookMergedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_webui_exposes_one_combined_recovery_task(self):
        ids = [item["id"] for item in SCRIPTS]
        self.assertIn("unlock_outlook", ids)
        self.assertNotIn("extract_graph_tokens", ids)

        recovery = next(item for item in SCRIPTS if item["id"] == "unlock_outlook")
        self.assertIn("Graph", recovery["title"])
        statuses = next(arg for arg in recovery["args"] if arg["flag"] == "--statuses")
        self.assertEqual(statuses["default"], ["unlock", "expired", "unknown"])

    def test_registration_and_recovery_share_the_same_press_implementation(self):
        self.assertIs(
            register_outlook_standalone._outlook_press,
            unlock_outlook._outlook_press,
        )

    async def test_graph_extraction_prefers_the_live_browser_session(self):
        browser_graph = {
            "refresh_token": "browser-refresh-token",
            "client_id": "browser-client-id",
        }
        with (
            patch.object(
                register_outlook_standalone,
                "extract_graph_token",
                AsyncMock(return_value=browser_graph),
            ) as browser_extract,
            patch(
                "tools.extract_graph_tokens.get_graph_token",
                return_value=None,
            ) as http_extract,
        ):
            result = await unlock_outlook.extract_graph_after_recovery(
                "page", "context", "user@outlook.com", "password", 1
            )

        browser_extract.assert_awaited_once_with(
            "page", "context", "user@outlook.com", "password", 1
        )
        http_extract.assert_not_called()
        self.assertEqual(result["email"], "user@outlook.com")
        self.assertEqual(result["password"], "password")
        self.assertEqual(result["refresh_token"], "browser-refresh-token")

    async def test_graph_extraction_falls_back_to_http(self):
        with (
            patch.object(
                register_outlook_standalone,
                "extract_graph_token",
                AsyncMock(return_value=None),
            ),
            patch(
                "tools.extract_graph_tokens.get_graph_token",
                return_value={
                    "email": "user@outlook.com",
                    "password": "password",
                    "refresh_token": "http-refresh-token",
                    "client_id": "http-client-id",
                },
            ) as http_extract,
        ):
            result = await unlock_outlook.extract_graph_after_recovery(
                "page", "context", "user@outlook.com", "password", 2
            )

        http_extract.assert_called_once_with(
            "user@outlook.com", "password", 2
        )
        self.assertEqual(result["refresh_token"], "http-refresh-token")

    async def test_worker_extracts_graph_before_browser_context_closes(self):
        events = []
        page = MagicMock()
        context = MagicMock(pages=[page])
        browser = MagicMock(contexts=[context])
        client = MagicMock()

        async def extract(*_args, **_kwargs):
            events.append("graph_extracted")
            return {
                "email": "user@outlook.com",
                "password": "password",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
            }

        results = []
        graph_attempts = []
        with (
            patch.object(
                unlock_outlook,
                "open_and_connect",
                AsyncMock(return_value=(client, "profile", browser, context, page)),
            ) as connect,
            patch.object(
                unlock_outlook,
                "teardown",
                AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("context_closed")),
            ) as teardown,
            patch.object(
                unlock_outlook,
                "unlock_account",
                AsyncMock(return_value="unlocked"),
            ),
            patch.object(
                unlock_outlook,
                "extract_graph_after_recovery",
                side_effect=extract,
            ),
        ):
            await unlock_outlook.worker(
                [("user@outlook.com", "password", "user@outlook.com----password")],
                None,
                0,
                results,
                graph_attempts,
                asyncio.Semaphore(1),
            )

        self.assertEqual(events, ["graph_extracted", "context_closed"])
        connect.assert_awaited_once()
        teardown.assert_awaited_once_with(client, "profile", delete=True)
        self.assertEqual(results[0][3], "unlocked")
        self.assertEqual(
            graph_attempts[0]["result"]["refresh_token"], "refresh-token"
        )

    def test_unlock_alone_does_not_mark_merged_recovery_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(unlock_outlook, "OUTPUT_DIR", directory),
                patch.object(asset_scanner, "update_cached_outlook_statuses") as update,
            ):
                unlock_outlook.save_results(
                    [
                        (
                            "ok@outlook.com",
                            "password",
                            "ok@outlook.com----password",
                            "unlocked",
                        ),
                        (
                            "phone@outlook.com",
                            "password",
                            "phone@outlook.com----password",
                            "needs_phone",
                        ),
                    ],
                    "20260729_000000",
                )

        outcomes = update.call_args.args[0]
        self.assertNotIn("ok@outlook.com", outcomes)
        self.assertEqual(outcomes["phone@outlook.com"]["status"], "unlock")


if __name__ == "__main__":
    unittest.main()
