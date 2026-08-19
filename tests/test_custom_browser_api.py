from __future__ import annotations

from unittest.mock import Mock

from bitbrowser import BitBrowser


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_custom_api_generic_create_open_and_auth(monkeypatch):
    monkeypatch.setenv("FINGERPRINT_BROWSER", "custom_api")
    monkeypatch.setenv("CUSTOM_BROWSER_API", "http://browser.local/api")
    monkeypatch.setenv("CUSTOM_BROWSER_API_MODE", "generic")
    monkeypatch.setenv("CUSTOM_BROWSER_API_KEY", "secret")
    monkeypatch.setenv("CUSTOM_BROWSER_API_HEADERS", '{"X-Client":"reg-factory"}')
    monkeypatch.setenv("CUSTOM_BROWSER_API_CREATE_PATH", "/profiles")
    monkeypatch.setenv("CUSTOM_BROWSER_API_OPEN_PATH", "/profiles/{id}/start")
    monkeypatch.setenv("CUSTOM_BROWSER_API_OPEN_METHOD", "POST")

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/profiles"):
            return FakeResponse({"code": 0, "data": {"profileId": "p-1"}})
        return FakeResponse({"ok": True, "data": {"cdpUrl": "ws://127.0.0.1:9222/devtools/browser/x"}})

    browser = BitBrowser()
    browser.session.request = request
    profile_id = browser.create_browser(
        name="demo",
        browserFingerPrint={"coreVersion": "146"},
        proxy_str="http://127.0.0.1:7890",
    )
    endpoint = browser.open_browser(profile_id)

    assert profile_id == "p-1"
    assert endpoint["ws"].startswith("ws://")
    assert calls[0][0] == "POST"
    assert calls[0][1] == "http://browser.local/api/profiles"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer secret"
    assert calls[0][2]["headers"]["X-Client"] == "reg-factory"
    assert calls[0][2]["json"]["fingerprint"] == {"coreVersion": "146"}
    assert calls[1][1].endswith("/profiles/p-1/start")
    assert calls[1][2]["json"] is None


def test_custom_api_auto_accepts_bitbrowser_response(monkeypatch):
    monkeypatch.setenv("FINGERPRINT_BROWSER", "custom_api")
    monkeypatch.setenv("CUSTOM_BROWSER_API", "http://browser.local")
    monkeypatch.setenv("CUSTOM_BROWSER_API_MODE", "auto")

    browser = BitBrowser()
    browser.session.request = Mock(
        return_value=FakeResponse({"success": True, "data": {"id": "bb-1"}})
    )
    assert browser.create_browser(name="legacy") == "bb-1"


def test_custom_api_normalizes_list_response(monkeypatch):
    monkeypatch.setenv("FINGERPRINT_BROWSER", "custom_api")
    monkeypatch.setenv("CUSTOM_BROWSER_API", "http://browser.local")
    monkeypatch.setenv("CUSTOM_BROWSER_API_MODE", "generic")
    monkeypatch.setenv("CUSTOM_BROWSER_API_LIST_METHOD", "GET")

    browser = BitBrowser()
    browser.session.request = Mock(return_value=FakeResponse([{"id": "p-1"}]))
    result = browser.list_browsers()
    assert result["data"]["list"] == [{"id": "p-1"}]


def test_custom_api_normalizes_nested_debugger_address(monkeypatch):
    monkeypatch.setenv("FINGERPRINT_BROWSER", "custom_api")
    monkeypatch.setenv("CUSTOM_BROWSER_API", "http://browser.local")
    monkeypatch.setenv("CUSTOM_BROWSER_API_MODE", "generic")

    browser = BitBrowser()
    browser.session.request = Mock(
        return_value=FakeResponse({"status": True, "data": {"ws": {"selenium": "127.0.0.1:9333"}}})
    )
    assert browser.open_browser("p-2")["ws"] == "http://127.0.0.1:9333"
