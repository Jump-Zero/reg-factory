"""Online status scanner for local mailbox and platform asset pools."""

from __future__ import annotations

import copy
import hashlib
import base64
import json
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import requests

from common import asset_store

PLATFORMS = ("outlook", "chatgpt", "claude", "grok", "kiro")

# 缓存 Sub2API 账号列表（60 秒过期），避免每次 get_report 都登录查询
_sub2api_cache: dict = {"data": None, "ts": 0.0}
_SUB2API_CACHE_TTL = 60
STATUSES = (
    "normal",
    "unlock",
    "banned",
    "expired",
    "restricted",
    "invalid",
    "unknown",
    "error",
)
PLUS_TRIAL_STATUSES = (
    "eligible",
    "zero_price",
    "discount",
    "ineligible",
    "active",
    "unknown",
    "disabled",
)

SAFE_SCAN_DEFAULT_CACHE_SECONDS = 6 * 60 * 60
SAFE_SCAN_DEFAULT_MIN_INTERVAL = 3.0
SAFE_SCAN_DEFAULT_MAX_INTERVAL = 6.0
REPORT_CACHE_SECONDS = 5.0

_REPORT_CACHE_LOCK = threading.Lock()
_REPORT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}

_BANNED_MARKERS = (
    "account_deactivated",
    "account deactivated",
    "account_disabled",
    "account disabled",
    "account suspended",
    "your account has been suspended",
    "your account has been banned",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_number(name: str, default, minimum, maximum, cast=float):
    try:
        value = cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = cast(default)
    return min(maximum, max(minimum, value))


def _checked_at_epoch(item: dict) -> float:
    value = str(item.get("checked_at") or "").strip()
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _scan_path() -> Path:
    return asset_store._data_root() / "runtime" / "state" / "asset_pool_scan.json"


def invalidate_report_cache() -> None:
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE.clear()


def _stable_id(platform: str, email: str, source: str) -> str:
    identity = email.strip().lower() or source.strip().lower()
    return hashlib.sha256(f"{platform}|{identity}".encode("utf-8")).hexdigest()[:20]


def _read_cache() -> dict:
    try:
        value = json.loads(_scan_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_cache(report: dict) -> None:
    path = _scan_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    invalidate_report_cache()


def _history_outcomes() -> dict[str, dict]:
    """Return the newest known Outlook unlock outcome for each email."""
    root = asset_store._data_root()
    mappings = {
        "unlocked_": ("normal", "历史解锁成功"),
        "unlocked_clean_": ("normal", "历史解锁成功"),
        "needs_phone_": ("unlock", "需要手机验证解锁"),
        "locked_for_unlock_": ("unlock", "等待解锁"),
        "abuse_locked_": ("banned", "Abuse 锁定"),
        "dead_account_": ("banned", "账号不可用"),
        "failed_": ("unknown", "历史检测失败"),
    }
    result: dict[str, dict] = {}
    paths = []
    for directory in (root / "unlock_results", root / "check_results"):
        if directory.is_dir():
            paths.extend(path for path in directory.glob("*.txt") if path.is_file())
    for path in sorted(paths, key=lambda item: item.stat().st_mtime):
        mapping = next((value for prefix, value in mappings.items() if path.name.startswith(prefix)), None)
        if not mapping:
            continue
        checked_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            email = raw.strip().split("----", 1)[0].strip().lower()
            if "@" in email:
                result[email] = {
                    "status": mapping[0],
                    "detail": mapping[1],
                    "evidence": f"history:{path.name}",
                    "checked_at": checked_at,
                }
    return result


def _claude_token_records() -> list[dict]:
    directory = asset_store._token_root() / "claude"
    records = []
    paths = directory.glob("*.sessionKey.json") if directory.is_dir() else ()
    for path in paths:
        try:
            data = asset_store._read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            records.append({"path": path, "data": data})
    return sorted(records, key=lambda item: (item["path"].stat().st_mtime, str(item["path"]).lower()))


def _merge_platform_records(platform: str) -> list[dict]:
    merged: dict[str, dict] = {}

    def obtain(email: str, source: str) -> dict:
        email = str(email or "").strip()
        key = email.lower() or f"source:{source.lower()}"
        if key not in merged:
            merged[key] = {
                "platform": platform,
                "kind": "platform",
                "email": email,
                "email_provider": asset_store.classify_email_provider(email),
                "sources": set(),
                "_cookies": [],
                "_token": {},
                **({"codex_phone_status": "not_verified"} if platform == "chatgpt" else {}),
            }
        elif email and not merged[key]["email"]:
            merged[key]["email"] = email
            merged[key]["email_provider"] = asset_store.classify_email_provider(email)
        merged[key]["sources"].add(source)
        return merged[key]

    token_records = _claude_token_records() if platform == "claude" else asset_store._token_records(platform)
    for record in token_records:
        data = record["data"]
        source = record["path"].name
        email = asset_store._email_from_session(data, record["path"].stem.split(".")[0])
        target = obtain(email, source)
        target["_token"] = data
        provider = str(data.get("email_provider") or "").strip().lower()
        target["email_provider"] = provider if provider in asset_store.EMAIL_PROVIDERS else asset_store.classify_email_provider(target.get("email", ""))
        if platform == "chatgpt":
            phone_status = str(data.get("codex_phone_status") or "not_verified").strip().lower()
            target["codex_phone_status"] = phone_status if phone_status in {"verified", "not_verified"} else "not_verified"
            target["registration_country"] = str(
                data.get("registration_country") or ""
            ).strip().upper()
            target["network_node"] = str(data.get("network_node") or "").strip()

    for record in asset_store._cookie_records(platform):
        source = record["path"].name
        target = obtain(record.get("email", ""), source)
        target["_cookies"] = record["cookies"]

    records = []
    for record in merged.values():
        source = ", ".join(sorted(record.pop("sources")))
        record["source"] = source
        record["id"] = _stable_id(platform, record["email"], source)
        records.append(record)
    return sorted(records, key=lambda item: (item["email"].lower(), item["source"].lower()))


def _inventory_records() -> list[dict]:
    records = []
    history = _history_outcomes()
    registered = asset_store.registered_mailbox_usage()
    seen_mailboxes = set()
    for index, mailbox in enumerate(asset_store._mailboxes()):
        email = mailbox.get("email", "").strip()
        identity = email.lower()
        if identity in seen_mailboxes:
            continue
        seen_mailboxes.add(identity)
        source = f"emails.txt:{index + 1}"
        records.append({
            "id": _stable_id("outlook", email, source),
            "platform": "outlook",
            "kind": "mailbox",
            "email": email,
            "email_provider": mailbox.get("email_provider") or asset_store.classify_email_provider(email),
            "pristine": identity not in registered,
            "registered_platforms": list(registered.get(identity, ())),
            "source": source,
            "_mailbox": mailbox,
            "_history": history.get(identity),
        })
    for platform in ("chatgpt", "claude", "grok"):
        records.extend(_merge_platform_records(platform))
    # Kiro is an account bundle rather than a browser/cookie pool. Include it
    # in inventory when present without changing the legacy scan pool contract.
    if asset_store._token_records("kiro"):
        records.extend(_merge_platform_records("kiro"))
    return records


def _public_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def _status_summary(items: list[dict]) -> dict:
    statuses = {status: 0 for status in STATUSES}
    plus_trial = {status: 0 for status in PLUS_TRIAL_STATUSES}
    email_providers = {provider: 0 for provider in asset_store.EMAIL_PROVIDERS}
    platforms = {}
    for item in items:
        status = item.get("status", "unknown")
        if status not in statuses:
            status = "unknown"
        statuses[status] += 1
        platform = item.get("platform", "unknown")
        entry = platforms.setdefault(platform, {"total": 0, **{name: 0 for name in STATUSES}})
        entry["total"] += 1
        entry[status] += 1
        if platform == "chatgpt":
            trial_status = str(item.get("plus_trial") or "unknown")
            plus_trial[trial_status if trial_status in plus_trial else "unknown"] += 1
        provider = str(item.get("email_provider") or "other")
        email_providers[provider if provider in email_providers else "other"] += 1
    return {
        "total": len(items),
        "statuses": statuses,
        "platforms": platforms,
        "plus_trial": plus_trial,
        "email_providers": email_providers,
    }


def _load_sub2api_uploaded() -> dict[str, set[str]]:
    """Return {platform: set(emails)} that exist in the Sub2API database.
    
    Queries the Sub2API admin API for real account data. Falls back to the
    local uploaded_sub2api.txt marker file when the API is unreachable.
    Results are cached for _SUB2API_CACHE_TTL seconds.
    """
    now = time.time()
    if _sub2api_cache["data"] is not None and now - _sub2api_cache["ts"] < _SUB2API_CACHE_TTL:
        return _sub2api_cache["data"]

    result: dict[str, set[str]] = {}
    
    # 读取 Sub2API 配置
    try:
        import config
        sub2api_url = (getattr(config, "SUB2API_URL", "") or "").strip()
        sub2api_email = (getattr(config, "SUB2API_EMAIL", "") or "").strip()
        sub2api_password = (getattr(config, "SUB2API_PASSWORD", "") or "").strip()
    except Exception:
        sub2api_url = sub2api_email = sub2api_password = ""

    if sub2api_url and sub2api_email and sub2api_password:
        try:
            from common.uploaders import _sub2api_login, _sub2api_request, _origin
            from urllib.parse import urlparse

            origin = _origin(sub2api_url)
            token = _sub2api_login(origin, sub2api_email, sub2api_password, timeout=10)
            
            # 查询所有平台的账号（分页获取）。
            # chatgpt 账号经 import/codex-session 导入后在 Sub2API 中属于
            # openai 平台，需同时查询两个平台名并合并，否则导入状态永远显示未导入。
            sub2api_platforms = {
                "grok": ("grok",),
                "chatgpt": ("chatgpt", "openai"),
                "claude": ("claude",),
            }
            for platform, query_platforms in sub2api_platforms.items():
                all_names: set[str] = set()
                for query_platform in query_platforms:
                    page = 1
                    while True:
                        resp = _sub2api_request(
                            origin,
                            f"/api/v1/admin/accounts?page={page}&page_size=200&platform={query_platform}",
                            token=token,
                            timeout=15,
                            retries=1,
                            use_env_proxy=False,
                        )
                        items = resp.get("items", []) if isinstance(resp, dict) else []
                        for item in items:
                            name = str(item.get("name") or "").strip().lower()
                            if name:
                                all_names.add(name)
                        total_pages = resp.get("pages", 1) if isinstance(resp, dict) else 1
                        if page >= total_pages or not items:
                            break
                        page += 1
                if all_names:
                    result[platform] = all_names
            
            _sub2api_cache["data"] = result
            _sub2api_cache["ts"] = now
            return result
        except Exception:
            pass  # API 失败时回退到本地标记

    # 回退：读取本地 uploaded_sub2api.txt 标记文件
    token_root = asset_store._token_root()
    for platform in ("chatgpt", "claude", "grok", "kiro"):
        path = token_root / platform / "uploaded_sub2api.txt"
        if path.is_file():
            result[platform] = {
                line.strip().lower()
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            }
    _sub2api_cache["data"] = result
    _sub2api_cache["ts"] = now
    return result


def get_report() -> dict:
    report_key = (str(asset_store._data_root()).lower(), str(asset_store._token_root()).lower())
    now = time.monotonic()
    with _REPORT_CACHE_LOCK:
        cached_report = _REPORT_CACHE.get(report_key)
        if cached_report and cached_report[0] > now:
            return copy.deepcopy(cached_report[1])

        cache = _read_cache()
        cached_items = {
            str(item.get("id")): item
            for item in cache.get("items", [])
            if isinstance(item, dict) and item.get("id")
        }
        sub2api_map = _load_sub2api_uploaded()
        items = []
        for record in _inventory_records():
            public = _public_record(record)
            cached = cached_items.get(public["id"], {})
            for key in (
                "status", "detail", "evidence", "checked_at", "latency_ms",
                "plus_trial", "plus_trial_detail", "plus_trial_evidence", "plan_type",
            ):
                if key in cached:
                    public[key] = cached[key]
            if (
                public.get("status") == "unknown"
                and str(public.get("evidence") or "").startswith("safe_scan:circuit_breaker:")
            ):
                public["status"] = "error"
                public["detail"] = "上次扫描因网络或风控熔断而未完成，账号仍保留待重试"
            public.setdefault("status", "unknown")
            public.setdefault("detail", "尚未扫描")
            public.setdefault("evidence", "none")
            public.setdefault("checked_at", "")
            platform = public.get("platform", "")
            email = str(public.get("email", "")).strip().lower()
            uploaded_set = sub2api_map.get(platform)
            public["sub2api_uploaded"] = bool(uploaded_set and email in uploaded_set)
            if public.get("platform") == "chatgpt":
                public.setdefault("plus_trial", "unknown")
                public.setdefault("plus_trial_detail", "尚未检测 Plus 试用资格")
                public.setdefault("plus_trial_evidence", "none")
                # Older scanners used ``eligible`` for any campaign, including
                # non-zero discounts. Fail closed until a fresh strict scan.
                if public.get("plus_trial") == "eligible":
                    public["plus_trial"] = "unknown"
                    public["plus_trial_detail"] = "历史活动结果未确认 0 元，请重新扫描"
                    public["plus_trial_evidence"] = (
                        f"{public.get('plus_trial_evidence') or 'legacy'}:legacy_unconfirmed"
                    )
            if public.get("platform") == "grok" and public.get("status") == "normal":
                authorization = _grok_authorization_status(record)
                if authorization in {"failed", "pending"}:
                    public["status"] = "restricted" if authorization == "failed" else "unknown"
                    public["detail"] = {
                        "failed": "Grok SSO 正常，但 OAuth 授权失败",
                        "pending": "Grok SSO 正常，OAuth 授权尚未完成",
                    }.get(authorization, "Grok OAuth 授权状态未知")
                    public["evidence"] = f"local:grok_oauth_{authorization}"
            items.append(public)
        report = {
            "schema_version": 2,
            "last_scan_at": cache.get("finished_at", ""),
            "items": items,
            "summary": _status_summary(items),
            "sub2api_counts": {
                "imported": sum(1 for item in items if item.get("sub2api_uploaded")),
                "not_imported": sum(1 for item in items if not item.get("sub2api_uploaded")),
            },
        }
        if isinstance(cache.get("safe_mode"), dict):
            report["safe_mode"] = dict(cache["safe_mode"])
        _REPORT_CACHE[report_key] = (time.monotonic() + REPORT_CACHE_SECONDS, report)
        return copy.deepcopy(report)


def update_cached_outlook_statuses(outcomes: dict[str, dict]) -> int:
    """Update cached Outlook outcomes after unlock or RT recovery without storing secrets."""
    normalized = {
        str(email or "").strip().lower(): outcome
        for email, outcome in (outcomes or {}).items()
        if str(email or "").strip() and isinstance(outcome, dict)
    }
    if not normalized:
        return 0
    cache = _read_cache()
    items = cache.get("items")
    if not isinstance(items, list):
        return 0
    updated = 0
    for item in items:
        if not isinstance(item, dict) or item.get("platform") != "outlook":
            continue
        outcome = normalized.get(str(item.get("email") or "").strip().lower())
        if not outcome:
            continue
        status = str(outcome.get("status") or "unknown").strip().lower()
        item.update({
            "status": status if status in STATUSES else "unknown",
            "detail": str(outcome.get("detail") or "恢复任务已更新账号状态"),
            "evidence": str(outcome.get("evidence") or "recovery:updated"),
            "checked_at": _now_iso(),
            "latency_ms": 0,
        })
        updated += 1
    if updated:
        cache["summary"] = _status_summary(items)
        cache["finished_at"] = _now_iso()
        _write_cache(cache)
    return updated


def _web_session(platform: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    try:
        from common import proxy_switch

        target_env = proxy_switch.platform_environment(os.environ, platform) if platform else os.environ
        proxy = proxy_switch.effective_proxy_url(target_env)
    except Exception:
        proxy = ""
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return session


def _response_status(response, service: str) -> dict | None:
    text = (response.text or "")[:3000].lower()
    if response.status_code == 401:
        return {"status": "expired", "detail": f"{service} 登录凭据已过期", "evidence": f"{service}:401"}
    if response.status_code == 403:
        if any(marker in text for marker in _BANNED_MARKERS):
            return {"status": "banned", "detail": f"{service} 明确返回账号停用", "evidence": f"{service}:403"}
        return {"status": "restricted", "detail": f"{service} HTTP 403，可能为账号风控或出口限制", "evidence": f"{service}:403"}
    if response.status_code == 429:
        return {"status": "restricted", "detail": f"{service} 请求限流", "evidence": f"{service}:429"}
    if response.status_code >= 500:
        return {"status": "error", "detail": f"{service} 服务异常 HTTP {response.status_code}", "evidence": f"{service}:{response.status_code}"}
    return None


def _platform_preflight(platform: str, timeout: int) -> dict | None:
    """Short-circuit a whole pool when its service is unreachable from this exit."""
    try:
        if platform == "outlook":
            with _web_session("outlook") as session:
                response = session.post(
                    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                    data={
                        "client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
                        "grant_type": "refresh_token",
                        "refresh_token": "preflight",
                        "scope": "https://graph.microsoft.com/Mail.Read",
                    },
                    timeout=timeout,
                )
        else:
            urls = {
                "chatgpt": "https://chatgpt.com/api/auth/session",
                "claude": "https://claude.ai/api/account",
                "grok": "https://accounts.x.ai/",
                "kiro": "https://oidc.us-east-1.amazonaws.com/",
            }
            with _web_session(platform) as session:
                response = session.get(urls[platform], timeout=timeout, allow_redirects=True)
        if response.status_code >= 500:
            return {
                "status": "error",
                "detail": f"{platform} 服务预检 HTTP {response.status_code}",
                "evidence": f"preflight:{response.status_code}",
            }
        return None
    except requests.Timeout:
        return {"status": "error", "detail": f"{platform} 服务预检超时", "evidence": "preflight:timeout"}
    except requests.RequestException as exc:
        return {
            "status": "error",
            "detail": f"{platform} 服务预检失败：{type(exc).__name__}",
            "evidence": "preflight:network_error",
        }


def _scan_outlook(record: dict, timeout: int) -> dict:
    mailbox = record.get("_mailbox") or {}
    refresh_token = str(mailbox.get("refresh_token") or "").strip()
    client_id = str(mailbox.get("client_id") or "").strip() or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    history = record.get("_history")
    if not refresh_token:
        if history and history.get("status") in {"unlock", "banned", "normal"}:
            return dict(history)
        return {"status": "unknown", "detail": "缺少 Graph refresh token，无法在线确认", "evidence": "local:missing_refresh_token"}

    with _web_session("outlook") as session:
        response = session.post(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://graph.microsoft.com/Mail.Read",
            },
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code == 200 and payload.get("access_token"):
            graph = session.get(
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox?$select=id",
                headers={"Authorization": f"Bearer {payload['access_token']}"},
                timeout=timeout,
            )
            if graph.status_code == 200:
                return {"status": "normal", "detail": "Graph 邮箱访问正常", "evidence": "microsoft_graph:200"}
            if graph.status_code in {401, 403}:
                return {"status": "restricted", "detail": f"Graph 邮箱访问 HTTP {graph.status_code}", "evidence": f"microsoft_graph:{graph.status_code}"}
            return {"status": "error", "detail": f"Graph 检测 HTTP {graph.status_code}", "evidence": f"microsoft_graph:{graph.status_code}"}

    description = str(payload.get("error_description") or payload.get("error") or "").lower()
    error_codes = {str(value) for value in payload.get("error_codes", [])}
    if "service abuse mode" in description:
        return {
            "status": "banned",
            "detail": "Microsoft 账号处于服务滥用限制",
            "evidence": "microsoft_oauth:service_abuse",
        }
    if "different tenant" in description:
        return {
            "status": "expired",
            "detail": "Graph refresh token 与账号租户不匹配",
            "evidence": "microsoft_oauth:tenant_mismatch",
        }
    if "50057" in error_codes or "user account is disabled" in description:
        return {"status": "banned", "detail": "Microsoft 账号已禁用", "evidence": "microsoft_oauth:AADSTS50057"}
    if "50053" in error_codes or "account is locked" in description:
        return {"status": "unlock", "detail": "Microsoft 账号已锁定，需要解锁", "evidence": "microsoft_oauth:AADSTS50053"}
    if error_codes.intersection({"50055", "50076", "50079"}):
        return {"status": "unlock", "detail": "Microsoft 要求补充验证", "evidence": f"microsoft_oauth:AADSTS{sorted(error_codes)[0]}"}
    if history and history.get("status") in {"unlock", "banned"}:
        return dict(history)
    if response.status_code == 400 and str(payload.get("error") or "") == "invalid_grant":
        return {"status": "expired", "detail": "Graph refresh token 已失效或撤销", "evidence": "microsoft_oauth:invalid_grant"}
    if response.status_code == 429:
        return {"status": "restricted", "detail": "Microsoft 请求限流", "evidence": "microsoft_oauth:429"}
    if response.status_code >= 500:
        return {"status": "error", "detail": f"Microsoft 服务异常 HTTP {response.status_code}", "evidence": f"microsoft_oauth:{response.status_code}"}
    return {"status": "unknown", "detail": f"Microsoft OAuth HTTP {response.status_code}", "evidence": f"microsoft_oauth:{response.status_code}"}


def _scan_chatgpt(record: dict, timeout: int) -> dict:
    cookies = record.get("_cookies") or []
    if cookies:
        with _web_session("chatgpt") as session:
            response = session.get(
                "https://chatgpt.com/api/auth/session",
                headers={"Cookie": asset_store._cookie_header(cookies), "Cache-Control": "no-cache"},
                timeout=timeout,
            )
        classified = _response_status(response, "chatgpt_session")
        if classified:
            return classified
        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if payload.get("accessToken"):
                return {
                    "status": "normal",
                    "detail": "ChatGPT 登录会话正常",
                    "evidence": "chatgpt_session:200",
                    "_access_token": str(payload["accessToken"]),
                }
            return {"status": "expired", "detail": "ChatGPT 会话未返回 accessToken", "evidence": "chatgpt_session:empty"}
        return {"status": "unknown", "detail": f"ChatGPT HTTP {response.status_code}", "evidence": f"chatgpt_session:{response.status_code}"}

    token = record.get("_token") or {}
    access_token = str(token.get("accessToken") or token.get("access_token") or "").strip()
    if not access_token:
        return {"status": "invalid", "detail": "缺少 ChatGPT session Cookie 或 accessToken", "evidence": "local:missing_credential"}
    with _web_session("chatgpt") as session:
        response = session.get(
            "https://chatgpt.com/backend-api/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
        )
    classified = _response_status(response, "chatgpt_token")
    if classified:
        return classified
    if response.status_code == 200:
        return {
            "status": "normal",
            "detail": "ChatGPT accessToken 正常",
            "evidence": "chatgpt_token:200",
            "_access_token": access_token,
        }
    return {"status": "unknown", "detail": f"ChatGPT token HTTP {response.status_code}", "evidence": f"chatgpt_token:{response.status_code}"}


def _chatgpt_plan_type(record: dict) -> str:
    token = record.get("_token") if isinstance(record.get("_token"), dict) else {}
    account = token.get("account") if isinstance(token.get("account"), dict) else {}
    entitlement = token.get("entitlement") if isinstance(token.get("entitlement"), dict) else {}
    raw = (
        account.get("planType")
        or account.get("plan_type")
        or token.get("planType")
        or token.get("plan_type")
        or entitlement.get("subscription_plan")
        or ""
    )
    plan = str(raw).strip().lower()
    if plan in {"chatgptfreeplan", "free_plan", "free"}:
        return "free"
    return plan


def _jwt_chatgpt_account_id(access_token: str) -> str:
    """Read the account id claim used by ChatGPT's accounts/check endpoint."""
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    direct = claims.get("https://api.openai.com/auth.chatgpt_account_id")
    if direct:
        return str(direct).strip()
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "").strip()
    return ""


def _chatgpt_account_id(record: dict, access_token: str) -> str:
    token = record.get("_token") if isinstance(record.get("_token"), dict) else {}
    account = token.get("account") if isinstance(token.get("account"), dict) else {}
    for value in (
        token.get("chatgpt_account_id"),
        token.get("account_id"),
        account.get("id"),
        account.get("account_id"),
        record.get("chatgpt_account_id"),
        record.get("account_id"),
    ):
        if value:
            return str(value).strip()
    return _jwt_chatgpt_account_id(access_token)


def _decimal_value(value):
    """Parse a finite numeric API value without treating malformed data as zero."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def parse_chatgpt_accounts_check(payload, account_id: str = "") -> dict:
    """Normalize the read-only ``accounts/check`` response.

    A free plan and a campaign are not enough to call an offer free. The
    public scanner only treats an explicit zero price or a 100% discount as
    ``plus_trial_zero_price``; campaigns with a lower discount are retained as
    ``discount`` so they cannot enter the zero-price protocol pool.
    """
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        return {"ok": False, "error": "accounts_check_missing_accounts"}
    selected_id = ""
    selected = None
    if account_id and isinstance(accounts.get(account_id), dict):
        selected_id, selected = account_id, accounts.get(account_id)
    elif isinstance(accounts.get("default"), dict):
        selected_id, selected = "default", accounts.get("default")
    else:
        for key, value in accounts.items():
            if key != "default" and isinstance(value, dict):
                selected_id, selected = str(key), value
                break
    if not isinstance(selected, dict):
        return {"ok": False, "error": "accounts_check_no_entry"}

    account = selected.get("account") if isinstance(selected.get("account"), dict) else {}
    entitlement = selected.get("entitlement") if isinstance(selected.get("entitlement"), dict) else {}
    campaigns = selected.get("eligible_promo_campaigns")
    campaigns = campaigns if isinstance(campaigns, dict) else {}
    plus_campaign = campaigns.get("plus")
    if isinstance(plus_campaign, list):
        plus_campaign = plus_campaign[0] if plus_campaign else None
    if not isinstance(plus_campaign, dict):
        plus_campaign = None
    metadata = plus_campaign.get("metadata") if plus_campaign else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    discount = metadata.get("discount") if isinstance(metadata.get("discount"), dict) else {}
    duration = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}

    plan_type = str(account.get("plan_type") or account.get("planType") or "").strip()
    subscription_plan = str(entitlement.get("subscription_plan") or "").strip()
    plan_lower = plan_type.lower()
    subscription_lower = subscription_plan.lower()
    is_free = plan_lower in {"free", "free_plan", "chatgptfreeplan"} or subscription_lower == "chatgptfreeplan"
    offers = selected.get("eligible_offers") if isinstance(selected.get("eligible_offers"), dict) else {}
    offer_rows = offers.get("offers") if isinstance(offers.get("offers"), list) else []
    offer_ids = [str(row.get("id")) for row in offer_rows if isinstance(row, dict) and row.get("id")]

    discount_percentage = discount.get("percentage") if plus_campaign else None
    discount_value = _decimal_value(discount_percentage)
    explicit_zero_path = _zero_price_offer(plus_campaign) if plus_campaign else ""
    zero_price = bool(
        is_free
        and plus_campaign
        and (explicit_zero_path or (discount_value is not None and discount_value >= 100))
    )
    has_campaign = bool(is_free and plus_campaign)

    return {
        "ok": True,
        "account_id": selected_id,
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": bool(entitlement.get("has_active_subscription")),
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "plus_trial_eligible": bool(has_campaign and zero_price),
        "plus_trial_has_campaign": has_campaign,
        "plus_trial_zero_price": bool(zero_price),
        "plus_trial_campaign_id": plus_campaign.get("id") if plus_campaign else None,
        "plus_trial_title": metadata.get("title") if plus_campaign else None,
        "plus_trial_discount_percentage": discount_percentage,
        "plus_trial_duration_num_periods": duration.get("num_periods") if plus_campaign else None,
        "plus_trial_duration_period": duration.get("period") if plus_campaign else None,
        "eligible_offer_ids": offer_ids,
    }


_ZERO_PRICE_KEYS = {
    "amount_due",
    "amount_total",
    "checkout_price",
    "display_price",
    "discounted_price",
    "due",
    "final_amount",
    "final_price",
    "formatted_price",
    "payable_amount",
    "price",
    "price_after_discount",
    "price_label",
    "total",
    "total_amount",
}
_ZERO_PRICE_TEXT = re.compile(
    r"(?:[$\u20ac\u00a3\u00a5\u20b1\u20b9\u20a9]\s*0(?:[.,]0+)?|"
    r"0(?:[.,]0+)?\s*(?:usd|eur|gbp|jpy|cny|rmb|php|inr|krw|aud|cad|"
    r"\u5143|\u5186)|\bfree\b|no\s+charge|\u514d\u8d39)",
    re.IGNORECASE,
)


def _zero_price_offer(payload) -> str:
    """Return the evidence path for an explicitly payable zero-price offer."""
    queue = [("", payload)]
    while queue:
        path, value = queue.pop(0)
        if isinstance(value, dict):
            for key, nested in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                normalized_key = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
                parent_parts = set(re.split(r"[.\[\]_]+", path.lower()))
                excluded_price = bool(
                    parent_parts
                    & {"credit", "discount", "discounts", "saving", "savings"}
                )
                if (
                    normalized_key in _ZERO_PRICE_KEYS
                    and not excluded_price
                    and not isinstance(nested, bool)
                ):
                    try:
                        if Decimal(str(nested).strip()) == 0:
                            return child_path
                    except (InvalidOperation, TypeError, ValueError):
                        if isinstance(nested, str) and _ZERO_PRICE_TEXT.search(nested):
                            return child_path
                if isinstance(nested, str) and any(
                    marker in normalized_key
                    for marker in ("display_price", "formatted_price", "price_label")
                ) and _ZERO_PRICE_TEXT.search(nested):
                    return child_path
                if isinstance(nested, (dict, list)):
                    queue.append((child_path, nested))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                if isinstance(nested, (dict, list)):
                    queue.append((f"{path}[{index}]", nested))
    return ""


def _discount_percentage(payload):
    """Find an explicit percentage discount in a promotion response."""
    queue = [payload]
    while queue:
        value = queue.pop(0)
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
                if normalized_key in {"percentage", "percent", "discount_percentage"}:
                    parsed = _decimal_value(nested)
                    if parsed is not None:
                        return parsed
                if isinstance(nested, (dict, list)):
                    queue.append(nested)
        elif isinstance(value, list):
            queue.extend(nested for nested in value if isinstance(nested, (dict, list)))
    return None


def _scan_chatgpt_plus_trial(record: dict, access_token: str, timeout: int) -> dict:
    """Check trial eligibility through ChatGPT's authoritative accounts API."""
    enabled = str(os.environ.get("ASSET_SCAN_CHATGPT_PLUS_TRIAL", "true")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return {
            "plus_trial": "disabled",
            "plus_trial_detail": "Plus trial eligibility check disabled",
            "plus_trial_evidence": "config:disabled",
        }

    plan_type = _chatgpt_plan_type(record)
    if plan_type and plan_type not in {"free", "unknown"}:
        return {
            "plus_trial": "active",
            "plus_trial_detail": f"ChatGPT account has {plan_type} plan",
            "plus_trial_evidence": f"session:plan:{plan_type}",
        }
    token = str(access_token or "").strip()
    if not token:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "Missing access token; trial eligibility was not checked",
            "plus_trial_evidence": "local:missing_access_token",
        }

    account_id = _chatgpt_account_id(record, token)
    identity = str(record.get("email") or hashlib.sha256(token.encode("utf-8")).hexdigest())
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"reg-factory-plus-trial:{identity.lower()}"))
    endpoint = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "x-openai-target-path": "/backend-api/accounts/check/v4-2023-04-27",
        "x-openai-target-route": "/backend-api/accounts/check/{version}",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    try:
        with _web_session("chatgpt") as session:
            response = session.get(
                endpoint,
                params={"timezone_offset_min": "-"},
                headers=headers,
                timeout=timeout,
            )
            try:
                payload = response.json() if response.status_code < 500 else {}
            except Exception:
                payload = {}
    except requests.Timeout:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "ChatGPT accounts check timed out",
            "plus_trial_evidence": "accounts_check:timeout",
        }
    except requests.RequestException as exc:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"ChatGPT accounts check failed: {type(exc).__name__}",
            "plus_trial_evidence": "accounts_check:network_error",
        }
    except Exception as exc:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"ChatGPT accounts check failed: {type(exc).__name__}",
            "plus_trial_evidence": "accounts_check:error",
        }

    if response.status_code == 401:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "ChatGPT access token is invalid",
            "plus_trial_evidence": "accounts_check:401",
        }
    if response.status_code == 403:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "ChatGPT accounts check was blocked or restricted",
            "plus_trial_evidence": "accounts_check:403",
        }

    parsed = parse_chatgpt_accounts_check(payload, account_id=account_id)
    if response.status_code == 200 and parsed.get("ok"):
        evidence = "accounts_check:200"
        if parsed.get("plus_trial_zero_price"):
            detail = "ChatGPT Plus trial explicitly priced at 0"
            title = parsed.get("plus_trial_title")
            if title:
                detail = f"{detail}: {title}"
            return {
                "plus_trial": "zero_price",
                "plus_trial_detail": detail,
                "plus_trial_evidence": f"{evidence}:zero_price",
                "plus_trial_campaign_id": parsed.get("plus_trial_campaign_id"),
                "plus_trial_discount_percentage": parsed.get("plus_trial_discount_percentage"),
                "plus_trial_duration_num_periods": parsed.get("plus_trial_duration_num_periods"),
                "plus_trial_duration_period": parsed.get("plus_trial_duration_period"),
            }
        if parsed.get("plus_trial_has_campaign"):
            percentage = _decimal_value(parsed.get("plus_trial_discount_percentage"))
            title = parsed.get("plus_trial_title")
            if percentage is not None and percentage < 100:
                detail = f"ChatGPT Plus campaign is {parsed.get('plus_trial_discount_percentage')}% off; not free"
                if title:
                    detail = f"{detail}: {title}"
                return {
                    "plus_trial": "discount",
                    "plus_trial_detail": detail,
                    "plus_trial_evidence": f"{evidence}:discount",
                    "plus_trial_campaign_id": parsed.get("plus_trial_campaign_id"),
                    "plus_trial_discount_percentage": parsed.get("plus_trial_discount_percentage"),
                    "plus_trial_duration_num_periods": parsed.get("plus_trial_duration_num_periods"),
                    "plus_trial_duration_period": parsed.get("plus_trial_duration_period"),
                }
            return {
                "plus_trial": "unknown",
                "plus_trial_detail": "Plus campaign found, but the payable amount is not confirmed as 0",
                "plus_trial_evidence": f"{evidence}:price_unconfirmed",
                "plus_trial_campaign_id": parsed.get("plus_trial_campaign_id"),
                "plus_trial_discount_percentage": parsed.get("plus_trial_discount_percentage"),
                "plus_trial_duration_num_periods": parsed.get("plus_trial_duration_num_periods"),
                "plus_trial_duration_period": parsed.get("plus_trial_duration_period"),
            }
        return {
            "plus_trial": "ineligible",
            "plus_trial_detail": "Free ChatGPT account has no eligible Plus trial campaign",
            "plus_trial_evidence": f"{evidence}:no_plus_campaign",
            "plus_trial_offer_ids": parsed.get("eligible_offer_ids") or [],
        }

    # Keep compatibility with older deployments that only expose the coupon
    # endpoint. A valid accounts response without a Plus campaign is final;
    # fallback is only for responses that do not contain the new shape.
    if not isinstance(payload, dict) or "accounts" not in payload:
        return _scan_chatgpt_coupon_trial(record, token, timeout)
    return {
        "plus_trial": "unknown",
        "plus_trial_detail": f"ChatGPT accounts check returned HTTP {response.status_code}",
        "plus_trial_evidence": f"accounts_check:{response.status_code}",
    }


