"""RoxyBrowser API adapter compatible with the BitBrowser lifecycle."""

from __future__ import annotations

import json
import random
import time
from urllib.parse import unquote, urljoin, urlparse

import requests

from config import *  # noqa: F403 - these are environment-backed settings


def _value(payload, keys):
    if isinstance(payload, dict):
        for key in keys:
            item = payload.get(key)
            if item not in (None, ""):
                return item
        for child in payload.values():
            found = _value(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _value(child, keys)
            if found not in (None, ""):
                return found
    return None


def _proxy_info(options: dict) -> dict | None:
    try:
        from common.direct_proxy import parse_proxy

        raw = options.get("proxy_str") or options.get("proxyUrl") or options.get("proxy")
        if not raw:
            from common import proxy_switch

            raw = proxy_switch.effective_proxy_url()
        spec = parse_proxy(raw)
        if not spec:
            return None
        protocol = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5", "socks5h": "SOCKS5"}.get(spec.scheme)
        if not protocol:
            raise ValueError(f"Roxy 不支持代理协议: {spec.scheme}")
        value = {
            "moduleId": 0,
            "proxyMethod": "custom",
            "proxyCategory": protocol,
            "ipType": "IPV4",
            "protocol": protocol,
            "host": spec.host,
            "port": str(spec.port),
        }
        if spec.username:
            value["proxyUserName"] = spec.username
        if spec.password:
            value["proxyPassword"] = spec.password
        return value
    except Exception:
        return None


class RoxyBrowser:
    provider_name = "roxy"

    def __init__(self, api_base=None):
        self.api_base = str(api_base or ROXY_API_BASE).rstrip("/")  # noqa: F405
        self.session = requests.Session()
        self.session.trust_env = False
        if str(ROXY_API_TOKEN).strip():  # noqa: F405
            token = str(ROXY_API_TOKEN).strip()  # noqa: F405
            self.session.headers.update({"token": token, "Authorization": f"Bearer {token}"})
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method, path, body=None, retries=None):
        url = urljoin(self.api_base + "/", str(path).lstrip("/"))
        attempts = max(1, int(retries if retries is not None else ROXY_API_RETRIES))  # noqa: F405
        last = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    str(method or "POST").upper(), url,
                    params=body if str(method).upper() == "GET" else None,
                    json=body if str(method).upper() != "GET" else None,
                    timeout=max(5, int(ROXY_API_TIMEOUT)),  # noqa: F405
                )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"data": response.text}
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(payload.get("message") or payload.get("msg") or "Roxy API rejected request")
                if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, "0", "200", True):
                    raise RuntimeError(payload.get("message") or payload.get("msg") or f"Roxy API code {payload.get('code')}")
                return payload
            except Exception as exc:
                last = exc
                text = str(exc).lower()
                retryable = any(marker in text for marker in ("timeout", "timed out", "connection", "502", "503", "504", "429"))
                if attempt + 1 >= attempts or not retryable:
                    raise
                time.sleep(min(1.5 * (attempt + 1), 5))
        raise RuntimeError(str(last or "Roxy API request failed"))

    def create_browser(self, name="reg_factory", **kwargs):
        if not ROXY_ONE_PROFILE_PER_ACCOUNT and str(ROXY_PROFILE_ID).strip():  # noqa: F405
            profile_id = str(ROXY_PROFILE_ID).strip()  # noqa: F405
            try:
                from common.browser_registry import register

                register(profile_id, name=name, provider="roxy", api_base=self.api_base)
            except Exception:
                pass
            print(f"  RoxyBrowser using configured profile: {profile_id}")
            return profile_id
        body = {"name": name, "os": ROXY_DEFAULT_OS}  # noqa: F405
        if ROXY_RANDOM_OS_ON_CREATE:  # noqa: F405
            choices = [item.strip() for item in str(ROXY_RANDOM_OS_CHOICES).replace("\n", ",").split(",") if item.strip()]  # noqa: F405
            body["os"] = random.choice(choices or ["Windows", "macOS"])
        if str(ROXY_WORKSPACE_ID).strip():  # noqa: F405
            body["workspaceId"] = int(ROXY_WORKSPACE_ID) if str(ROXY_WORKSPACE_ID).isdigit() else str(ROXY_WORKSPACE_ID)  # noqa: F405
        if str(ROXY_PROJECT_ID).strip():  # noqa: F405
            body["projectId"] = int(ROXY_PROJECT_ID) if str(ROXY_PROJECT_ID).isdigit() else str(ROXY_PROJECT_ID)  # noqa: F405
        if ROXY_CREATE_USE_PROXY_POOL:  # noqa: F405
            proxy = _proxy_info(kwargs)
            if proxy:
                body["proxyInfo"] = proxy
        body.update({
            key: value for key, value in kwargs.items()
            if key not in {
                "browserFingerPrint", "proxy_str", "proxyMethod", "proxyType",
                "host", "port", "proxyUserName", "proxyPassword", "proxy",
            }
        })
        if not body.get("workspaceId"):
            raise RuntimeError("RoxyBrowser 缺少 ROXY_WORKSPACE_ID，请在环境配置中填写工作区 ID")
        result = self._request(ROXY_CREATE_METHOD, ROXY_CREATE_PATH, body, retries=1)  # noqa: F405
        profile_id = _value(result, ("id", "dirId", "dir_id", "profile_id", "profileId", "browser_id"))
        if profile_id in (None, ""):
            raise RuntimeError(f"Roxy 创建环境未返回 profile ID: {json.dumps(result, ensure_ascii=False)[:500]}")
        profile_id = str(profile_id)
        try:
            from common.browser_registry import register

            register(profile_id, name=name, provider="roxy", api_base=self.api_base)
        except Exception:
            pass
        print(f"  RoxyBrowser profile created: {name} ({profile_id})")
        return profile_id

    def open_browser(self, profile_id):
        body = {
            "workspaceId": int(ROXY_WORKSPACE_ID) if str(ROXY_WORKSPACE_ID).isdigit() else str(ROXY_WORKSPACE_ID),  # noqa: F405
            "dirId": int(profile_id) if str(profile_id).isdigit() else str(profile_id),
            "args": [], "forceOpen": True, "headless": bool(ROXY_OPEN_HEADLESS),  # noqa: F405
        }
        result = self._request(ROXY_OPEN_METHOD, str(ROXY_OPEN_PATH).format(profile_id=profile_id), body)  # noqa: F405
        endpoint = _value(result, ("ws", "wsEndpoint", "ws_endpoint", "debuggerWsUrl", "debuggerAddress", "debugger_address", "debugAddress", "http", "webdriver", "selenium"))
        if not endpoint:
            raise RuntimeError(f"Roxy 打开环境未返回 CDP/调试地址: {json.dumps(result, ensure_ascii=False)[:500]}")
        endpoint = str(endpoint)
        if endpoint.isdigit():
            endpoint = f"http://127.0.0.1:{endpoint}"
        elif endpoint.startswith(":"):
            endpoint = f"http://127.0.0.1{endpoint}"
        elif not endpoint.startswith(("ws://", "wss://", "http://", "https://")):
            endpoint = f"http://{endpoint}"
        return {"ws": endpoint, "http": endpoint}

    def close_browser(self, profile_id):
        body = {"workspaceId": ROXY_WORKSPACE_ID, "dirId": int(profile_id) if str(profile_id).isdigit() else str(profile_id)}  # noqa: F405
        return self._request(ROXY_CLOSE_METHOD, str(ROXY_CLOSE_PATH).format(profile_id=profile_id), body)  # noqa: F405

    def delete_browser(self, profile_id):
        body = {"workspaceId": ROXY_WORKSPACE_ID, "dirIds": [int(profile_id) if str(profile_id).isdigit() else str(profile_id)]}  # noqa: F405
        result = self._request(ROXY_DELETE_METHOD, str(ROXY_DELETE_PATH).format(profile_id=profile_id), body)  # noqa: F405
        try:
            from common.browser_registry import unregister

            unregister(profile_id)
        except Exception:
            pass
        return result
