# -*- coding: utf-8 -*-
"""LIYE 卡密式接码离线测试：前缀服务识别、卡池按服务过滤、ChatGPT 链路路由。"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from common import liye_sms


def _write_state(root, cards, strict=False):
    os.makedirs(os.path.join(root, "runtime", "state"), exist_ok=True)
    with open(os.path.join(root, "runtime", "state", "liye_cards.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "cards": cards, "strict_selected": strict}, f)


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


class SelectionTests(unittest.TestCase):
    """卡密勾选：selected 卡优先取用；勾选卡全不可用回落全池原顺序。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_sel_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()

    def _state(self):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_pick_available_prefers_selected(self):
        state = {"cards": [
            {"code": "GPT-aaa", "status": "available"},
            {"code": "GPT-bbb", "status": "available", "selected": True},
        ]}
        # 勾选的卡即使排在后面也先被取用
        picked = liye_sms._pick_available(state, service="chatai")
        self.assertEqual(picked["code"], "GPT-bbb")

    def test_pick_available_falls_back_when_selected_unavailable(self):
        state = {"cards": [
            {"code": "GPT-aaa", "status": "available", "selected": True,
             "cooldown_until": 9999999999},
            {"code": "GPT-bbb", "status": "available"},
        ]}
        # 勾选卡冷却中：回落全池原顺序，不直接取号失败
        picked = liye_sms._pick_available(state, service="chatai")
        self.assertEqual(picked["code"], "GPT-bbb")

    def test_pick_available_selected_wrong_service_still_skipped(self):
        state = {"cards": [
            {"code": "GOO-bbb", "status": "available", "selected": True},
            {"code": "GPT-aaa", "status": "available"},
        ]}
        # 勾选的 Gmail 卡对 chatai 依旧不可见
        picked = liye_sms._pick_available(state, service="chatai")
        self.assertEqual(picked["code"], "GPT-aaa")

    def test_set_selection_writes_state_and_clears(self):
        _write_state(self._tmp, [
            {"code": "GPT-aaa", "status": "available", "selected": True},
            {"code": "GPT-bbb", "status": "available"},
        ])
        ok, _ = liye_sms.set_selection(["GPT-bbb"])
        self.assertTrue(ok)
        entries = {c["code"]: c for c in self._state()["cards"]}
        self.assertTrue(entries["GPT-bbb"]["selected"])
        self.assertFalse(entries["GPT-aaa"]["selected"])   # 不在列表 → 清除
        # 空列表 = 全部取消勾选
        ok, _ = liye_sms.set_selection([])
        self.assertTrue(ok)
        entries = {c["code"]: c for c in self._state()["cards"]}
        self.assertFalse(entries["GPT-bbb"]["selected"])

    def test_set_selection_rejects_unknown_code(self):
        _write_state(self._tmp, [{"code": "GPT-aaa", "status": "available"}])
        ok, msg = liye_sms.set_selection(["GPT-nope"])
        self.assertFalse(ok)
        self.assertIn("不在池中", msg)


