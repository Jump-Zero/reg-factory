# -*- coding: utf-8 -*-
"""LIYE 卡密式接码离线测试：前缀服务识别、卡池按服务过滤、ChatGPT 链路路由。"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from common import liye_sms


def _write_state(root, cards):
    os.makedirs(os.path.join(root, "runtime", "state"), exist_ok=True)
    with open(os.path.join(root, "runtime", "state", "liye_cards.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "cards": cards}, f)


class ServiceDetectTests(unittest.TestCase):
    def test_prefix_maps_to_service(self):
        self.assertEqual(liye_sms._service_for_card("GPT-abc123"), "chatai")
        self.assertEqual(liye_sms._service_for_card("CZ-xyz"), "chatai")
        self.assertEqual(liye_sms._service_for_card("GOO-12345"), "google")

    def test_unknown_prefix_falls_back_to_config(self):
        with patch.object(liye_sms, "LIYE_SERVICE", "chatai"):
            self.assertEqual(liye_sms._service_for_card("RAWCODE"), "chatai")
        with patch.object(liye_sms, "LIYE_SERVICE", ""):
            self.assertEqual(liye_sms._service_for_card("RAWCODE"), "chatai")

    def test_pick_available_skips_other_service_cards(self):
        state = {"cards": [
            {"code": "GOO-bbb", "status": "available"},
            {"code": "GPT-aaa", "status": "available"},
        ]}
        # ChatGPT(chatai) 取号必须跳过 GOO- 的 Gmail 卡，即使它排在卡池前面
        self.assertEqual(liye_sms._pick_available(state, service="chatai")["code"], "GPT-aaa")

    def test_pick_available_skips_cooldown(self):
        state = {"cards": [
            {"code": "GPT-aaa", "status": "available", "cooldown_until": 9999999999},
            {"code": "GPT-ccc", "status": "available"},
        ]}
        picked = liye_sms._pick_available(state, service="chatai")
        self.assertEqual(picked["code"], "GPT-ccc")


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_test_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        # 屏蔽 .env 里的真实卡密，避免 sync_cards 把生产卡带进测试状态
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()
        _write_state(self._tmp, [
            {"code": "GOO-bbb", "status": "available"},
            {"code": "GPT-aaa", "status": "available"},
        ])
        liye_sms._SESSIONS.clear()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()

    def _order(self, order_id="ord1", phone="15550001111"):
        return {"id": order_id, "phone": phone, "activationId": "act1",
                "activationGeneration": 0, "status": "waiting"}

    def test_claim_default_picks_chatai_card_and_skips_goO(self):
        with patch.object(liye_sms, "_claim_one") as claim_one:
            claim_one.return_value = ("15550001111", "", self._order())
            phone, dial, pkey = liye_sms.claim(max_cards=1)
        self.assertEqual((phone, dial, pkey), ("15550001111", "", "liye_ord1"))
        # 只应拿到 GPT- 卡(chatai)，不能错拿排在卡池前面的 GOO- Gmail 卡
        self.assertEqual(claim_one.call_args[0][0], "GPT-aaa")
        self.assertEqual(claim_one.call_args[1]["service"], "chatai")

    def test_claim_raises_when_only_other_service_cards(self):
        _write_state(self._tmp, [{"code": "GOO-bbb", "status": "available"}])
        with self.assertRaises(RuntimeError) as ctx:
            liye_sms.claim(max_cards=1)
        self.assertIn("service chatai", str(ctx.exception))

    def test_claim_stores_service_on_entry(self):
        with patch.object(liye_sms, "_claim_one") as claim_one:
            claim_one.return_value = ("15550001111", "", self._order())
            liye_sms.claim(max_cards=1)
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"), encoding="utf-8") as f:
            state = json.load(f)
        entry = next(c for c in state["cards"] if c["code"] == "GPT-aaa")
        self.assertEqual(entry["service"], "chatai")
        self.assertEqual(entry["status"], "in_use")
        self.assertEqual(entry["order_id"], "ord1")


class SessionKeyTests(unittest.TestCase):
    def test_login_service_defaults_to_prefix(self):
        liye_sms._SESSIONS.clear()
        with patch.object(liye_sms, "_api") as api:
            liye_sms._login("GPT-aaa")
            liye_sms._login("GPT-aaa")  # 命中缓存不再登录
            liye_sms._login("CZ-bbb")
        logins = [c for c in api.call_args_list if c[0][2] == "/api/card/login"]
        self.assertEqual(len(logins), 2)
        self.assertEqual(logins[0][0][3]["service"], "chatai")
        self.assertEqual(logins[1][0][3]["service"], "chatai")

    def test_drop_session_clears_all_services(self):
        liye_sms._SESSIONS.clear()
        with patch.object(liye_sms, "_api"):
            liye_sms._login("GPT-aaa", service="chatai")
            liye_sms._login("GPT-aaa", service="google")
        self.assertEqual(len(liye_sms._SESSIONS), 2)
        liye_sms._drop_session("GPT-aaa")
        self.assertEqual(liye_sms._SESSIONS, {})


class ImportTextTests(unittest.TestCase):
    """WebUI 卡密导入：多卡解析、去重、注释/非法行处理。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_test_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()

    def _txt(self):
        path = os.path.join(self._tmp, "runtime", "state", "liye_cards.txt")
        with open(path, encoding="utf-8") as f:
            return f.read().split()

    def test_multi_card_parse_and_dedupe(self):
        result = liye_sms.import_text(
            "GPT-TEST-0001-0002-0003\n"
            "CZ-TEST-0004-0005-0006, GOO-TEST-0007-0008-0009\n"
            "GPT-TEST-0001-0002-0003\n"  # 批内重复
            "# 注释行\n"
            "bad card!!!"
        )
        self.assertEqual(result["added"], 3)
        self.assertEqual(result["bad"], 2)
        self.assertEqual(self._txt(), [
            "GPT-TEST-0001-0002-0003", "CZ-TEST-0004-0005-0006", "GOO-TEST-0007-0008-0009",
        ])
        # 再导一遍：全部跳过，txt 不重复追加
        again = liye_sms.import_text("GPT-TEST-0001-0002-0003\nCZ-TEST-0004-0005-0006")
        self.assertEqual((again["added"], again["skipped"]), (0, 2))
        self.assertEqual(len(self._txt()), 3)