def _scan_chatgpt_coupon_trial(record: dict, access_token: str, timeout: int) -> dict:
    enabled = str(os.environ.get("ASSET_SCAN_CHATGPT_PLUS_TRIAL", "true")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return {
            "plus_trial": "disabled",
            "plus_trial_detail": "Plus 试用资格检测已关闭",
            "plus_trial_evidence": "config:disabled",
        }

    plan_type = _chatgpt_plan_type(record)
    if plan_type and plan_type not in {"free", "unknown"}:
        return {
            "plus_trial": "active",
            "plus_trial_detail": f"账号已有 {plan_type} 套餐",
            "plus_trial_evidence": f"session:plan:{plan_type}",
        }
    token = str(access_token or "").strip()
    if not token:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "缺少 accessToken，未检测 Plus 试用资格",
            "plus_trial_evidence": "local:missing_access_token",
        }

    campaign = str(
        os.environ.get("ASSET_SCAN_CHATGPT_PLUS_CAMPAIGN", "plus-1-month-free")
    ).strip() or "plus-1-month-free"
    identity = str(record.get("email") or hashlib.sha256(token.encode("utf-8")).hexdigest())
    device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"reg-factory-plus-trial:{identity.lower()}"))
    try:
        with _web_session("chatgpt") as session:
            response = session.get(
                "https://chatgpt.com/backend-api/promo_campaign/check_coupon",
                params={"coupon": campaign, "is_coupon_from_query_param": "true"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://chatgpt.com",
                    "Referer": "https://chatgpt.com/",
                    "oai-device-id": device_id,
                    "x-openai-target-path": "/backend-api/promo_campaign/check_coupon",
                    "x-openai-target-route": "/backend-api/promo_campaign/check_coupon",
                },
                timeout=timeout,
            )
        try:
            payload = response.json() if response.status_code < 500 else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        state = str(payload.get("state") or "").strip().lower()
        redemption = payload.get("redemption") if isinstance(payload.get("redemption"), dict) else {}
        redeemed_by_user = redemption.get("redeemed_by_user") is True
        evidence = f"promo_campaign:{response.status_code}:{state or 'none'}"
        zero_price_path = _zero_price_offer(payload)
        ineligible = state in {"ineligible", "redeemed", "expired"} or redeemed_by_user
        if response.status_code == 200 and zero_price_path and not ineligible:
            return {
                "plus_trial": "zero_price",
                "plus_trial_detail": "命中明确显示 0 元的 Plus 优惠",
                "plus_trial_evidence": f"{evidence}:zero:{zero_price_path}",
            }
        if response.status_code == 200 and state == "eligible" and not redeemed_by_user:
            discount_percentage = _discount_percentage(payload)
            if discount_percentage is not None and discount_percentage >= 100:
                return {
                    "plus_trial": "zero_price",
                    "plus_trial_detail": "命中 Plus 100% 折扣，按 0 元试用处理",
                    "plus_trial_evidence": f"{evidence}:zero_discount",
                    "plus_trial_discount_percentage": str(discount_percentage),
                }
            if discount_percentage is not None and discount_percentage < 100:
                return {
                    "plus_trial": "discount",
                    "plus_trial_detail": f"命中 Plus 优惠，但折扣为 {discount_percentage}%（不是 0 元）",
                    "plus_trial_evidence": f"{evidence}:discount",
                    "plus_trial_discount_percentage": str(discount_percentage),
                }
            return {
                "plus_trial": "unknown",
                "plus_trial_detail": "命中 Plus 活动，但接口未确认应付金额为 0 元",
                "plus_trial_evidence": f"{evidence}:price_unconfirmed",
            }
        if response.status_code == 200 and ineligible:
            return {
                "plus_trial": "ineligible",
                "plus_trial_detail": "当前没有可用的 Plus 免费试用资格",
                "plus_trial_evidence": evidence,
            }
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 资格接口未返回明确结果（HTTP {response.status_code}）",
            "plus_trial_evidence": evidence,
        }
    except requests.Timeout:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": "Plus 试用资格检测超时",
            "plus_trial_evidence": "promo_campaign:timeout",
        }
    except requests.RequestException as exc:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 试用资格网络检测失败：{type(exc).__name__}",
            "plus_trial_evidence": "promo_campaign:network_error",
        }
    except Exception as exc:
        return {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 试用资格检测异常：{type(exc).__name__}",
            "plus_trial_evidence": "promo_campaign:error",
        }