class StrictSelectionTests(unittest.TestCase):
    """「仅用勾选卡密」开关：开启后取号只用勾选(selected)卡，不回落未勾选卡。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_strict_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()

    def _state(self):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_strict_picks_only_selected(self):
        state = {"cards": [
            {"code": "GPT-aaa", "status": "available"},
            {"code": "GPT-bbb", "status": "available", "selected": True},
        ], "strict_selected": True}
        self.assertEqual(liye_sms._pick_available(state, service="chatai")["code"],
                         "GPT-bbb")

    def test_strict_never_falls_back_to_unselected(self):
        state = {"cards": [
            {"code": "GPT-aaa", "status": "available", "selected": True,
             "cooldown_until": 9999999999},
            {"code": "GPT-bbb", "status": "available"},
        ], "strict_selected": True}
        # 勾选卡冷却中：严格模式不回落 GPT-bbb，直接取号失败
        self.assertIsNone(liye_sms._pick_available(state, service="chatai"))

    def test_strict_without_selection_fails_closed(self):
        state = {"cards": [{"code": "GPT-aaa", "status": "available"}],
                 "strict_selected": True}
        self.assertIsNone(liye_sms._pick_available(state, service="chatai"))

    def test_strict_selected_wrong_service_not_visible(self):
        state = {"cards": [
            {"code": "GOO-bbb", "status": "available", "selected": True},
            {"code": "GPT-aaa", "status": "available"},
        ], "strict_selected": True}
        # 勾选的 Gmail 卡对 chatai 依旧不可见，严格模式也不回落 GPT-aaa
        self.assertIsNone(liye_sms._pick_available(state, service="chatai"))

    def test_strict_off_keeps_pool_fallback(self):
        state = {"cards": [
            {"code": "GPT-aaa", "status": "available", "selected": True,
             "cooldown_until": 9999999999},
            {"code": "GPT-bbb", "status": "available"},
        ], "strict_selected": False}
        self.assertEqual(liye_sms._pick_available(state, service="chatai")["code"],
                         "GPT-bbb")

    def test_set_strict_selected_persists_and_summary_reports(self):
        _write_state(self._tmp, [{"code": "GPT-aaa", "status": "available"}])
        ok, message = liye_sms.set_strict_selected(True)
        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertTrue(self._state().get("strict_selected"))
        self.assertTrue(liye_sms.summary()["strict_selected"])
        ok, _ = liye_sms.set_strict_selected(False)
        self.assertTrue(ok)
        self.assertFalse(liye_sms.summary()["strict_selected"])

    def test_claim_strict_failure_message_mentions_mode(self):
        _write_state(self._tmp, [
            {"code": "GPT-aaa", "status": "available"},
            {"code": "GPT-sel", "status": "available", "selected": True,
             "cooldown_until": 9999999999},
        ], strict=True)
        with self.assertRaises(RuntimeError) as ctx:
            liye_sms.claim(max_cards=1)
        self.assertIn("仅用勾选卡密", str(ctx.exception))
        self.assertIn("不回落未勾选卡", str(ctx.exception))

    def test_claim_strict_no_selection_failure_message(self):
        _write_state(self._tmp, [{"code": "GPT-aaa", "status": "available"}],
                     strict=True)
        with self.assertRaises(RuntimeError) as ctx:
            liye_sms.claim(max_cards=1)
        self.assertIn("未勾选任何卡密", str(ctx.exception))

    def test_claim_strict_uses_selected_card(self):
        _write_state(self._tmp, [
            {"code": "GPT-aaa", "status": "available"},
            {"code": "GPT-bbb", "status": "available", "selected": True},
        ], strict=True)
        with patch.object(liye_sms, "_claim_one") as claim_one:
            claim_one.return_value = ("15550001111", "",
                                      {"id": "ord1", "phone": "15550001111",
                                       "activationId": "act1",
                                       "activationGeneration": 0,
                                       "status": "waiting"})
            phone, dial, pkey = liye_sms.claim(max_cards=1)
        self.assertEqual((phone, pkey), ("15550001111", "liye_ord1"))
        self.assertEqual(claim_one.call_args[0][0], "GPT-bbb")


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

    def test_claim_prefers_selected_card(self):
        _write_state(self._tmp, [
            {"code": "GPT-aaa", "status": "available"},
            {"code": "GPT-bbb", "status": "available", "selected": True},
        ])
        with patch.object(liye_sms, "_claim_one") as claim_one:
            claim_one.return_value = ("15550001111", "", self._order())
            liye_sms.claim(max_cards=1)
        # 勾选的 GPT-bbb 优先于排在前面的 GPT-aaa
        self.assertEqual(claim_one.call_args[0][0], "GPT-bbb")

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


class ClaimRetryTests(unittest.TestCase):
    """分配超时→退出卡密→重新登录取号循环；「尝试次数过多」→ 卡密冷却。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_retry_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()
        _write_state(self._tmp, [{"code": "GPT-aaa", "status": "available"}])
        liye_sms._SESSIONS.clear()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()
        liye_sms._SESSIONS.clear()

    @staticmethod
    def _order(order_id="ord1", phone=""):
        return {"id": order_id, "phone": phone, "activationId": "act1",
                "activationGeneration": 0, "status": "waiting"}

    def _entry(self):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  encoding="utf-8") as f:
            return json.load(f)["cards"][0]

    def _api_mock(self, post_orders):
        """GET /api/orders → 无在途订单；POST /api/orders 依次返回 post_orders。"""
        created = {"n": 0}

        def fake(code, method, path, body=None, service=None):
            if path == "/api/orders" and method == "POST":
                order = post_orders[min(created["n"], len(post_orders) - 1)]
                created["n"] += 1
                return {"order": order}
            return {"orders": []}

        return fake, created

    def test_timeout_reenters_card_until_number(self):
        """第 1 轮超时 → 退出卡密重新登录，第 2 轮重建单拿到号码。"""
        o1, o2 = self._order("ord1"), self._order("ord2", "15550001111")
        fake, created = self._api_mock([o1, o2])
        with patch.object(liye_sms, "LIYE_ALLOC_ROUNDS", 3), \
             patch.object(liye_sms, "_login", return_value=object()), \
             patch.object(liye_sms, "_api_with_relogin", side_effect=fake), \
             patch.object(liye_sms, "_wait_phone", side_effect=[o1, o2]), \
             patch.object(liye_sms, "_exit_order") as exit_order, \
             patch.object(liye_sms, "_drop_session", wraps=liye_sms._drop_session) as drop:
            phone, dial, pkey = liye_sms.claim(max_cards=1)
        self.assertEqual((phone, pkey), ("15550001111", "liye_ord2"))
        self.assertEqual(created["n"], 2)   # 超时后重新建单
        exit_order.assert_called_once()     # 退出卡密（取消订单退回次数）
        drop.assert_called_once()           # 再次输入卡密（重登）

    def test_rate_limit_raises_card_rate_limited(self):
        """平台返回「尝试次数过多，请稍后再试」→ 转换为 CARD_RATE_LIMITED。"""
        def limited(code, method, path, body=None, service=None):
            if path == "/api/orders" and method == "POST":
                raise liye_sms.LiyeError("尝试次数过多，请稍后再试", code="TOO_MANY")
            return {"orders": []}

        with patch.object(liye_sms, "LIYE_ALLOC_ROUNDS", 3), \
             patch.object(liye_sms, "_login", return_value=object()), \
             patch.object(liye_sms, "_api_with_relogin", side_effect=limited):
            with self.assertRaises(liye_sms.LiyeError) as ctx:
                liye_sms._claim_one("GPT-aaa", 1, service="chatai")
        self.assertEqual(ctx.exception.code, "CARD_RATE_LIMITED")

    def test_rate_limit_marks_card_cooldown_via_claim(self):
        """claim 全链路：限频后报错透出，卡密置 cooldown 600s。"""
        def limited(code, method, path, body=None, service=None):
            if path == "/api/orders" and method == "POST":
                raise liye_sms.LiyeError("尝试次数过多，请稍后再试", code="TOO_MANY")
            return {"orders": []}

        with patch.object(liye_sms, "LIYE_ALLOC_ROUNDS", 3), \
             patch.object(liye_sms, "_login", return_value=object()), \
             patch.object(liye_sms, "_api_with_relogin", side_effect=limited):
            with self.assertRaises(RuntimeError) as ctx:
                liye_sms.claim(max_cards=1)
        self.assertIn("尝试次数过多", str(ctx.exception))
        entry = self._entry()
        self.assertEqual(entry["status"], "cooldown")
        self.assertGreater(entry["cooldown_until"], time.time() + 500)

    def test_rounds_exhausted_falls_back_no_numbers(self):
        """到达轮数上限仍未限频：NO_NUMBERS 报错，重试 N 轮，卡回 available+60s。"""
        fake, created = self._api_mock([self._order("ordX")])
        with patch.object(liye_sms, "LIYE_ALLOC_ROUNDS", 3), \
             patch.object(liye_sms, "_login", return_value=object()), \
             patch.object(liye_sms, "_api_with_relogin", side_effect=fake), \
             patch.object(liye_sms, "_wait_phone", return_value=None), \
             patch.object(liye_sms, "_exit_order"):
            with self.assertRaises(RuntimeError) as ctx:
                liye_sms.claim(max_cards=1)
        self.assertIn("未分配号码", str(ctx.exception))
        self.assertEqual(created["n"], 3)   # 每轮重建单
        entry = self._entry()
        self.assertEqual(entry["status"], "available")
        self.assertGreater(entry["cooldown_until"], time.time() + 30)

    def test_queued_order_cancel_rejected_still_exits_and_retries(self):
        """订单排队中平台不允许取消(ORDER_NOT_CANCELLABLE)：
        保留订单，仍退出卡密重新登录取号，循环继续直至拿到号码。"""
        o1 = self._order("ord1")
        o1["status"] = "queued"
        o2 = self._order("ord2", "15550002222")
        created = {"n": 0}

        def fake(code, method, path, body=None, service=None):
            if path == "/api/orders" and method == "POST":
                created["n"] += 1
                return {"order": o1 if created["n"] == 1 else o2}
            if path.endswith("/action"):
                # 排队中平台拒绝取消
                raise liye_sms.LiyeError("order queued, cannot cancel",
                                         code="ORDER_NOT_CANCELLABLE")
            return {"orders": []}

        with patch.object(liye_sms, "LIYE_ALLOC_ROUNDS", 3), \
             patch.object(liye_sms, "_login", return_value=object()), \
             patch.object(liye_sms, "_api_with_relogin", side_effect=fake), \
             patch.object(liye_sms, "_wait_phone", side_effect=[o1, o2]), \
             patch.object(liye_sms, "_drop_session", wraps=liye_sms._drop_session) as drop:
            phone, dial, pkey = liye_sms.claim(max_cards=1)
        self.assertEqual((phone, pkey), ("15550002222", "liye_ord2"))
        self.assertEqual(created["n"], 2)   # 取消被拒后仍重新建单取号
        drop.assert_called_once()           # 仍退出卡密（重登）再取

    def test_wait_phone_timeout_returns_snapshot(self):
        """等待超时未分号：返回订单快照供退出卡密取消，而非 None。"""
        order = self._order("ordQ")
        order["status"] = "queued"

        def fake_api(session, method, path, body=None, timeout=30):
            return {"order": order}

        with patch.object(liye_sms, "_api", side_effect=fake_api), \
             patch.object(liye_sms.time, "sleep"):
            cur = liye_sms._wait_phone(object(), order, 0.05)
        self.assertEqual(cur["id"], "ordQ")

    def test_wait_phone_terminal_returns_snapshot(self):
        """订单进入终态(取消/失败)：直接返回终态快照，不再轮询。"""
        order = self._order("ordC")
        order["status"] = "cancelled"
        with patch.object(liye_sms, "_api") as api_mock:
            cur = liye_sms._wait_phone(object(), order, 0.05)
        self.assertEqual(cur["status"], "cancelled")
        api_mock.assert_not_called()


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


