# -*- coding: utf-8 -*-
"""账号健康（401 处置）离线测试：401 实录判定、服务端刷新终判、扫描分类。"""
import sys
import unittest
from unittest.mock import MagicMock, patch

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from webui import health


def _item(email, status="active", error_message=""):
    return {
        "id": 1,
        "name": email,
        "email": email,
        "status": status,
        "error_message": error_message,
    }


ERR_401 = (
    'Authentication failed (401): {"error": {"message": '
    '"Encountered invalidated oauth token for ..."}}'
)


class Sub401EvidenceTests(unittest.TestCase):
    def test_detects_401_markers(self):
        self.assertTrue(health.sub2api_401_evidence(_item("a@x.com", error_message=ERR_401)))
        self.assertTrue(health.sub2api_401_evidence(
            _item("a@x.com", error_message="token refresh failed: status 401")))
        self.assertTrue(health.sub2api_401_evidence(
            _item("a@x.com", error_message="Encountered invalidated oauth token")))

    def test_clean_messages_are_not_401(self):
        self.assertFalse(health.sub2api_401_evidence(_item("a@x.com")))
        self.assertFalse(health.sub2api_401_evidence(_item("a@x.com", error_message="rate limited")))
        self.assertFalse(health.sub2api_401_evidence({}))
        self.assertFalse(health.sub2api_401_evidence(None))


class ServerRefreshKindTests(unittest.TestCase):
    def test_session_ended_is_rejected(self):
        with patch.object(health, "_sub2api_request",
                          side_effect=RuntimeError(
                              "token refresh failed: status 401, body: "
                              '{"error": {"message": "Your session has ended."}}')):
            ok, note, kind = health._sub2api_refresh_account("http://s", "t", {"id": 9})
        self.assertFalse(ok)
        self.assertEqual(kind, "rejected")
        self.assertIn("session has ended", note)

    def test_other_failure_is_unknown(self):
        with patch.object(health, "_sub2api_request",
                          side_effect=RuntimeError("connection refused")):
            ok, _note, kind = health._sub2api_refresh_account("http://s", "t", {"id": 9})
        self.assertFalse(ok)
        self.assertEqual(kind, "unknown")

    def test_transient_failure_is_retried_once(self):
        # 服务端出站抖动（EOF）后重试成功：未知失败自动重试一次。
        with patch.object(health, "_sub2api_request",
                          side_effect=[RuntimeError('Post "https://auth.openai.com/oauth/token": EOF'),
                                       {}]) as request:
            ok, _note, kind = health._sub2api_refresh_account("http://s", "t", {"id": 9})
        self.assertTrue(ok)
        self.assertEqual(kind, "")
        self.assertEqual(request.call_count, 2)

    def test_rejected_is_not_retried(self):
        with patch.object(health, "_sub2api_request",
                          side_effect=RuntimeError(
                              "token refresh failed: status 401, session has ended")) as request:
            ok, _note, kind = health._sub2api_refresh_account("http://s", "t", {"id": 9})
        self.assertFalse(ok)
        self.assertEqual(kind, "rejected")
        self.assertEqual(request.call_count, 1)

    def test_success_has_empty_kind(self):
        with patch.object(health, "_sub2api_request", return_value={}):
            ok, _note, kind = health._sub2api_refresh_account("http://s", "t", {"id": 9})
        self.assertTrue(ok)
        self.assertEqual(kind, "")