def check_chatgpt_plus_trial_for_session(
    session: dict, email: str = "", timeout: int = 15
) -> dict:
    """Check one newly saved ChatGPT session and update the local scan cache.

    Registration must not wait for a full pool scan just to label the account.
    This targeted path reuses the same read-only promotion check as the asset
    scanner, then merges the result into the cached public record without ever
    writing the access token to the cache.
    """
    data = session if isinstance(session, dict) else {}
    token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    address = str(
        email
        or (data.get("user") or {}).get("email")
        or data.get("email")
        or ""
    ).strip()
    record = {
        "platform": "chatgpt",
        "kind": "platform",
        "email": address,
        "_token": data,
    }
    started = time.monotonic()
    try:
        outcome = _scan_chatgpt_plus_trial(record, token, int(timeout))
    except Exception as exc:  # keep registration success independent of labeling
        outcome = {
            "plus_trial": "unknown",
            "plus_trial_detail": f"Plus 试用资格检测异常：{type(exc).__name__}",
            "plus_trial_evidence": "promo_campaign:error",
        }

    now = _now_iso()
    source = f"{address}.session.json" if address else "registration.session.json"
    target_record = next(
        (
            item
            for item in _inventory_records()
            if item.get("platform") == "chatgpt"
            and address
            and str(item.get("email") or "").strip().lower() == address.lower()
        ),
        None,
    )
    target_id = str((target_record or {}).get("id") or _stable_id("chatgpt", address, "registration"))
    if target_record:
        source = str(target_record.get("source") or source)
    public = {
        "platform": "chatgpt",
        "kind": "platform",
        "email": address,
        "email_provider": asset_store.classify_email_provider(address),
        "source": source,
        "id": target_id,
        "status": "normal",
        "detail": "ChatGPT 注册会话正常",
        "evidence": "chatgpt_session:registration",
        "checked_at": now,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "registration_country": str(data.get("registration_country") or "").strip().upper(),
        "network_node": str(data.get("network_node") or "").strip(),
    }
    public.update(outcome)

    cache = _read_cache()
    items = cache.get("items")
    if not isinstance(items, list):
        items = []
    replaced = False
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        same_id = str(item.get("id") or "") == target_id
        same_email = bool(address) and str(item.get("email") or "").strip().lower() == address.lower()
        if same_id or same_email:
            items[index] = {**item, **public, "id": str(item.get("id") or target_id)}
            replaced = True
            break
    if not replaced:
        items.append(public)
    cache["schema_version"] = max(2, int(cache.get("schema_version") or 0))
    cache["items"] = items
    cache["summary"] = _status_summary(items)
    _write_cache(cache)
    return public