class RemoveCardTests(unittest.TestCase):
    """WebUI 删除卡密：状态文件 + txt 导入文件一并移除；占用中/不存在拒绝。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_test_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()
        self._cards = patch.object(liye_sms, "LIYE_CARDS", "")
        self._cards.start()
        liye_sms._SESSIONS.clear()

    def tearDown(self):
        self._cards.stop()
        self._env.stop()

    def _state(self):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def _txt(self):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.txt"),
                  encoding="utf-8") as f:
            return f.read().split()

    def test_remove_deletes_state_and_txt(self):
        liye_sms.import_text("GPT-TEST-0001-0002-0003\nCZ-TEST-0004-0005-0006")
        ok, message = liye_sms.remove_card("GPT-TEST-0001-0002-0003")
        self.assertTrue(ok)
        self.assertEqual(message, "")
        codes = [c["code"] for c in self._state()["cards"]]
        self.assertEqual(codes, ["CZ-TEST-0004-0005-0006"])   # 状态已移除
        self.assertEqual(self._txt(), ["CZ-TEST-0004-0005-0006"])  # txt 同步移除，sync 不复活
        self.assertEqual(liye_sms.summary()["total"], 1)

    def test_summary_cards_carry_full_code(self):
        liye_sms.import_text("GPT-TEST-0001-0002-0003")
        cards = liye_sms.summary()["cards"]
        self.assertEqual(cards[0]["full_code"], "GPT-TEST-0001-0002-0003")
        self.assertEqual(cards[0]["code"], "GPT-TE...0003")   # 展示仍脱敏

    def test_remove_rejects_in_use_card(self):
        _write_state(self._tmp, [{"code": "GPT-live", "status": "in_use",
                                  "claimed_at": time.time()}])
        ok, message = liye_sms.remove_card("GPT-live")
        self.assertFalse(ok)
        self.assertIn("使用", message)
        self.assertEqual(self._state()["cards"][0]["code"], "GPT-live")  # 未被误删

    def test_remove_missing_card_returns_false(self):
        liye_sms.import_text("GPT-TEST-0001-0002-0003")
        ok, message = liye_sms.remove_card("GPT-NOPE-0000-0000-0000")
        self.assertFalse(ok)
        self.assertEqual(message, "卡密不存在")

    def test_remove_rejects_env_configured_card(self):
        with patch.object(liye_sms, "LIYE_CARDS", "GPT-ENV-0001-0002-0003"):
            ok, message = liye_sms.remove_card("GPT-ENV-0001-0002-0003")
        self.assertFalse(ok)
        self.assertIn("env", message)


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

    def test_recover_uncancellable_marks_long_cooldown(self):
        def fake_api(code, method, path, body=None, service=None):
            if path == "/api/orders" and code == "GPT-stale":
                return {"orders": [{"id": "ord6", "status": "queued",
                                    "activationId": "act6", "activationGeneration": 0}]}
            if path == "/api/orders":
                return {"orders": []}
            if path == "/api/orders/ord6/action":
                raise liye_sms.LiyeError("只有等待验证码的订单可以取消",
                                         code="ORDER_NOT_CANCELLABLE")
            raise AssertionError(f"unexpected {method} {path}")

        with patch.object(liye_sms, "_api_with_relogin", side_effect=fake_api):
            result = liye_sms.recover_all()
        self.assertEqual(result["uncancellable"], 1)
        self.assertEqual(result["still_busy"], 0)
        self.assertEqual(result["reasons"], {"只有等待验证码的订单可以取消": 1})
        entry = self._entry("GPT-stale")
        self.assertEqual(entry["status"], "cooldown")
        # 平台不允许取消：拉长到约 30 分钟，避免每 10 分钟空轮询
        self.assertGreater(entry["cooldown_until"], time.time() + 1500)
        self.assertEqual(self._entry("GPT-cool")["status"], "available")

    def test_recover_uncancellable_aggregates_reasons(self):
        def fake_api(code, method, path, body=None, service=None):
            oid = "ord5" if code == "GPT-stale" else "ord4"
            if path == "/api/orders":
                return {"orders": [{"id": oid, "status": "queued",
                                    "activationId": f"act-{oid}",
                                    "activationGeneration": 0}]}
            if path == f"/api/orders/{oid}/action":
                raise liye_sms.LiyeError("只有等待验证码的订单可以取消",
                                         code="ORDER_NOT_CANCELLABLE")
            raise AssertionError(f"unexpected {method} {path}")

        with patch.object(liye_sms, "_api_with_relogin", side_effect=fake_api):
            result = liye_sms.recover_all()
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["uncancellable"], 2)
        self.assertEqual(result["reasons"], {"只有等待验证码的订单可以取消": 2})
        self.assertGreater(self._entry("GPT-cool")["cooldown_until"],
                           time.time() + 1500)
        self.assertGreater(self._entry("GPT-stale")["cooldown_until"],
                           time.time() + 1500)

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

        # 轮换游标是模块级全局：同进程先跑过其它 sms 测试会推进它，
        # 使 liye 不在末位；这里重置保证断言的是「默认 last」排列。
        root_sms._AUTO_PROVIDER_CURSOR = 0
        with patch.object(root_sms, "SMS_TOKEN", "firefox-token"), \
             patch.object(root_sms, "SMSMAN_TOKEN", "smsman-token"), \
             patch.object(root_sms, "HERO_SMS_API_KEY", "hero-key"), \
             patch("common.liye_sms.has_cards", return_value=True):
            order = root_sms._auto_provider_order("2313", "openai", "dr")
        # 默认 last：liye 排在最后兜底
        self.assertEqual(order[-1], "liye")
        self.assertEqual(set(order), {"firefox", "smsman", "hero", "liye"})


class SessionExitTests(unittest.TestCase):
    """退出卡密：先调平台 POST /api/card/logout（服务端结束会话），再丢本地 Session。"""

    def test_drop_session_logs_out_on_platform(self):
        sess = MagicMock()
        liye_sms._SESSIONS["GPT-aaa|chatai"] = sess
        try:
            liye_sms._drop_session("GPT-aaa")
        finally:
            liye_sms._SESSIONS.clear()
        sess.post.assert_called_once()
        args, _kwargs = sess.post.call_args
        self.assertIn("/api/card/logout", str(args[0]))

    def test_drop_session_logout_failure_still_discards(self):
        sess = MagicMock()
        sess.post.side_effect = RuntimeError("network down")
        liye_sms._SESSIONS["GPT-aaa|chatai"] = sess
        try:
            liye_sms._drop_session("GPT-aaa")  # 不应抛错
        finally:
            liye_sms._SESSIONS.clear()
        self.assertNotIn("GPT-aaa|chatai", liye_sms._SESSIONS)

    def test_drop_session_without_local_session_is_noop(self):
        liye_sms._drop_session("GPT-notloaded")  # 不应抛错


class OrderStateTests(unittest.TestCase):
    """订单-卡密状态：order_consumed 判断与 _transition 的 exhausted 保护。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="liye_ord_")
        self._env = patch.dict(os.environ, {"REG_FACTORY_DATA_DIR": self._tmp})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _write_state(self, cards):
        os.makedirs(os.path.join(self._tmp, "runtime", "state"), exist_ok=True)
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"version": 1, "cards": cards, "strict_selected": False}, f)

    def _read_state(self):
        with open(os.path.join(self._tmp, "runtime", "state", "liye_cards.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_order_consumed_true_for_exhausted_card(self):
        self._write_state([{"code": "GPT-aaa", "status": "exhausted", "order_id": "77"}])
        self.assertTrue(liye_sms.order_consumed("liye_77"))

    def test_order_consumed_false_for_in_use_card(self):
        self._write_state([{"code": "GPT-aaa", "status": "in_use", "order_id": "77"}])
        self.assertFalse(liye_sms.order_consumed("liye_77"))

    def test_order_consumed_false_for_unknown_order(self):
        self._write_state([{"code": "GPT-aaa", "status": "available"}])
        self.assertFalse(liye_sms.order_consumed("liye_404"))

    def test_transition_does_not_resurrect_exhausted_card(self):
        # 已收码(一卡一次已消耗)：取消失败的冷却不能把 exhausted 拉回 cooldown
        self._write_state([{"code": "GPT-aaa", "status": "exhausted", "order_id": "77"}])
        liye_sms._transition("77", "cooldown", cooldown_until=9999999999)
        self.assertEqual(self._read_state()["cards"][0]["status"], "exhausted")

    def test_transition_still_releases_active_card(self):
        self._write_state([{"code": "GPT-aaa", "status": "in_use", "order_id": "77"}])
        liye_sms._transition("77", "available")
        card = self._read_state()["cards"][0]
        self.assertEqual(card["status"], "available")
        self.assertEqual(card["order_id"], "")


if __name__ == "__main__":
    unittest.main()