class RecoverAllTests(unittest.TestCase):
    """WebUI「检查恢复」：冷却/超租期占用卡按平台真实订单回退；租期内占用卡跳过。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_test_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()
        self.now = time.time()
        _write_state(self._tmp, [
            # 冷却卡：即使冷却未到也纳入检查
            {"code": "GPT-cool", "status": "cooldown", "cooldown_until": self.now + 600},
            # 租期已过的占用卡（疑似任务中断残留）
            {"code": "GPT-stale", "status": "in_use",
             "claimed_at": self.now - 9999, "order_id": "ord9"},
            # 租期内的占用卡（可能有任务正在用，必须跳过）
            {"code": "GPT-live", "status": "in_use", "claimed_at": self.now},
            {"code": "GPT-ok", "status": "available"},
        ])
        liye_sms._SESSIONS.clear()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()

    def _entry(self, code):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  encoding="utf-8") as f:
            state = json.load(f)
        return next(c for c in state["cards"] if c["code"] == code)

    def test_recover_free_and_skip_leased(self):
        def fake_api(code, method, path, body=None, service=None):
            if path == "/api/orders":
                orders = [] if code == "GPT-cool" else [
                    {"id": "ord9", "status": "waiting", "activationId": "act9",
                     "activationGeneration": 0}]
                return {"orders": orders}
            if path == "/api/orders/ord9/action":
                return {"order": {"id": "ord9", "status": "cancelled"}}
            raise AssertionError(f"unexpected {method} {path}")

        with patch.object(liye_sms, "_api_with_relogin", side_effect=fake_api):
            result = liye_sms.recover_all()
        self.assertEqual(result["checked"], 2)          # 冷却 + 超租期占用
        self.assertEqual(result["skipped_active"], 1)   # 租期内的占用卡
        self.assertEqual(result["recovered"], 2)
        self.assertEqual(self._entry("GPT-cool")["status"], "available")
        self.assertEqual(self._entry("GPT-stale")["status"], "available")
        self.assertEqual(self._entry("GPT-live")["status"], "in_use")  # 未被误动

    def test_recover_got_code_marks_exhausted(self):
        def fake_api(code, method, path, body=None, service=None):
            if path == "/api/orders" and code == "GPT-stale":
                return {"orders": [{"id": "ord8", "status": "completed",
                                    "smsCode": "123456"}]}
            if path == "/api/orders":
                return {"orders": []}
            raise AssertionError(f"unexpected {method} {path}")

        with patch.object(liye_sms, "_api_with_relogin", side_effect=fake_api):
            result = liye_sms.recover_all()
        self.assertEqual(result["exhausted"], 1)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(self._entry("GPT-stale")["status"], "exhausted")
        self.assertEqual(self._entry("GPT-cool")["status"], "available")

    def test_recover_cancel_rejected_extends_cooldown(self):
        def fake_api(code, method, path, body=None, service=None):
            if path == "/api/orders" and code == "GPT-stale":
                return {"orders": [{"id": "ord7", "status": "waiting",
                                    "activationId": "act7", "activationGeneration": 0}]}
            if path == "/api/orders":
                return {"orders": []}
            if path == "/api/orders/ord7/action":
                raise liye_sms.LiyeError("too early", code="CANCEL_TOO_EARLY")
            raise AssertionError(f"unexpected {method} {path}")

        with patch.object(liye_sms, "_api_with_relogin", side_effect=fake_api):
            result = liye_sms.recover_all()
        self.assertEqual(result["still_busy"], 1)
        entry = self._entry("GPT-stale")
        self.assertEqual(entry["status"], "cooldown")
        self.assertGreater(entry["cooldown_until"], time.time())

    def test_recover_network_error_keeps_status(self):
        def fake_api(code, method, path, body=None, service=None):
            raise liye_sms.LiyeError("network down", code="NETWORK_ERROR")

        with patch.object(liye_sms, "_api_with_relogin", side_effect=fake_api):
            result = liye_sms.recover_all()
        self.assertEqual(result["failed"], 2)
        self.assertEqual(self._entry("GPT-cool")["status"], "cooldown")
        self.assertEqual(self._entry("GPT-stale")["status"], "in_use")


class ChatgptChainTests(unittest.TestCase):
    """ChatGPT/Codex 链路：get_phone(provider=liye) → _liye_get_phone(chatai)。"""

    def test_liye_get_phone_claims_chatai_service(self):
        from common import sms as root_sms

        with patch.object(root_sms, "LIYE_MAX_CARDS_PER_CLAIM", 1), \
             patch("common.liye_sms.has_cards", return_value=True), \
             patch("common.liye_sms.claim", return_value=("15550001111", "", "liye_ord1")) as claim:
            phone, dial, pkey = root_sms._liye_get_phone()
        self.assertEqual((phone, dial, pkey), ("15550001111", "", "liye_ord1"))
        self.assertEqual(claim.call_args[1]["service"], "chatai")

    def test_get_phone_routes_explicit_liye_provider(self):
        from common import sms as root_sms

        with patch.object(root_sms, "_liye_get_phone", return_value=("15550001111", "", "liye_ord1")) as liye:
            result = root_sms.get_phone("2313", "dr", smsman_app="openai", provider="liye")
        self.assertEqual(result, ("15550001111", "", "liye_ord1"))
        liye.assert_called_once()

    def test_auto_order_includes_liye_when_cards_configured(self):
        from common import sms as root_sms

        with patch.object(root_sms, "SMS_TOKEN", "firefox-token"), \
             patch.object(root_sms, "SMSMAN_TOKEN", "smsman-token"), \
             patch.object(root_sms, "HERO_SMS_API_KEY", "hero-key"), \
             patch("common.liye_sms.has_cards", return_value=True):
            order = root_sms._auto_provider_order("2313", "openai", "dr")
        # 默认 last：liye 排在最后兜底
        self.assertEqual(order[-1], "liye")
        self.assertEqual(set(order), {"firefox", "smsman", "hero", "liye"})


if __name__ == "__main__":
    unittest.main()