def _claude_plan_type(payload: dict) -> str:
    """Return a conservative plan label from Claude's account payload."""
    if not isinstance(payload, dict):
        return "unknown"

    def normalize(value) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if "free" in text:
            return "free"
        for plan in ("max", "pro", "team", "enterprise"):
            if plan in text:
                return plan
        return text

    for key in ("plan", "plan_type", "subscription_type", "seat_tier"):
        plan = normalize(payload.get(key))
        if plan:
            return plan
    memberships = payload.get("memberships")
    memberships = memberships if isinstance(memberships, list) else []
    for membership in memberships:
        if not isinstance(membership, dict):
            continue
        plan = normalize(membership.get("seat_tier"))
        if plan:
            return plan
        organization = membership.get("organization")
        organization = organization if isinstance(organization, dict) else {}
        for key in ("plan", "plan_type", "subscription_type", "billing_type"):
            plan = normalize(organization.get(key))
            if plan:
                return plan
        if (
            str(organization.get("rate_limit_tier") or "").strip().lower()
            == "default_claude_ai"
            and not organization.get("billing_type")
        ):
            return "free"
    return "unknown"


def _scan_claude(record: dict, timeout: int) -> dict:
    cookies = record.get("_cookies") or []
    token = record.get("_token") or {}
    session_key = str(token.get("sessionKey") or "").strip()
    if not session_key:
        key_cookie = next((item for item in cookies if item.get("name") == "sessionKey" and item.get("value")), None)
        session_key = str((key_cookie or {}).get("value") or "").strip()
    if not session_key:
        return {"status": "invalid", "detail": "缺少 Claude sessionKey", "evidence": "local:missing_session_key"}
    with _web_session("claude") as session:
        response = session.get(
            "https://claude.ai/api/account",
            headers={"Cookie": f"sessionKey={session_key}"},
            timeout=timeout,
        )
    if "/login" in response.url or "/logout" in response.url:
        return {"status": "expired", "detail": "Claude 已跳转登录页", "evidence": "claude_account:login_redirect"}
    classified = _response_status(response, "claude_account")
    if classified:
        return classified
    if response.status_code == 200:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            memberships = payload.get("memberships")
            memberships = memberships if isinstance(memberships, list) else []
            has_identity = bool(
                payload.get("uuid") or payload.get("email") or payload.get("email_address")
            )
            if has_identity and memberships:
                plan_type = _claude_plan_type(payload)
                plan_label = "Free" if plan_type == "free" else plan_type.title()
                detail = (
                    f"Claude {plan_label} 登录会话正常"
                    if plan_type != "unknown"
                    else "Claude 登录会话正常（套餐未知）"
                )
                return {
                    "status": "normal",
                    "detail": detail,
                    "evidence": "claude_account:200",
                    "plan_type": plan_type,
                }
            if has_identity:
                return {
                    "status": "unknown",
                    "detail": "Claude 会话有效，但账号没有可用组织成员关系",
                    "evidence": "claude_account:no_membership",
                    "plan_type": "unknown",
                }
        text = (response.text or "").lower()
        if "just a moment" in text or "cf-chl" in text or "cloudflare" in text:
            return {"status": "restricted", "detail": "Claude 返回 Cloudflare 验证页", "evidence": "claude_account:challenge"}
        return {"status": "unknown", "detail": "Claude 未返回有效账号数据", "evidence": "claude_account:empty"}
    return {"status": "unknown", "detail": f"Claude HTTP {response.status_code}", "evidence": f"claude_account:{response.status_code}"}