class ScanServerProbeTests(unittest.TestCase):
    """扫描校验一律走 SUB2API 服务端刷新（与实际使用同路径），严禁本地刷新。"""

    def _scan(self, items, local=None, server_refresh=None, imported=("a@x.com",),
              probe="suspects", platform="openai"):
        local = local if local is not None else {
            "a@x.com": {"path": "p", "data": {"refresh_token": "rt", "client_id": "cid"}},
        }
        with patch.object(health, "_origin", return_value="http://s"), \
             patch.object(health, "_sub2api_login", return_value="tok"), \
             patch.object(health, "_fetch_sub2api_items", return_value=items), \
             patch.object(health, "list_local_credentials", return_value=local), \
             patch.object(health, "list_grok_sso_credentials", return_value={}), \
             patch.object(health, "_effective_proxy", return_value=""), \
             patch.object(health, "_project_imported_emails", return_value=set(imported)), \
             patch.object(health, "_sub2api_refresh_account",
                          return_value=server_refresh or
                          (False, "SUB2API 侧刷新同样失败: status 401 session has ended", "rejected")), \
             patch.object(health, "update_scan_cache"), \
             patch.object(health.time, "sleep"):
            return health.scan_accounts(
                {"url": "http://s", "email": "e", "password": "p"},
                probe=probe, platform=platform,
            )

    def test_scan_never_refreshes_locally(self):
        # 回归铁律：本地刷新会轮换 token 只落本地文件，SUB2API 持有的旧 token
        # 立即作废——这正是「扫描显示正常、一到 SUB2API 使用就 401」的根因。
        with patch.object(health, "_refresh_oauth") as local_refresh:
            self._scan([_item("a@x.com", status="error", error_message=ERR_401)])
            self._scan([_item("a@x.com", status="active")], probe="all")
            self._scan([_item("a@x.com", status="active", error_message=ERR_401)],
                       probe="all", local={})
        local_refresh.assert_not_called()

    def test_suspect_with_server_recovery_is_sub2api_active(self):
        result = self._scan(
            [_item("a@x.com", status="error", error_message=ERR_401)],
            server_refresh=(True, "SUB2API 侧刷新成功", ""),
        )
        row = result["accounts"][0]
        self.assertEqual(row["category"], "sub2api_active")
        self.assertIn("服务端刷新成功", row["detail"])

    def test_suspect_server_rejected_with_local_copy_is_fixable(self):
        result = self._scan([_item("a@x.com", status="error", error_message=ERR_401)])
        row = result["accounts"][0]
        self.assertEqual(row["category"], "fixable")
        self.assertIn("session ended", row["detail"])
        self.assertIn("修复", row["detail"])

    def test_suspect_server_rejected_without_local_is_banned(self):
        result = self._scan(
            [_item("a@x.com", status="error", error_message=ERR_401)],
            local={},
        )
        row = result["accounts"][0]
        self.assertEqual(row["category"], "suspicious_banned")
        self.assertIn("session ended", row["detail"])

    def test_server_network_error_is_probe_error(self):
        result = self._scan(
            [_item("a@x.com", status="error", error_message=ERR_401)],
            server_refresh=(False, "SUB2API 侧刷新同样失败: timeout", "unknown"),
        )
        row = result["accounts"][0]
        self.assertEqual(row["category"], "probe_error")
        self.assertIn("网络异常", row["detail"])

    def test_active_status_with_401_evidence_is_probed(self):
        # status=active 但 error_message 已实录 401：默认 suspects 模式也必须校验。
        result = self._scan([_item("a@x.com", status="active", error_message=ERR_401)])
        row = result["accounts"][0]
        self.assertTrue(row["suspect"])
        # 已进入探测（默认 server_refresh=rejected + 本地有副本 → fixable）
        self.assertEqual(row["category"], "fixable")
        self.assertIn("session ended", row["detail"])

    def test_active_clean_account_is_ok_without_probe(self):
        server = MagicMock()
        result = self._scan([_item("a@x.com", status="active")], server_refresh=server)
        row = result["accounts"][0]
        self.assertFalse(row["suspect"])
        self.assertEqual(row["category"], "ok")
        server.assert_not_called()

    def test_probe_all_verifies_active_accounts(self):
        # 「真实校验全部」：active 账号也逐个服务端刷新，杜绝 status 滞后造成的假正常。
        result = self._scan(
            [_item("a@x.com", status="active")],
            server_refresh=(True, "SUB2API 侧刷新成功", ""),
            probe="all",
        )
        row = result["accounts"][0]
        self.assertEqual(row["category"], "ok")
        self.assertIn("真实可用", row["detail"])

    def test_probe_all_catches_actually_dead_active_account(self):
        # 用户核心场景：status=active 看似正常，实际 token 已死 → 真实校验揪出。
        result = self._scan(
            [_item("a@x.com", status="active")],
            local={},
            probe="all",
        )
        row = result["accounts"][0]
        self.assertEqual(row["category"], "suspicious_banned")
        self.assertIn("session ended", row["detail"])

    def test_external_accounts_are_excluded(self):
        result = self._scan(
            [_item("ext@x.com", status="error", error_message=ERR_401)],
            imported=("other@x.com",),
        )
        self.assertEqual(result["accounts"], [])
        self.assertEqual(result["excluded_not_imported"], 1)


