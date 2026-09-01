"""Codex workspace operations for the new standalone K12 adapter."""

from __future__ import annotations

import time
import uuid
from typing import Any

import requests


BASE_URL = "https://chatgpt.com"


def workspace_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        current = str(item or "").strip()
        key = current.lower()
        if current and key not in seen:
            result.append(current)
            seen.add(key)
    return result[:100]


def operate(
    access_token: str,
    workspace_id: str,
    route: str = "request",
    *,
    timeout: float = 30,
    retries: int = 2,
    interval: float = 1.5,
) -> dict[str, Any]:
    token = str(access_token or "").strip()
    workspace = str(workspace_id or "").strip()
    action = str(route or "request").strip().lower()
    if not token or not workspace:
        raise ValueError("access token and workspace id are required")
    if action not in {"request", "accept"}:
        raise ValueError("route must be request or accept")
    url = f"{BASE_URL}/backend-api/accounts/{workspace}/invites/{action}"
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
        "oai-device-id": str(uuid.uuid4()),
        "oai-language": "zh-CN",
        "user-agent": "Mozilla/5.0 RegFactory-K12/1.0",
    }
    last: dict[str, Any] | None = None
    for attempt in range(1, max(1, int(retries) + 1) + 1):
        try:
            response = requests.post(url, headers=headers, data=b"", timeout=timeout)
            last = {
                "workspace_id": workspace,
                "route": action,
                "ok": response.ok,
                "status": response.status_code,
                "body": response.text[:500],
                "attempt": attempt,
            }
            if response.ok or response.status_code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                return last
        except requests.RequestException as exc:
            last = {
                "workspace_id": workspace,
                "route": action,
                "ok": False,
                "status": 0,
                "body": str(exc)[:500],
                "attempt": attempt,
            }
        if attempt <= int(retries):
            time.sleep(max(0, float(interval)) * attempt)
    return last or {"workspace_id": workspace, "route": action, "ok": False, "status": 0, "body": "not executed", "attempt": 0}


def operate_many(access_token: str, values: Any, route: str = "request", **kwargs: Any) -> list[dict[str, Any]]:
    results = []
    for value in workspace_ids(values):
        results.append(operate(access_token, value, route, **kwargs))
    return results
