import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common import sms


class SmsRoutingTests(unittest.TestCase):
    def setUp(self):
        sms._AUTO_PROVIDER_CURSOR = 0

    def test_auto_uses_firefox_before_smsman_when_available(self):
        response = SimpleNamespace(
            text="1|firefox-key|x|x|60|x|x|15551234567"
        )
        with patch.object(sms, "SMS_TOKEN", "firefox-token"), \
             patch.object(sms, "SMSMAN_TOKEN", "smsman-token"), \
             patch.object(sms, "HERO_SMS_API_KEY", "hero-key"), \
             patch.object(sms.requests, "get", return_value=response) as request, \
             patch.object(sms, "_smsman_get_phone") as smsman, \
             patch.object(sms, "_hero_get_phone") as hero:
            result = sms.get_phone(
                "2313", "dr", smsman_app="openai", provider="auto"
            )

        self.assertEqual(result, ("15551234567", "60", "firefox-key"))
        request.assert_called_once()
        smsman.assert_not_called()
        hero.assert_not_called()

    def test_auto_switches_after_firefox_failure(self):
        response = SimpleNamespace(text="0|-8")
        with patch.object(sms, "SMS_TOKEN", "firefox-token"), \
             patch.object(sms, "SMSMAN_TOKEN", "smsman-token"), \
             patch.object(sms, "HERO_SMS_API_KEY", "hero-key"), \
             patch.object(sms.requests, "get", return_value=response), \
             patch.object(sms, "_smsman_get_phone", return_value=None) as smsman, \
             patch.object(sms, "_hero_get_phone", return_value=("15550001111", "hero_1")) as hero, \
             patch.object(sms.time, "sleep"):
            result = sms.get_phone(
                "2313", "dr", max_retries=1, smsman_app="openai", provider="auto"
            )

        self.assertEqual(result, ("15550001111", "", "hero_1"))
        smsman.assert_called_once()
        hero.assert_called_once_with("dr")

    def test_auto_rotates_starting_provider_between_requests(self):
        with patch.object(sms, "SMS_TOKEN", "firefox-token"), \
             patch.object(sms, "SMSMAN_TOKEN", "smsman-token"), \
             patch.object(sms, "HERO_SMS_API_KEY", "hero-key"):
            first = sms._auto_provider_order("2313", "openai", "dr")
            second = sms._auto_provider_order("2313", "openai", "dr")
            third = sms._auto_provider_order("2313", "openai", "dr")
        self.assertEqual(first, ["firefox", "smsman", "hero"])
        self.assertEqual(second, ["smsman", "hero", "firefox"])
        self.assertEqual(third, ["hero", "firefox", "smsman"])


if __name__ == "__main__":
    unittest.main()