def _grok_authorization_status(record: dict) -> str:
    """Resolve local Grok OAuth completion without exposing credentials."""
    token = record.get("_token") if isinstance(record.get("_token"), dict) else {}
    email = str(record.get("email") or token.get("email") or "").strip().lower()
    marker = asset_store._token_root() / "grok" / "uploaded_sub2api.txt"
    if email and marker.is_file():
        uploaded = {
            line.strip().lower()
            for line in marker.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        }
        if email in uploaded:
            return "authorized"
    status = str(token.get("authorization_status") or "").strip().lower()
    if status in {"authorized", "failed", "pending", "not_requested"}:
        return status
    return "unverified"


def _scan_grok(record: dict, timeout: int) -> dict:
    cookies = record.get("_cookies") or []
    token = record.get("_token") or {}
    sso = str(token.get("sso") or "").strip()
    if not sso:
        key_cookie = next(
            (item for item in cookies if item.get("name") in {"sso", "sso-rw"} and item.get("value")),
            None,
        )
        sso = str((key_cookie or {}).get("value") or "").strip()
    if not sso:
        return {"status": "invalid", "detail": "缺少 Grok SSO", "evidence": "local:missing_sso"}
    with _web_session("grok") as session:
        session.cookies.set("sso", sso, domain=".x.ai", path="/")
        session.cookies.set("sso-rw", sso, domain=".x.ai", path="/")
        response = session.get("https://accounts.x.ai/", timeout=timeout, allow_redirects=True)
    if "sign-in" in response.url or "sign-up" in response.url:
        return {"status": "expired", "detail": "Grok SSO 已跳转登录页", "evidence": "xai_account:login_redirect"}
    classified = _response_status(response, "xai_account")
    if classified:
        return classified
    if 200 <= response.status_code < 400:
        authorization = _grok_authorization_status(record)
        if authorization == "authorized":
            return {
                "status": "normal",
                "detail": "Grok SSO 与 OAuth 授权均正常",
                "evidence": f"xai_account:{response.status_code}:oauth_authorized",
            }
        if authorization == "failed":
            return {
                "status": "restricted",
                "detail": "Grok SSO 正常，但 OAuth 授权失败",
                "evidence": "local:grok_oauth_failed",
            }
        if authorization == "pending":
            return {
                "status": "unknown",
                "detail": "Grok SSO 正常，OAuth 授权尚未完成",
                "evidence": "local:grok_oauth_pending",
            }
        return {
            "status": "normal",
            "detail": (
                "Grok SSO 与 OAuth 授权均正常"
                if authorization == "authorized"
                else "Grok SSO 登录正常（未要求 OAuth 导入）"
            ),
            "evidence": (
                f"xai_account:{response.status_code}:oauth_authorized"
                if authorization == "authorized"
                else f"xai_account:{response.status_code}:sso_only"
            ),
        }
    return {"status": "unknown", "detail": f"Grok HTTP {response.status_code}", "evidence": f"xai_account:{response.status_code}"}


