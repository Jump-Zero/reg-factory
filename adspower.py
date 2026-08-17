"""
AdsPower local API adapter.

The rest of this project expects a BitBrowser-like client:
create_browser/open_browser/close_browser/delete_browser/list_browsers and a
private _post("/browser/...") compatibility method. This adapter translates
that shape to AdsPower's local API.
"""

import os
import re
import sys
import time
from urllib.parse import urlparse

import requests

try:
    from config import ADSPOWER_API, ADSPOWER_API_KEY, ADSPOWER_GROUP_ID
except Exception:
    ADSPOWER_API = os.environ.get("ADSPOWER_API", "http://127.0.0.1:50325")
    ADSPOWER_API_KEY = os.environ.get("ADSPOWER_API_KEY", "")
    ADSPOWER_GROUP_ID = os.environ.get("ADSPOWER_GROUP_ID", "0")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass


_NETWORK_MARKERS = (
    "socket",
    "connection",
    "timed out",
    "timeout",
    "max retries",
    "remotedisconnected",
    "econnreset",
)


def _truthy_env(name, default="0"):
    return (os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _platform_domain(value):
    if not value:
        return ""
    raw = str(value).strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    return (parsed.netloc or parsed.path).strip("/")


class AdsPower:
    provider_name = "adspower"

    def __init__(self, api_base=None):
        self.api_base = (api_base or ADSPOWER_API or "http://127.0.0.1:50325").rstrip("/")
        self.api_key = ADSPOWER_API_KEY or os.environ.get("ADSPOWER_API_KEY", "")
        self.session = requests.Session()
        # Local browser APIs must not be sent through Clash/system proxy.
        self.session.trust_env = False

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method, path, params=None, data=None, timeout=120, _retries=5):
        url = path if str(path).startswith("http") else f"{self.api_base}{path}"
        last_exc = None
        for attempt in range(_retries):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params or None,
                    json=data if data is not None else None,
                    headers=self._headers(),
                    timeout=timeout,
                )
                resp.raise_for_status()
                result = resp.json()
                code = result.get("code")
                if code not in (0, "0", None):
                    raise Exception(f"AdsPower API error: {result.get('msg', result)}")
                return result
            except Exception as exc:
                msg = str(exc).lower()
                last_exc = exc
                if attempt < _retries - 1 and any(k in msg for k in _NETWORK_MARKERS):
                    time.sleep(2 + attempt)
                    continue
                raise
        if last_exc:
            raise last_exc

    def _get(self, path, params=None, **kwargs):
        return self._request("GET", path, params=params, **kwargs)

    def _api_post(self, path, data=None, **kwargs):
        return self._request("POST", path, data=data or {}, **kwargs)

    def _post(self, path, data=None, _retries=5):
        """BitBrowser endpoint compatibility used by older scripts."""
        data = data or {}
        if path == "/browser/list":
            return self.list_browsers(
                page=int(data.get("page", 0) or 0),
                page_size=int(data.get("pageSize", data.get("page_size", 100)) or 100),
            )
        if path == "/browser/open":
            return {"success": True, "data": self.open_browser(data.get("id") or data.get("user_id"))}
        if path == "/browser/close":
            return {"success": True, "data": self.close_browser(data.get("id") or data.get("user_id"))}
        if path == "/browser/delete":
            return {"success": True, "data": self.delete_browser(data.get("id") or data.get("user_id"))}
        if path == "/browser/update":
            profile_id = data.get("id") or data.get("browserId") or data.get("user_id")
            body = dict(data)
            if profile_id:
                self.update_browser(profile_id, **body)
                return {"success": True, "data": {"id": str(profile_id), "browserId": str(profile_id)}}
            name = body.pop("name", "reg_factory")
            new_id = self.create_browser(name=name, **body)
            return {"success": True, "data": {"id": new_id, "browserId": new_id}}
        raise NotImplementedError(f"AdsPower compatibility endpoint not supported: {path}")

    @staticmethod
    def _parse_proxy(proxy_str):
        if not proxy_str:
            return None
        proxy_type = "http"
        s = str(proxy_str).strip()
        for prefix in ("socks5://", "socks4://", "http://", "https://"):
            if s.lower().startswith(prefix):
                proxy_type = prefix.split("://", 1)[0]
                s = s[len(prefix):]
                break
        s = s.replace(",", "@", 1) if "@" not in s and "," in s else s
        match = re.match(r"^(.+):(.+)@(.+):(\d+)$", s)
        if match:
            return {
                "type": proxy_type,
                "username": match.group(1),
                "password": match.group(2),
                "host": match.group(3),
                "port": match.group(4),
            }
        match = re.match(r"^(.+):(\d+)$", s)
        if match:
            return {"type": proxy_type, "host": match.group(1), "port": match.group(2)}
        return None

    def _proxy_config(self, data):
        if data.get("user_proxy_config"):
            return data["user_proxy_config"]
        if data.get("proxyid"):
            return {"proxyid": data["proxyid"]}

        proxy_type = str(data.get("proxyType") or data.get("proxy_type") or "noproxy").lower()
        if proxy_type in {"noproxy", "no_proxy", "none", "direct"}:
            return {"proxy_soft": "no_proxy"}

        host = data.get("host") or data.get("proxyHost") or data.get("proxy_host")
        port = data.get("port") or data.get("proxyPort") or data.get("proxy_port")
        if not host or not port:
            return {"proxy_soft": "no_proxy"}

        return {
            "proxy_soft": data.get("proxy_soft") or "other",
            "proxy_type": proxy_type,
            "proxy_host": str(host),
            "proxy_port": str(port),
            "proxy_user": data.get("proxyUserName") or data.get("proxy_user") or "",
            "proxy_password": data.get("proxyPassword") or data.get("proxy_password") or "",
        }

    def _fingerprint_config(self, data):
        direct = dict(data.get("fingerprint_config") or {})
        fp = dict(data.get("browserFingerPrint") or {})
        version = os.environ.get("ADSPOWER_BROWSER_VERSION", "").strip() or "ua_auto"

        config = {
            "automatic_timezone": "1",
            "language_switch": "1",
            "page_language_switch": "1",
            "webrtc": "disabled",
            "canvas": "1",
            "webgl_image": "1",
            "webgl": "3",
            "audio": "1",
            "hardware_concurrency": str(fp.get("hardwareConcurrency") or 8),
            "device_memory": str(fp.get("deviceMemory") or 8),
            "browser_kernel_config": {"type": "chrome", "version": version},
        }

        if fp.get("isIpCreateTimeZone") is False:
            config["automatic_timezone"] = "0"
        if fp.get("isIpCreateLanguage") is False:
            config["language_switch"] = "0"
        if fp.get("isIpCreateDisplayLanguage") is False:
            config["page_language_switch"] = "0"
        if fp.get("language"):
            config["language"] = fp["language"]
            config["language_switch"] = "0"
        if fp.get("ua"):
            config["ua"] = fp["ua"]
        if fp.get("ostype"):
            config["device_name_switch"] = "1"

        config.update(direct)
        return config

    def _profile_payload(self, data, update=False):
        payload = {}
        for key in (
            "name",
            "remark",
            "username",
            "password",
            "cookie",
            "domain_name",
            "open_urls",
            "group_id",
        ):
            if data.get(key) not in (None, ""):
                payload[key] = data[key]

        if not update:
            payload.setdefault("group_id", str(data.get("group_id") or ADSPOWER_GROUP_ID or "0"))

        domain = _platform_domain(data.get("platform") or data.get("platformIcon"))
        if domain and "domain_name" not in payload:
            payload["domain_name"] = domain

        payload["user_proxy_config"] = self._proxy_config(data)
        payload["fingerprint_config"] = self._fingerprint_config(data)
        return payload

    def create_browser(self, name="claude_register", proxy_str=None, **kwargs):
        data = dict(kwargs)
        data["name"] = name
        data.setdefault("remark", "reg-factory auto profile")
        if proxy_str:
            parsed = self._parse_proxy(proxy_str)
            if parsed:
                data.setdefault("proxyType", parsed.get("type", "http"))
                data.setdefault("host", parsed["host"])
                data.setdefault("port", parsed["port"])
                if parsed.get("username"):
                    data.setdefault("proxyUserName", parsed["username"])
                if parsed.get("password"):
                    data.setdefault("proxyPassword", parsed["password"])
            else:
                data.setdefault("proxyType", "noproxy")

        result = self._api_post("/api/v1/user/create", self._profile_payload(data))
        payload = result.get("data") or {}
        profile_id = payload.get("id") or payload.get("user_id") or payload.get("profile_id")
        if not profile_id:
            raise RuntimeError(f"AdsPower create returned no id: {payload}")
        try:
            from common.browser_registry import register
            register(
                profile_id,
                name=name,
                provider=self.provider_name,
                api_base=self.api_base,
            )
        except Exception:
            pass
        print(f"  AdsPower profile created: {name} (ID: {profile_id})")
        return str(profile_id)

    def update_browser(self, profile_id, **kwargs):
        body = dict(kwargs)
        for key in ("id", "browserId", "user_id"):
            body.pop(key, None)
        payload = self._profile_payload(body, update=True)
        payload["user_id"] = str(profile_id)
        self._api_post("/api/v1/user/update", payload)
        return {"id": str(profile_id)}

    def list_browsers(self, page=0, page_size=100):
        params = {"page": int(page) + 1, "page_size": int(page_size)}
        result = self._get("/api/v1/user/list", params=params, timeout=30)
        data = result.get("data") or {}
        raw_list = data.get("list") or data.get("data") or []
        items = []
        for idx, item in enumerate(raw_list):
            mapped = dict(item)
            user_id = mapped.get("user_id") or mapped.get("id")
            if user_id is not None:
                mapped["id"] = str(user_id)
            mapped.setdefault("seq", mapped.get("serial_number", idx))
            items.append(mapped)
        total = data.get("total") or data.get("totalNum") or len(items)
        return {"success": True, "data": {"list": items, "totalNum": total}}

    def open_browser(self, profile_id):
        params = {
            "user_id": str(profile_id),
            "open_tabs": os.environ.get("ADSPOWER_OPEN_TABS", "1"),
            "ip_tab": os.environ.get("ADSPOWER_IP_TAB", "0"),
            "new_first_tab": os.environ.get("ADSPOWER_NEW_FIRST_TAB", "0"),
            "cdp_mask": os.environ.get("ADSPOWER_CDP_MASK", "1"),
        }
        if _truthy_env("ADSPOWER_HEADLESS"):
            params["headless"] = "1"
        result = self._get("/api/v1/browser/start", params=params, timeout=120)
        data = result.get("data") or {}
        ws_data = data.get("ws") or {}
        ws = ""
        if isinstance(ws_data, dict):
            ws = ws_data.get("puppeteer") or ws_data.get("playwright") or ws_data.get("selenium") or ""
        elif isinstance(ws_data, str):
            ws = ws_data
        if not ws:
            debug_port = data.get("debug_port") or data.get("debugPort")
            if debug_port:
                ws = f"http://127.0.0.1:{debug_port}"
        if ws and not ws.startswith(("ws://", "wss://", "http://", "https://")) and ":" in ws:
            ws = "http://" + ws
        if not ws:
            raise RuntimeError(f"AdsPower start returned no CDP endpoint: {data}")
        return {
            "ws": ws,
            "http": f"http://127.0.0.1:{data.get('debug_port')}" if data.get("debug_port") else "",
            "webdriver": data.get("webdriver") or data.get("webdriver_path") or "",
            "debug_port": data.get("debug_port") or data.get("debugPort"),
            "raw": data,
        }

    def close_browser(self, profile_id):
        return self._get("/api/v1/browser/stop", params={"user_id": str(profile_id)}, timeout=60)

    def delete_browser(self, profile_id):
        result = self._api_post("/api/v1/user/delete", {"user_ids": [str(profile_id)]}, timeout=60)
        try:
            from common.browser_registry import unregister
            unregister(profile_id)
        except Exception:
            pass
        print(f"  AdsPower profile deleted: {profile_id}")
        return result

    def cleanup_browsers(self, keep=0):
        result = self.list_browsers(page=0, page_size=200)
        browsers = result["data"]["list"]
        if not browsers:
            print("  no AdsPower profiles to clean")
            return 0
        browsers.sort(key=lambda b: b.get("seq", 0) or 0, reverse=True)
        to_delete = browsers[int(keep):]
        deleted = 0
        for item in to_delete:
            profile_id = item.get("id")
            if not profile_id:
                continue
            try:
                self.close_browser(profile_id)
            except Exception:
                pass
            time.sleep(1)
            try:
                self.delete_browser(profile_id)
                deleted += 1
            except Exception as exc:
                print(f"  AdsPower delete failed {item.get('name', '')}: {exc}")
        print(f"  AdsPower cleanup: deleted {deleted}/{len(to_delete)} profiles")
        return deleted

    def select_browser(self):
        result = self.list_browsers()
        browsers = result["data"]["list"]
        print("\nAvailable AdsPower profiles:")
        print("-" * 50)
        if browsers:
            for i, item in enumerate(browsers):
                print(f"  [{i}] #{item.get('seq', '')} {item.get('name', '')}  id={item.get('id', '')}")
        else:
            print("  (none)")
        print("  [n] create new profile")
        print("-" * 50)

        while True:
            max_idx = len(browsers) - 1 if browsers else -1
            hint = f"[0-{max_idx}/n]" if browsers else "[n]"
            choice = input(f"select {hint}: ").strip().lower()
            if choice == "n":
                name = input("profile name (blank = auto): ").strip() or f"reg_factory_{len(browsers) + 1}"
                return self.create_browser(name=name)
            if browsers and choice.isdigit() and 0 <= int(choice) < len(browsers):
                selected = browsers[int(choice)]
                print(f"selected: {selected.get('name', '')} (ID: {selected['id']})")
                return selected["id"]
            print("invalid selection")