class GrokScanRowTests(unittest.TestCase):
    """grok 行：服务端刷新真实校验；被拒时回退本地 sso 区分 reauth / 封禁。"""

    def _row(self, suspect=True):
        return {"category": "ok", "detail": "", "suspect": suspect, "local": False}

    def _run(self, row, local, server_refresh, sso_result=None):
        with patch.object(health, "_sub2api_refresh_account", return_value=server_refresh), \
             patch.object(health, "_probe_grok_sso",
                          return_value=sso_result or {"ok": True, "kind": ""}), \
             patch.object(health.time, "sleep"):
            health._scan_grok_row(row, {"id": 1}, local, True, "http://s", "t", "")

    def test_no_probe_keeps_passive(self):
        row = self._row(suspect=False)
        health._scan_grok_row(row, {"id": 1}, {"sso": "s"}, False, "http://s", "t", "")
        self.assertEqual(row["category"], "ok")
        self.assertTrue(row["local"])

    def test_server_success_recovers_suspect(self):
        row = self._row(suspect=True)
        self._run(row, None, (True, "SUB2API 侧刷新成功", ""))
        self.assertEqual(row["category"], "sub2api_active")

    def test_server_success_on_clean_account_is_ok(self):
        row = self._row(suspect=False)
        self._run(row, {"sso": "s"}, (True, "SUB2API 侧刷新成功", ""))
        self.assertEqual(row["category"], "ok")
        self.assertIn("真实可用", row["detail"])
        self.assertTrue(row["local"])

    def test_rejected_with_live_sso_is_reauth(self):
        row = self._row()
        self._run(row, {"sso": "s"},
                  (False, "status 401 session has ended", "rejected"),
                  sso_result={"ok": True})
        self.assertEqual(row["category"], "reauth")

    def test_rejected_with_dead_sso_is_banned(self):
        row = self._row()
        self._run(row, {"sso": "s"},
                  (False, "status 401 session has ended", "rejected"),
                  sso_result={"ok": False, "kind": "rejected", "error": "denied"})
        self.assertEqual(row["category"], "suspicious_banned")

    def test_rejected_without_sso_is_banned(self):
        row = self._row()
        self._run(row, None, (False, "status 401 session has ended", "rejected"))
        self.assertEqual(row["category"], "suspicious_banned")

    def test_network_error_is_probe_error(self):
        row = self._row()
        self._run(row, {"sso": "s"}, (False, "timeout", "unknown"))
        self.assertEqual(row["category"], "probe_error")


class Fix401Tests(unittest.TestCase):
    def _fix(self, item, refresh_result, server_refresh, proxy="http://p:1",
             refresh_mock=None):
        refresh_patch = patch.object(health, "_refresh_oauth",
                                     return_value=refresh_result)
        if refresh_mock is not None:
            refresh_patch = patch.object(health, "_refresh_oauth", refresh_mock)
        with patch.object(health, "_origin", return_value="http://s"), \
             patch.object(health, "_sub2api_login", return_value="tok"), \
             patch.object(health, "fetch_sub2api_accounts",
                          return_value=("http://s", "tok", [item])), \
             patch.object(health, "list_local_credentials", return_value={
                 "a@x.com": {"path": "p", "data": {"refresh_token": "rt", "client_id": "cid"}},
             }), \
             patch.object(health, "_effective_proxy", return_value=proxy), \
             refresh_patch, \
             patch.object(health, "_sub2api_refresh_account", return_value=server_refresh), \
             patch.object(health.time, "sleep"):
            return health.fix_accounts(
                {"url": "http://s", "email": "e", "password": "p"}, ["a@x.com"])

    def test_fix_refreshes_via_proxy(self):
        # 回归：fix 闭环本地刷新必须挂全局代理出站；
        # 本机直连 auth.openai.com 被 OpenAI 地区风控拦截（HTTP 403），
        # 曾导致批量修复 20 个账号全部失败。
        from unittest.mock import MagicMock

        mock = MagicMock(return_value={"ok": False, "kind": "http", "status": 403})
        self._fix(_item("a@x.com"), {"ok": False, "kind": "http", "status": 403},
                  (False, "net", "unknown"), refresh_mock=mock)
        self.assertEqual(mock.call_args.kwargs.get("proxy"), "http://p:1")

    def test_blocked_local_refresh_with_401_and_rejected_server_is_failed(self):
        result = self._fix(
            _item("a@x.com", status="error", error_message=ERR_401),
            {"ok": False, "kind": "http", "status": 403, "error": "region"},
            (False, "SUB2API 侧刷新同样失败: status 401 session has ended", "rejected"),
        )
        self.assertEqual(result["results"][0]["state"], "failed")
        self.assertIn("疑似封禁", result["results"][0]["detail"])

    def test_blocked_local_refresh_with_401_and_recovered_server_is_fixed(self):
        result = self._fix(
            _item("a@x.com", status="error", error_message=ERR_401),
            {"ok": False, "kind": "http", "status": 403, "error": "region"},
            (True, "SUB2API 侧刷新成功", ""),
        )
        self.assertEqual(result["results"][0]["state"], "fixed")
        self.assertIn("服务端刷新成功", result["results"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
