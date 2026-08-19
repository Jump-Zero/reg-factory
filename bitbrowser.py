"""
BitBrowser (比特浏览器) 本地 API 封装
API 基础地址: http://127.0.0.1:54345
"""

import sys
import time

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")

import os

import requests
from config import BITBROWSER_API, FINGERPRINT_BROWSER


def _selected_provider():
    return (
        os.environ.get("FINGERPRINT_BROWSER")
        or os.environ.get("BROWSER_PROVIDER")
        or FINGERPRINT_BROWSER
        or "bitbrowser"
    ).strip().lower()


def selected_browser_provider():
    """Return the live provider selection, including WebUI environment updates."""
    return _selected_provider()


def _use_adspower():
    return _selected_provider() in {"adspower", "ads_power", "ads"}


class BitBrowser:
    provider_name = "bitbrowser"

    def __new__(cls, api_base=None):
        if cls is BitBrowser and _selected_provider() in {
            "bundled", "embedded", "local", "custom", "chrome", "chromium",
        }:
            from common.bundled_browser import BundledBrowser
            return BundledBrowser(api_base=api_base)
        if cls is BitBrowser and _selected_provider() in {"custom_api", "api"}:
            from common.custom_browser_api import CustomBrowserAPI
            return CustomBrowserAPI(api_base=api_base)
        if cls is BitBrowser and _use_adspower():
            from adspower import AdsPower
            return AdsPower(api_base=api_base)
        return super().__new__(cls)

    def __init__(self, api_base=None):
        custom_api = os.environ.get("CUSTOM_BROWSER_API", "").strip()
        self.api_base = api_base or (custom_api if _selected_provider() in {"custom_api", "api"} else BITBROWSER_API)

    def _post(self, path, data=None, _retries=5):
        url = f"{self.api_base}{path}"
        last_exc = None
        for attempt in range(_retries):
            try:
                resp = requests.post(url, json=data or {}, timeout=30)
                resp.raise_for_status()
                result = resp.json()
                if not result.get("success"):
                    # 业务错误(如窗口数已满)：直接抛出，不重试
                    raise Exception(f"BitBrowser API 错误: {result.get('msg', '未知错误')}")
                return result
            except Exception as e:
                msg = str(e)
                # 仅对网络抖动重试(BitBrowser API 常见 TLS/socket 断连)
                if any(k in msg.lower() for k in [
                    "socket disconnected", "tls", "econnreset", "connection",
                    "timed out", "timeout", "max retries", "remotedisconnected",
                ]):
                    last_exc = e
                    if attempt < _retries - 1:
                        time.sleep(2 + attempt)  # 递增退避
                        continue
                raise
        if last_exc:
            raise last_exc

    def list_browsers(self, page=0, page_size=100):
        """获取浏览器窗口列表"""
        return self._post("/browser/list", {"page": page, "pageSize": page_size})

    def open_browser(self, profile_id):
        """
        打开浏览器窗口，返回 WebSocket 调试地址
        返回: {"ws": "ws://...", "http": "http://..."}
        """
        from common.traffic_saver import bitbrowser_open_payload

        payload = bitbrowser_open_payload(profile_id)
        try:
            result = self._post("/browser/open", payload)
        except Exception:
            if "args" not in payload:
                raise
            print("  BitBrowser rejected traffic-saving launch args; retrying normally")
            result = self._post("/browser/open", {"id": profile_id})
        return result["data"]

    def update_browser_fingerprint(self, profile_id, **fingerprint):
        """Partially update fingerprint fields without resetting the profile."""
        return self._post(
            "/browser/update/partial",
            {
                "ids": [profile_id],
                "browserFingerPrint": fingerprint,
            },
        )

    def close_browser(self, profile_id):
        """关闭浏览器窗口"""
        return self._post("/browser/close", {"id": profile_id})

    def delete_browser(self, profile_id):
        """删除浏览器窗口配置"""
        result = self._post("/browser/delete", {"id": profile_id})
        try:
            from common.browser_registry import unregister
            unregister(profile_id)
        except Exception:
            pass
        print(f"  窗口已删除: {profile_id}")
        return result

    def cleanup_browsers(self, keep=0):
        """删除所有浏览器窗口（释放配额）
        keep: 保留最新的 N 个窗口，0=全删"""
        result = self.list_browsers(page=0, page_size=200)
        browsers = result["data"]["list"]
        if not browsers:
            print("  无窗口需要清理")
            return 0
        # 按 seq 降序 = 最新的在前
        browsers.sort(key=lambda b: b.get("seq", 0), reverse=True)
        to_delete = browsers[keep:]
        deleted = 0
        for b in to_delete:
            try:
                self.close_browser(b["id"])
            except Exception:
                pass
            time.sleep(2)  # 等浏览器进程退出
            try:
                self.delete_browser(b["id"])
                deleted += 1
            except Exception as e:
                print(f"  删除失败 {b.get('name','')}: {e}")
        print(f"  清理完成: 删除 {deleted}/{len(to_delete)} 个窗口")
        return deleted

    def create_browser(self, name="claude_register", **kwargs):
        """
        创建新的浏览器窗口配置
        返回创建的窗口 ID
        """
        from common.traffic_saver import bitbrowser_profile_defaults

        data = {
            "name": name,
            "remark": "claude.ai 自动注册",
            "proxyMethod": 2,  # 自定义代理
            "proxyType": "noproxy",
            "browserFingerPrint": {
                "coreVersion": "130",
            },
            **bitbrowser_profile_defaults(),
            **kwargs,
        }
        result = self._post("/browser/update", data)
        profile_id = result["data"]["id"]
        try:
            from common.browser_registry import register
            register(
                profile_id,
                name=name,
                provider=self.provider_name,
                api_base=self.api_base,
            )
        except Exception as exc:
            print(f"  profile registry warning: {str(exc)[:120]}")
        print(f"  新窗口已创建: {name} (ID: {profile_id})")
        return profile_id

    def select_browser(self):
        """
        交互式选择一个浏览器窗口，或创建新窗口
        返回选中/创建的窗口 ID
        """
        result = self.list_browsers()
        browsers = result["data"]["list"]

        print("\n可用的浏览器窗口:")
        print("-" * 50)
        if browsers:
            for i, b in enumerate(browsers):
                seq = b.get("seq", "")
                name = b.get("name", "未命名")
                remark = b.get("remark", "")
                print(f"  [{i}] #{seq} {name}  {remark}")
        else:
            print("  (无)")
        print(f"  [n] 创建新窗口")
        print("-" * 50)

        while True:
            max_idx = len(browsers) - 1 if browsers else -1
            hint = f"[0-{max_idx}/n]" if browsers else "[n]"
            choice = input(f"请选择 {hint}: ").strip().lower()

            if choice == 'n':
                name = input("窗口名称 (留空自动): ").strip()
                if not name:
                    name = f"claude_{len(browsers) + 1}"
                profile_id = self.create_browser(name=name)
                return profile_id

            if browsers and choice.isdigit() and 0 <= int(choice) < len(browsers):
                selected = browsers[int(choice)]
                print(f"已选择: {selected.get('name', '')} (ID: {selected['id']})")
                return selected["id"]

            print("无效选择，请重新输入。")
