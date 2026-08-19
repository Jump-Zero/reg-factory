"""Configurable adapter for third-party fingerprint browser APIs.

The adapter keeps the BitBrowser-compatible contract used by the registration
flows, while also accepting a small, conventional REST contract.  A provider
only needs a base URL, optional auth header, and (when necessary) endpoint
paths in the environment configuration.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

import requests


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool = True) -> bool:
    value = _env(name, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


class CustomBrowserAPI:
    """Expose the BitBrowser-like methods expected by ``common.browser``."""

    provider_name = "custom_api"

    def __init__(self, api_base: str | None = None):
        self.api_base = (api_base or _env("CUSTOM_BROWSER_API", "")).strip().rstrip("/")
        if not self.api_base:
            raise RuntimeError("CUSTOM_BROWSER_API is not configured")
        self.mode = _env("CUSTOM_BROWSER_API_MODE", "auto").strip().lower() or "auto"
        self.timeout = _int("CUSTOM_BROWSER_API_TIMEOUT", 60)
        self.verify_tls = _bool("CUSTOM_BROWSER_API_VERIFY_TLS", True)
        self.id_field = _env("CUSTOM_BROWSER_API_ID_FIELD", "id").strip() or "id"
        self.paths = {
            "create": _env("CUSTOM_BROWSER_API_CREATE_PATH", "/browser/update"),
            "list": _env("CUSTOM_BROWSER_API_LIST_PATH", "/browser/list"),
            "open": _env("CUSTOM_BROWSER_API_OPEN_PATH", "/browser/open"),
            "close": _env("CUSTOM_BROWSER_API_CLOSE_PATH", "/browser/close"),
            "delete": _env("CUSTOM_BROWSER_API_DELETE_PATH", "/browser/delete"),
            "update": _env("CUSTOM_BROWSER_API_UPDATE_PATH", "/browser/update/partial"),
        }
        self.methods = {
            "create": _env("CUSTOM_BROWSER_API_CREATE_METHOD", "POST").upper(),
            "list": _env("CUSTOM_BROWSER_API_LIST_METHOD", "POST").upper(),
            "open": _env("CUSTOM_BROWSER_API_OPEN_METHOD", "POST").upper(),
            "close": _env("CUSTOM_BROWSER_API_CLOSE_METHOD", "POST").upper(),
            "delete": _env("CUSTOM_BROWSER_API_DELETE_METHOD", "POST").upper(),
            "update": _env("CUSTOM_BROWSER_API_UPDATE_METHOD", "POST").upper(),
        }
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def _legacy(self) -> bool:
        # auto is deliberately backward compatible.  Set mode=generic for a
        # conventional REST API; all paths and methods remain configurable.
        return self.mode in {"auto", "bitbrowser", "legacy", "compat"}

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        extra = _env("CUSTOM_BROWSER_API_HEADERS", "").strip()
        if extra:
            try:
                value = json.loads(extra)
                if not isinstance(value, dict):
                    raise ValueError("headers must be a JSON object")
                headers.update({str(key): str(item) for key, item in value.items()})
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"CUSTOM_BROWSER_API_HEADERS is invalid JSON: {exc}") from exc
        key = (_env("CUSTOM_BROWSER_API_KEY", "") or _env("CUSTOM_BROWSER_API_TOKEN", "")).strip()
        if key:
            name = _env("CUSTOM_BROWSER_API_AUTH_HEADER", "Authorization").strip() or "Authorization"
            prefix = _env("CUSTOM_BROWSER_API_AUTH_PREFIX", "Bearer ")
            if prefix.strip().lower() in {"bearer", "token", "basic", "apikey", "api-key"}:
                prefix = prefix.strip() + " "
            headers[name] = f"{prefix}{key}"
        return headers

    @staticmethod
    def _render_path(path: str, profile_id: str | None = None) -> str:
        path = (path or "/").strip()
        if not path.startswith(("http://", "https://")):
            path = "/" + path.lstrip("/")
        if profile_id is not None:
            encoded = quote(str(profile_id), safe="")
            path = path.replace("{profile_id}", encoded).replace("{id}", encoded)
        return path

    def _url(self, path: str, profile_id: str | None = None) -> str:
        rendered = self._render_path(path, profile_id)
        if rendered.startswith(("http://", "https://")):
            return rendered
        return f"{self.api_base}{rendered}"

    @staticmethod
    def _json_or_text(response):
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            text = (response.text or "").strip()
            return {"data": text} if text else {}

    @classmethod
    def _check_result(cls, result):
        if not isinstance(result, dict):
            return result
        if result.get("success") is False or result.get("ok") is False or result.get("status") is False:
            raise RuntimeError(str(result.get("msg") or result.get("message") or result.get("error") or "browser API rejected request"))
        if "code" in result:
            code = result.get("code")
            if code not in (None, 0, 200, "0", "200", "OK", "ok", True):
                raise RuntimeError(str(result.get("msg") or result.get("message") or result.get("error") or f"browser API code {code}"))
        return result

    def _request(self, method: str, path: str, payload: dict | None = None,
                 profile_id: str | None = None, retries: int = 2):
        method = (method or "POST").upper()
        url = self._url(path, profile_id)
        params = None
        body = payload if payload else None
        if method in {"GET", "HEAD", "DELETE"}:
            params = body
            body = None
        last = None
        for attempt in range(retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers=self._headers(),
                    timeout=self.timeout,
                    verify=self.verify_tls,
                )
                response.raise_for_status()
                return self._check_result(self._json_or_text(response))
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
                if attempt < retries:
                    time.sleep(min(1.5 * (attempt + 1), 4))
                    continue
                raise RuntimeError(f"custom browser API network error: {exc}") from exc
            except requests.HTTPError as exc:
                detail = ""
                try:
                    detail = str(self._json_or_text(exc.response))[:180]
                except Exception:
                    pass
                raise RuntimeError(f"custom browser API HTTP {getattr(exc.response, 'status_code', '?')}: {detail}") from exc
        raise RuntimeError(str(last or "custom browser API request failed"))

    @staticmethod
    def _data(result):
        if isinstance(result, dict):
            for key in ("data", "result", "profile"):
                if key in result and result[key] is not None:
                    return result[key]
        return result

    @classmethod
    def _find_value(cls, value, keys: tuple[str, ...]):
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if candidate not in (None, ""):
                    if isinstance(candidate, (dict, list)):
                        found = cls._find_value(candidate, keys)
                        if found not in (None, ""):
                            return found
                    else:
                        return candidate
            for child in value.values():
                found = cls._find_value(child, keys)
                if found not in (None, ""):
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_value(child, keys)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _profile_id(cls, result):
        value = cls._data(result)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
        found = cls._find_value(
            result,
            ("id", "profile_id", "profileId", "browser_id", "browserId", "uuid", "user_id", "userId"),
        )
        if found in (None, ""):
            raise RuntimeError("custom browser API did not return a profile id")
        return str(found)

    @classmethod
    def _endpoint(cls, result):
        value = cls._data(result)
        if isinstance(value, str) and value.startswith(("ws://", "wss://", "http://", "https://")):
            return value
        found = cls._find_value(
            value,
            (
                "ws", "wsUrl", "ws_url", "wsEndpoint", "ws_endpoint",
                "websocket", "webSocket", "webSocketDebuggerUrl",
                "cdp", "cdp_url", "cdpUrl", "debuggerUrl", "debug_url",
                "debuggerAddress", "debugger_address", "endpoint", "http",
                "selenium", "puppeteer",
            ),
        )
        if found:
            endpoint = str(found)
            if ":" in endpoint and not endpoint.startswith(("ws://", "wss://", "http://", "https://")):
                endpoint = "http://" + endpoint
            return endpoint
        port = cls._find_value(value, ("debugPort", "debug_port", "cdpPort"))
        if port:
            host = cls._find_value(value, ("debugHost", "debug_host", "host")) or "127.0.0.1"
            return f"http://{host}:{port}"
        raise RuntimeError("custom browser API did not return a CDP endpoint (ws/cdp/endpoint)")

    @staticmethod
    def _proxy_url(kwargs: dict) -> str:
        raw = kwargs.get("proxy_str") or kwargs.get("proxyUrl") or kwargs.get("proxy")
        if raw:
            return str(raw)
        host, port = str(kwargs.get("host") or "").strip(), str(kwargs.get("port") or "").strip()
        if not host or not port or str(kwargs.get("proxyType") or "").lower() == "noproxy":
            return ""
        scheme = "socks5" if str(kwargs.get("proxyType") or "").lower() == "socks5" else "http"
        user, password = str(kwargs.get("proxyUserName") or ""), str(kwargs.get("proxyPassword") or "")
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
        return f"{scheme}://{auth}{host}:{port}"

    def _id_payload(self, profile_id) -> dict:
        return {self.id_field: str(profile_id)}

    def _call(self, operation: str, payload=None, profile_id=None):
        path = self.paths[operation]
        if profile_id is not None and ("{id}" in path or "{profile_id}" in path) and isinstance(payload, dict):
            payload = dict(payload)
            payload.pop(self.id_field, None)
        return self._request(
            self.methods[operation],
            path,
            payload=payload,
            profile_id=profile_id,
        )

    def list_browsers(self, page=0, page_size=100):
        result = self._call("list", {"page": page, "pageSize": page_size})
        value = self._data(result)
        if isinstance(value, dict):
            values = value.get("list") or value.get("items") or value.get("profiles") or []
            total = value.get("total", len(values))
        elif isinstance(value, list):
            values, total = value, len(value)
        else:
            values, total = [], 0
        return {"success": True, "data": {"list": values, "total": total}}

    def open_browser(self, profile_id):
        if self._legacy:
            from common.traffic_saver import bitbrowser_open_payload
            payload = bitbrowser_open_payload(profile_id)
        else:
            payload = self._id_payload(profile_id)
        try:
            result = self._call("open", payload, profile_id=profile_id)
        except Exception:
            if self._legacy and payload.get("args"):
                result = self._call("open", self._id_payload(profile_id), profile_id=profile_id)
            else:
                raise
        endpoint = self._endpoint(result)
        value = self._data(result)
        return {**(value if isinstance(value, dict) else {}), "ws": endpoint}

    def update_browser_fingerprint(self, profile_id, **fingerprint):
        payload = (
            {"ids": [profile_id], "browserFingerPrint": fingerprint}
            if self._legacy
            else {**self._id_payload(profile_id), "fingerprint": fingerprint}
        )
        return self._call("update", payload, profile_id=profile_id)

    def close_browser(self, profile_id):
        return self._call("close", self._id_payload(profile_id), profile_id=profile_id)

    def delete_browser(self, profile_id):
        result = self._call("delete", self._id_payload(profile_id), profile_id=profile_id)
        try:
            from common.browser_registry import unregister
            unregister(profile_id)
        except Exception:
            pass
        return result

    def create_browser(self, name="reg_factory", **kwargs):
        if self._legacy:
            from common.traffic_saver import bitbrowser_profile_defaults
            payload = {
                "name": name,
                "remark": "reg-factory automated registration",
                "proxyMethod": 2,
                "proxyType": "noproxy",
                "browserFingerPrint": {"coreVersion": "130"},
                **bitbrowser_profile_defaults(),
                **kwargs,
            }
        else:
            payload = {
                "name": name,
                "remark": kwargs.get("remark", "reg-factory automated registration"),
                "fingerprint": kwargs.get("browserFingerPrint") or kwargs.get("fingerprint") or {},
                "proxy": self._proxy_url(kwargs),
            }
            if _bool("CUSTOM_BROWSER_API_FORWARD_FIELDS", False):
                for key, value in kwargs.items():
                    if key not in {"browserFingerPrint", "fingerprint", "proxy_str"}:
                        payload[key] = value
        result = self._call("create", payload)
        profile_id = self._profile_id(result)
        try:
            from common.browser_registry import register
            register(profile_id, name=name, provider=self.provider_name, api_base=self.api_base)
        except Exception as exc:
            print(f"  profile registry warning: {str(exc)[:120]}")
        print(f"  browser profile created: {name} (ID: {profile_id})")
        return profile_id

    def cleanup_browsers(self, keep=0):
        browsers = self.list_browsers(page=0, page_size=200)["data"]["list"]
        browsers = sorted(browsers, key=lambda item: item.get("seq", 0), reverse=True)
        deleted = 0
        for item in browsers[keep:]:
            profile_id = item.get("id") or item.get("profileId") or item.get("uuid")
            if not profile_id:
                continue
            try:
                self.close_browser(profile_id)
            except Exception:
                pass
            try:
                self.delete_browser(profile_id)
                deleted += 1
            except Exception:
                pass
        return deleted

    def _post(self, path, data=None, _retries=2):
        del _retries
        return self._request("POST", path, payload=data or {})

    def select_browser(self):
        browsers = self.list_browsers()["data"]["list"]
        if not browsers:
            return self.create_browser()
        for index, item in enumerate(browsers):
            print(f"  [{index}] {item.get('name', '')} ({item.get('id', '')})")
        choice = input("select browser index or n: ").strip().lower()
        if choice == "n":
            return self.create_browser()
        if choice.isdigit() and int(choice) < len(browsers):
            return browsers[int(choice)].get("id")
        raise ValueError("invalid browser selection")