def _scan_kiro(record: dict, timeout: int) -> dict:
    token = record.get("_token") or {}
    refresh = str(token.get("refreshToken") or token.get("refresh_token") or "").strip()
    client_id = str(token.get("clientId") or token.get("client_id") or "").strip()
    client_secret = str(token.get("clientSecret") or token.get("client_secret") or "").strip()
    if not refresh or not client_id or not client_secret:
        return {"status": "invalid", "detail": "缺少 Kiro Builder ID 长期凭据", "evidence": "local:missing_credential"}
    with _web_session("kiro") as session:
        response = session.post(
            "https://oidc.us-east-1.amazonaws.com/token",
            json={"clientId": client_id, "clientSecret": client_secret, "refreshToken": refresh, "grantType": "refresh_token"},
            timeout=timeout,
        )
        classified = _response_status(response, "kiro_token")
        if classified:
            return classified
        try:
            payload = response.json()
        except Exception:
            payload = {}
        access = str(payload.get("accessToken") or "").strip()
        if response.status_code != 200 or not access:
            return {"status": "expired", "detail": "Kiro refresh token 未返回访问令牌", "evidence": "kiro_token:empty"}
        usage = session.get(
            "https://q.us-east-1.amazonaws.com/getUsageLimits?origin=AI_EDITOR&resourceType=AGENTIC_REQUEST&isEmailRequired=true",
            headers={"Authorization": f"Bearer {access}"}, timeout=timeout,
        )
        classified = _response_status(usage, "kiro_usage")
        if classified:
            return classified
        if usage.status_code == 200:
            return {"status": "normal", "detail": "Kiro Builder ID 凭据正常", "evidence": "kiro_usage:200"}
        return {"status": "unknown", "detail": f"Kiro usage HTTP {usage.status_code}", "evidence": f"kiro_usage:{usage.status_code}"}


_SCANNERS = {
    "outlook": _scan_outlook,
    "chatgpt": _scan_chatgpt,
    "claude": _scan_claude,
    "grok": _scan_grok,
    "kiro": _scan_kiro,
}


def _scan_record(
    record: dict,
    timeout: int,
    include_plus_trial: bool | None = None,
) -> dict:
    started = time.monotonic()
    public = _public_record(record)
    try:
        outcome = _SCANNERS[record["platform"]](record, timeout)
    except requests.Timeout:
        outcome = {"status": "error", "detail": "检测请求超时", "evidence": "network:timeout"}
    except requests.RequestException as exc:
        outcome = {"status": "error", "detail": f"网络检测失败：{type(exc).__name__}", "evidence": "network:error"}
    except Exception as exc:
        outcome = {"status": "error", "detail": f"检测异常：{str(exc)[:120]}", "evidence": "scanner:error"}
    if outcome.get("status") not in STATUSES:
        outcome["status"] = "unknown"
    access_token = str(outcome.pop("_access_token", "") or "")
    if record.get("platform") == "chatgpt" and "plus_trial" not in outcome:
        if outcome.get("status") == "normal":
            if include_plus_trial is False:
                outcome.update({
                    "plus_trial": "disabled",
                    "plus_trial_detail": "本次健康扫描未启用 Plus 资格检测",
                    "plus_trial_evidence": "scan:disabled",
                })
            else:
                outcome.update(_scan_chatgpt_plus_trial(record, access_token, timeout))
        else:
            outcome.update({
                "plus_trial": "unknown",
                "plus_trial_detail": "账号状态异常，未检测 Plus 试用资格",
                "plus_trial_evidence": "health:not_normal",
            })
    public.update(outcome)
    public["checked_at"] = outcome.get("checked_at") or _now_iso()
    public["latency_ms"] = round((time.monotonic() - started) * 1000)
    return public


def _record_with_outcome(record: dict, outcome: dict) -> dict:
    public = _public_record(record)
    public.update(outcome)
    public["checked_at"] = _now_iso()
    public["latency_ms"] = 0
    return public


def _scan_should_trip_breaker(result: dict) -> str:
    evidence = str(result.get("evidence") or "").lower()
    status = str(result.get("status") or "").lower()
    if evidence.endswith(":429") or ":429:" in evidence:
        return "rate_limited"
    if status == "restricted" and (
        evidence.endswith(":403") or evidence.endswith(":challenge")
    ):
        return "restricted"
    if status == "error" and evidence.startswith(("network:", "preflight:")):
        return "network"
    return ""


def _scan_platform_safely(
    platform: str,
    records: list[dict],
    timeout: int,
    min_interval: float,
    max_interval: float,
    on_result: Callable[[dict], None] | None = None,
    account_concurrency: int = 1,
    include_plus_trial: bool | None = None,
) -> list[dict]:
    results = []
    consecutive_risk = 0
    breaker_reason = ""
    account_concurrency = min(8, max(1, int(account_concurrency)))

    def delayed_scan(record: dict) -> dict:
        if max_interval > 0:
            time.sleep(random.uniform(min_interval, max_interval))
        if include_plus_trial is None:
            return _scan_record(record, timeout)
        return _scan_record(record, timeout, include_plus_trial)

    index = 0
    while index < len(records):
        if breaker_reason:
            for record in records[index:]:
                result = _record_with_outcome(record, {
                    "status": "error",
                    "detail": f"为降低风控，本次已暂停 {platform} 后续检测",
                    "evidence": f"safe_scan:circuit_breaker:{breaker_reason}",
                })
                results.append(result)
                if on_result:
                    on_result(result)
            break

        batch = records[index:index + account_concurrency]
        batch_results: list[dict | None] = [None] * len(batch)
        if len(batch) == 1:
            batch_results[0] = delayed_scan(batch[0])
            if on_result:
                on_result(batch_results[0])
        else:
            with ThreadPoolExecutor(
                max_workers=len(batch),
                thread_name_prefix=f"asset-scan-{platform}",
            ) as executor:
                futures = {
                    executor.submit(delayed_scan, record): offset
                    for offset, record in enumerate(batch)
                }
                for future in as_completed(futures):
                    result = future.result()
                    batch_results[futures[future]] = result
                    if on_result:
                        on_result(result)

        for result in batch_results:
            if result is None:
                continue
            results.append(result)
            risk = _scan_should_trip_breaker(result)
            if risk == "rate_limited":
                breaker_reason = risk
            elif risk:
                consecutive_risk += 1
                if consecutive_risk >= 2:
                    breaker_reason = risk
            else:
                consecutive_risk = 0
        index += len(batch)
    return results


def scan_pool(
    platforms: list[str] | tuple[str, ...] | None = None,
    concurrency: int = 1,
    account_concurrency: int = 1,
    timeout: int = 15,
    progress: Callable[[dict], None] | None = None,
    emails: list[str] | None = None,
    force: bool = False,
    include_plus_trial: bool | None = None,
) -> dict:
    requested = {str(item).strip().lower() for item in (platforms or PLATFORMS)}
    invalid = requested.difference(PLATFORMS)
    if invalid:
        raise ValueError(f"不支持的平台：{', '.join(sorted(invalid))}")
    # Concurrency controls independent platforms only. Accounts within one
    # platform use bounded batches so breaker decisions are applied before the
    # next batch is submitted.
    concurrency = min(2, max(1, int(concurrency)))
    account_concurrency = min(8, max(1, int(account_concurrency)))
    timeout = min(60, max(5, int(timeout)))
    cache_seconds = int(_env_number(
        "ASSET_SCAN_CACHE_SECONDS",
        SAFE_SCAN_DEFAULT_CACHE_SECONDS,
        0,
        7 * 24 * 60 * 60,
        int,
    ))
    min_interval = _env_number(
        "ASSET_SCAN_MIN_INTERVAL",
        SAFE_SCAN_DEFAULT_MIN_INTERVAL,
        0.0,
        60.0,
    )
    max_interval = _env_number(
        "ASSET_SCAN_MAX_INTERVAL",
        SAFE_SCAN_DEFAULT_MAX_INTERVAL,
        min_interval,
        120.0,
    )
    started_at = _now_iso()
    records = _inventory_records()
    selected = [record for record in records if record["platform"] in requested]
    if emails:
        email_set = {str(e).strip().lower() for e in emails}
        selected = [record for record in selected if (record.get("email") or "").strip().lower() in email_set]
    previous = {
        str(item.get("id")): item
        for item in _read_cache().get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    scanned = {}
    now = time.time()
    selected_to_scan = []
    for record in selected:
        cached = previous.get(str(record.get("id")))
        cache_age = now - _checked_at_epoch(cached or {})
        legacy_plus_result = (
            include_plus_trial is not False
            and record.get("platform") == "chatgpt"
            and cached
            and str(cached.get("plus_trial") or "") == "eligible"
        )
        legacy_grok_result = (
            record.get("platform") == "grok"
            and cached
            and str(cached.get("status") or "") == "normal"
            and _grok_authorization_status(record) in {"failed", "pending"}
        )
        if (
            not force
            and cache_seconds > 0
            and cached
            and 0 <= cache_age < cache_seconds
            and not legacy_plus_result
            and not legacy_grok_result
        ):
            scanned[str(record["id"])] = dict(cached)
        else:
            selected_to_scan.append(record)
    total = len(selected)
    if progress:
        progress({"completed": 0, "total": total, "current": ""})
    records_by_platform = {
        platform: [record for record in selected_to_scan if record["platform"] == platform]
        for platform in requested
    }
    preflight_failures = {}
    with ThreadPoolExecutor(
        max_workers=min(concurrency, max(1, len(records_by_platform))),
        thread_name_prefix="asset-scan-preflight",
    ) as executor:
        future_map = {
            executor.submit(_platform_preflight, platform, timeout): platform
            for platform, platform_records in records_by_platform.items()
            if platform_records
        }
        for future in as_completed(future_map):
            outcome = future.result()
            if outcome:
                preflight_failures[future_map[future]] = outcome

    completed = len(scanned)
    if progress and completed:
        progress({"completed": completed, "total": total, "current": "复用近期扫描结果"})
    pending = []
    for record in selected_to_scan:
        outcome = preflight_failures.get(record["platform"])
        if outcome:
            result = _record_with_outcome(record, outcome)
            scanned[result["id"]] = result
            completed += 1
            if progress:
                progress({
                    "completed": completed,
                    "total": total,
                    "current": result.get("email") or result.get("source") or "",
                })
        else:
            pending.append(record)
    pending_by_platform = {
        platform: [record for record in pending if record["platform"] == platform]
        for platform in requested
    }
    pending_by_platform = {
        platform: records for platform, records in pending_by_platform.items() if records
    }
    progress_lock = threading.Lock()

    def collect_result(result: dict):
        nonlocal completed
        with progress_lock:
            scanned[result["id"]] = result
            completed += 1
            if progress:
                progress({
                    "completed": completed,
                    "total": total,
                    "current": result.get("email") or result.get("source") or "",
                })

    with ThreadPoolExecutor(
        max_workers=min(concurrency, max(1, len(pending_by_platform))),
        thread_name_prefix="asset-scan-platform",
    ) as executor:
        future_map = {
            executor.submit(
                _scan_platform_safely,
                platform,
                records,
                timeout,
                min_interval,
                max_interval,
                collect_result,
                account_concurrency,
                include_plus_trial,
            ): platform
            for platform, records in pending_by_platform.items()
        }
        for future in as_completed(future_map):
            future.result()

    items = []
    for record in records:
        public = _public_record(record)
        result = scanned.get(public["id"]) or previous.get(public["id"])
        if result:
            public.update({
                key: result[key]
                for key in (
                    "status", "detail", "evidence", "checked_at", "latency_ms",
                    "plus_trial", "plus_trial_detail", "plus_trial_evidence", "plan_type",
                )
                if key in result
            })
        else:
            public.update({"status": "unknown", "detail": "尚未扫描", "evidence": "none", "checked_at": ""})
        if public.get("platform") == "chatgpt":
            public.setdefault("plus_trial", "unknown")
            public.setdefault("plus_trial_detail", "尚未检测 Plus 试用资格")
            public.setdefault("plus_trial_evidence", "none")
        items.append(public)
    finished_at = _now_iso()
    report = {
        "schema_version": 2,
        "started_at": started_at,
        "finished_at": finished_at,
        "platforms_scanned": sorted(requested),
        "safe_mode": {
            "enabled": True,
            "platform_concurrency": concurrency,
            "account_concurrency": account_concurrency,
            "min_interval_seconds": min_interval,
            "max_interval_seconds": max_interval,
            "cache_seconds": cache_seconds,
            "force": bool(force),
            "include_plus_trial": include_plus_trial is not False,
        },
        "items": items,
        "summary": _status_summary(items),
    }
    _write_cache(report)
    return report
