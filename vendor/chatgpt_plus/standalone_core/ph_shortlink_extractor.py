#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模式8独立提炼器

只保留模式8的两阶段短链流程：
  1. 代理池1：创建 custom checkout，并携带优惠参数。
  2. 代理池2：更新同一 checkout，激活优惠参数。
  3. 校验金额与 oaics_，输出 ChatGPT checkout 短链。

脚本不内置 Token、代理、服务器密钥或其他模式。

推荐依赖：
    pip install curl_cffi

备用依赖：
    pip install requests

直接运行会打开图形界面：
    python mode8_standalone.py

命令行示例：
    python mode8_standalone.py --token-file token.txt \
        --pool1-file pool1.txt --pool2-file pool2.txt
"""

from __future__ import annotations

import argparse
import json
import queue
import random
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

try:
    import requests as plain_requests
except Exception:
    plain_requests = None


CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHECKOUT_UPDATE_URL = "https://chatgpt.com/backend-api/payments/checkout/update"
CHECKOUT_TAXES_URL = "https://chatgpt.com/backend-api/payments/checkout/taxes"
COUPON_CHECK_URL = "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
APP_BASE = "https://chatgpt.com"
DEFAULT_TIMEOUT = 45

FINGERPRINT_PROFILES = [
    {
        "name": "mac_chrome149_m2",
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.7827.102 Safari/537.36"
        ),
        "major": "149",
        "full": "149.0.7827.102",
        "platform": "macOS",
        "platform_version": "15.7.0",
        "arch": "arm",
    },
    {
        "name": "mac_chrome148_intel",
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.7748.91 Safari/537.36"
        ),
        "major": "148",
        "full": "148.0.7748.91",
        "platform": "macOS",
        "platform_version": "14.7.2",
        "arch": "x86",
    },
    {
        "name": "win_chrome146",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.7423.118 Safari/537.36"
        ),
        "major": "146",
        "full": "146.0.7423.118",
        "platform": "Windows",
        "platform_version": "15.0.0",
        "arch": "x86",
    },
    {
        "name": "win_chrome145",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.7339.207 Safari/537.36"
        ),
        "major": "145",
        "full": "145.0.7339.207",
        "platform": "Windows",
        "platform_version": "19.0.0",
        "arch": "x86",
    },
]

HAR_PAYMENT_FINGERPRINT = {
    "name": "win_chrome150_har",
    "ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "major": "150",
    "full": "150.0.0.0",
    "platform": "Windows",
    "platform_version": "",
    "arch": "x86",
    "sec_ch_ua": (
        '"Not;A=Brand";v="8", "Chromium";v="150", '
        '"Google Chrome";v="150"'
    ),
    "accept_language": "zh-CN,zh;q=0.9",
    "oai_language": "zh-CN",
    "har_exact": "1",
}


class Mode8Error(RuntimeError):
    """模式8流程中的可读错误。"""


def _find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = _find_token(item)
            if token:
                return token
    elif isinstance(value, list):
        for item in value:
            token = _find_token(item)
            if token:
                return token
    return ""


def normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    if token.startswith(("{", "[")):
        try:
            token = _find_token(json.loads(token)) or token
        except json.JSONDecodeError:
            pass
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1].strip()
    chunks = [item for item in re.split(r"\s+", token) if item]
    for candidate in chunks:
        if candidate.count(".") == 2:
            return candidate
    return re.sub(r"\s+", "", token)


def normalize_proxy(raw: str) -> str:
    """
    接受以下格式：
      http://user:pass@host:port
      https://user:pass@host:port
      socks4://user:pass@host:port
      socks4a://user:pass@host:port
      socks5://user:pass@host:port
      socks5h://user:pass@host:port
      user:pass@host:port
      host:port
      host:port:user:pass
    """
    value = str(raw or "").strip().strip("\"'")
    if not value:
        return ""

    prefixed_four_part = re.match(
        r"^(?P<scheme>https?|socks4a?|socks5h?)://"
        r"(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<port>\d+):"
        r"(?P<user>[^:\s]+):(?P<password>.+)$",
        value,
        flags=re.IGNORECASE,
    )
    if prefixed_four_part:
        item = prefixed_four_part.groupdict()
        value = (
            f"{item['scheme'].lower()}://{item['user']}:{item['password']}"
            f"@{item['host']}:{item['port']}"
        )

    if "://" not in value:
        parts = value.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, username = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            value = f"http://{username}:{password}@{host}:{port}"
        else:
            value = f"http://{value}"

    scheme_match = re.match(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://", value, re.IGNORECASE)
    if not scheme_match:
        return ""
    scheme = scheme_match.group("scheme").lower()
    aliases = {"socks": "socks5h"}
    scheme = aliases.get(scheme, scheme)
    supported = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
    if scheme not in supported:
        raise ValueError(
            "代理协议不受支持；可用 HTTP、HTTPS、SOCKS4、SOCKS4A、SOCKS5、SOCKS5H"
        )
    return f"{scheme}://{value.split('://', 1)[1]}"


def split_proxy_pool(raw: str, limit: int = 500) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\r\n,;]+", str(raw or "")):
        candidate = normalize_proxy(part)
        if candidate and candidate not in seen:
            seen.add(candidate)
            items.append(candidate)
        if len(items) >= limit:
            break
    return items


def mask_proxy(proxy: str) -> str:
    if not proxy:
        return "直连"
    try:
        parsed = urlsplit(proxy)
        host = parsed.hostname or "custom"
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***@" if parsed.username else ""
        return f"{parsed.scheme or 'http'}://{auth}{host}{port}"
    except Exception:
        return "custom"


def resolve_proxy_placeholders(proxy: str) -> str:
    value = str(proxy or "")
    replacements = {
        "{sid}": uuid.uuid4().hex[:12],
        "{{sid}}": uuid.uuid4().hex[:12],
        "{ts}": str(int(time.time())),
        "{{ts}}": str(int(time.time())),
        "{ms}": str(int(time.time() * 1000)),
        "{{ms}}": str(int(time.time() * 1000)),
        "{uuid}": uuid.uuid4().hex,
        "{{uuid}}": uuid.uuid4().hex,
        "{rand}": str(random.randint(100000, 999999)),
        "{{rand}}": str(random.randint(100000, 999999)),
    }
    for marker, replacement in replacements.items():
        value = value.replace(marker, replacement)
    return value


def random_fingerprint() -> dict[str, str]:
    profile = dict(random.choice(FINGERPRINT_PROFILES))
    grease_short, grease_full = random.choice(
        [
            ('"Not_A Brand";v="24"', '"Not_A Brand";v="24.0.0.0"'),
            ('"Not.A/Brand";v="8"', '"Not.A/Brand";v="8.0.0.0"'),
        ]
    )
    major = profile["major"]
    full = profile["full"]
    profile["sec_ch_ua"] = (
        f'"Google Chrome";v="{major}", "Chromium";v="{major}", {grease_short}'
    )
    profile["sec_ch_ua_full_version_list"] = (
        f'"Google Chrome";v="{full}", "Chromium";v="{full}", {grease_full}'
    )
    return profile


def default_session_factory(impersonate: str = "chrome146") -> Any:
    if curl_requests is not None:
        session = curl_requests.Session(impersonate=impersonate or "chrome146")
    elif plain_requests is not None:
        session = plain_requests.Session()
    else:
        raise Mode8Error("缺少 HTTP 依赖，请执行：pip install curl_cffi")
    if hasattr(session, "trust_env"):
        session.trust_env = False
    return session


def build_session(
    token: str,
    proxy: str,
    session_factory: Callable[[], Any] = default_session_factory,
    fingerprint_profile: dict[str, str] | None = None,
    device_id: str = "",
) -> tuple[Any, str]:
    fingerprint = (
        dict(fingerprint_profile)
        if isinstance(fingerprint_profile, dict) and fingerprint_profile
        else random_fingerprint()
    )
    if session_factory is default_session_factory:
        session = default_session_factory(
            fingerprint.get("tls_impersonate", "chrome146")
        )
    else:
        session = session_factory()
    runtime_device_id = str(device_id or "").strip() or str(uuid.uuid4())
    headers = {
            "User-Agent": fingerprint["ua"],
            "Accept": "*/*",
            "Accept-Language": fingerprint.get(
                "accept_language", "en-US,en;q=0.9"
            ),
            "Authorization": f"Bearer {token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": runtime_device_id,
            "oai-language": fingerprint.get("oai_language", "en-US"),
            "sec-ch-ua": fingerprint["sec_ch_ua"],
            "sec-ch-ua-mobile": fingerprint.get("mobile", "?0"),
            "sec-ch-ua-platform": f'"{fingerprint["platform"]}"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "priority": "u=1, i",
        }
    if not fingerprint.get("har_exact"):
        headers.update(
            {
                "sec-ch-ua-arch": f'"{fingerprint["arch"]}"',
                "sec-ch-ua-bitness": f'"{fingerprint.get("bitness", "64")}"',
                "sec-ch-ua-model": '""',
                "sec-ch-ua-full-version": f'"{fingerprint["full"]}"',
                "sec-ch-ua-full-version-list": fingerprint[
                    "sec_ch_ua_full_version_list"
                ],
                "sec-ch-ua-platform-version": (
                    f'"{fingerprint["platform_version"]}"'
                ),
            }
        )
    session.headers.update(headers)
    if hasattr(session, "cookies"):
        try:
            session.cookies.set(
                "oai-did",
                runtime_device_id,
                domain=".chatgpt.com",
                path="/",
            )
        except Exception:
            pass
    runtime_proxy = resolve_proxy_placeholders(proxy)
    if runtime_proxy:
        session.proxies = {"http": runtime_proxy, "https": runtime_proxy}
    return session, fingerprint["name"]


def response_json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json() or {}
    except Exception as exc:
        text = str(getattr(response, "text", "") or "")
        raise Mode8Error(f"上游未返回 JSON：{text[:240]}") from exc
    if not isinstance(payload, dict):
        raise Mode8Error(f"上游 JSON 类型异常：{type(payload).__name__}")
    return payload


def summarize_http_error(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "").strip()
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
            return " / ".join(
                part
                for part in (
                    f"HTTP {status}",
                    f"code={code}" if code else "",
                    message[:300],
                )
                if part
            )
        detail = str(payload.get("detail") or payload.get("message") or "").strip()
        if detail:
            return f"HTTP {status} / {detail[:300]}"
    if re.search(r"<\s*(?:!doctype|html|head|body)\b", text, re.I):
        return f"HTTP {status} / 上游返回 HTML 拦截页"
    compact = re.sub(r"\s+", " ", text)[:300]
    return f"HTTP {status}{f' / {compact}' if compact else ''}"


_OAICS_ID_RE = re.compile(r"oaics_[A-Za-z0-9_-]{8,160}")
_STRIPE_ID_RE = re.compile(r"cs_(?:live|test)_[A-Za-z0-9_-]{6,512}|cs_[A-Za-z0-9_-]{6,512}")


def _extract_oaics_checkout_id(payload: Any, seen: set[int] | None = None) -> str:
    """Prefer the OpenAI internal session when a response also contains Stripe IDs."""
    if isinstance(payload, str):
        match = _OAICS_ID_RE.search(payload)
        return match.group(0) if match else ""
    if not isinstance(payload, (dict, list)):
        return ""
    visited = seen if seen is not None else set()
    marker = id(payload)
    if marker in visited:
        return ""
    visited.add(marker)
    values = payload.values() if isinstance(payload, dict) else payload
    for value in values:
        found = _extract_oaics_checkout_id(value, visited)
        if found:
            return found
    return ""


def extract_checkout_id(payload: Any) -> str:
    preferred = _extract_oaics_checkout_id(payload)
    if preferred:
        return preferred
    if isinstance(payload, dict):
        for key in ("checkout_session_id", "session_id", "id"):
            value = str(payload.get(key) or "").strip()
            if value.startswith(("oaics_", "cs_")):
                return value
        for key in ("checkout_session", "checkout", "session", "data", "raw"):
            value = payload.get(key)
            found = extract_checkout_id(value)
            if found:
                return found
        text = json.dumps(payload, ensure_ascii=False)
        match = _STRIPE_ID_RE.search(text)
        return match.group(0) if match else ""
    if isinstance(payload, list):
        for item in payload:
            found = extract_checkout_id(item)
            if found:
                return found
    return ""


def extract_processor_entity(payload: Any, country: str) -> str:
    if isinstance(payload, dict):
        direct = payload.get("processor_entity") or payload.get("processorEntity")
        if direct:
            return str(direct).strip()
        for key in ("checkout_session", "checkout", "session", "data", "raw"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                found = extract_processor_entity(nested, "")
                if found:
                    return found
    if not str(country or "").strip():
        return ""
    return "openai_llc" if str(country).upper() in {"US", "AU"} else "openai_ie"


def extract_publishable_key(payload: Any) -> str:
    """Extract the Stripe publishable key for the checkout handoff."""
    if isinstance(payload, dict):
        for key in ("publishable_key", "publishableKey"):
            value = str(payload.get(key) or "").strip()
            if value.startswith(("pk_live_", "pk_test_")):
                return value
        for value in payload.values():
            found = extract_publishable_key(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = extract_publishable_key(value)
            if found:
                return found
    return ""


def amount_state(payload: Any) -> tuple[Decimal, str]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    queue: list[tuple[str, Any]] = [("", payload)]
    while queue:
        path, value = queue.pop(0)
        if isinstance(value, dict):
            candidates.append((path, value))
            for key, nested in value.items():
                nested_path = f"{path}.{key}" if path else str(key)
                if isinstance(nested, (dict, list)):
                    queue.append((nested_path, nested))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                if isinstance(nested, (dict, list)):
                    queue.append((f"{path}[{index}]", nested))

    # Prefer payable totals anywhere in the response before looking at broader
    # totals or individual line items. Current checkout responses sometimes
    # wrap these fields several levels below data/checkout_session.
    for path, candidate in candidates:
        total_summary = candidate.get("total_summary")
        if isinstance(total_summary, dict) and total_summary.get("due") is not None:
            try:
                source = f"{path + '.' if path else ''}total_summary.due"
                return Decimal(str(total_summary["due"])), source
            except InvalidOperation:
                pass
        invoice = candidate.get("invoice")
        if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
            try:
                source = f"{path + '.' if path else ''}invoice.amount_due"
                return Decimal(str(invoice["amount_due"])), source
            except InvalidOperation:
                pass

    for path, candidate in candidates:
        for key in ("amount_due", "amount_total", "total"):
            if candidate.get(key) is not None:
                try:
                    source = f"{path + '.' if path else ''}{key}"
                    return Decimal(str(candidate[key])), source
                except InvalidOperation:
                    pass

    for path, candidate in candidates:
        line_items = candidate.get("line_items")
        if isinstance(line_items, list):
            values: list[Decimal] = []
            for item in line_items:
                if isinstance(item, dict) and item.get("amount") is not None:
                    try:
                        values.append(Decimal(str(item["amount"])))
                    except InvalidOperation:
                        pass
            if values:
                source = f"{path + '.' if path else ''}line_items.amount"
                return sum(values, Decimal("0")), source
    return Decimal("0"), ""


def compact_checkout_diagnostic(payload: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = payload if isinstance(payload, dict) else {}
    for key in ("checkout_session", "checkout", "session", "data", "raw"):
        nested = candidate.get(key)
        if isinstance(nested, dict) and nested:
            candidate = nested
            break
    billing = (
        candidate.get("billing_details")
        if isinstance(candidate.get("billing_details"), dict)
        else {}
    )
    promo = (
        candidate.get("promo_campaign")
        if isinstance(candidate.get("promo_campaign"), dict)
        else {}
    )
    amount, source = amount_state(payload)
    return {
        "id": extract_checkout_id(payload) or "missing",
        "billing": f"{billing.get('country') or ''}/{billing.get('currency') or ''}",
        "promo": str(promo.get("promo_campaign_id") or ""),
        "status": str(candidate.get("status") or candidate.get("checkout_state") or ""),
        "payment_status": str(candidate.get("payment_status") or ""),
        "methods": (
            candidate.get("payment_method_types")[:8]
            if isinstance(candidate.get("payment_method_types"), list)
            else []
        ),
        "amount": str(amount) if source else "unknown",
        "amount_source": source,
        "fields": sorted(candidate.keys())[:24],
    }


def is_retryable_status(status: int) -> bool:
    return status in {403, 408, 409, 425, 429, 500, 502, 503, 504}


def is_retryable_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "proxy",
        "connect",
        "connection",
        "timed out",
        "timeout",
        "ssl",
        "tls",
        "reset",
        "remote end closed",
        "failed to perform",
    )
    return any(marker in text for marker in markers)


@dataclass
class Mode8Config:
    token: str
    country: str = "PH"
    currency: str = "PHP"
    promo_campaign: str = "plus-1-month-free"
    require_oaics: bool = True
    reject_nonzero: bool = True
    timeout: int = DEFAULT_TIMEOUT
    diagnose_coupon: bool = True
    fingerprint_profile: dict[str, str] | None = None
    device_id: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    web_deployment_attestation: str = ""
    checkout_sentinel_token: str = ""
    checkout_telemetry: str = ""
    preflight: bool = False


class Mode8Extractor:
    def __init__(
        self,
        config: Mode8Config,
        *,
        logger: Callable[[str], None] | None = None,
        session_factory: Callable[[], Any] = default_session_factory,
    ) -> None:
        self.config = config
        self.log = logger or print
        self.session_factory = session_factory

    def _session(self, proxy: str) -> Any:
        session, fingerprint = build_session(
            normalize_access_token(self.config.token),
            proxy,
            self.session_factory,
            self.config.fingerprint_profile,
            self.config.device_id,
        )
        self._hydrate_payment_metadata(session)
        self.log(f"纯 HTTP 指纹档案：{fingerprint}")
        return session

    def _hydrate_payment_metadata(self, session: Any) -> dict[str, str]:
        cfg = self.config
        metadata = {
            "client_build_number": str(cfg.client_build_number or "").strip(),
            "client_version": str(cfg.client_version or "").strip(),
            "session_id": str(cfg.oai_session_id or "").strip(),
            "attestation": str(cfg.web_deployment_attestation or "").strip(),
        }
        if not all(metadata.values()):
            try:
                response = session.get(
                    f"{APP_BASE}/",
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Content-Type": None,
                        "Origin": None,
                        "Referer": f"{APP_BASE}/",
                    },
                    timeout=min(cfg.timeout, 30),
                )
                if int(getattr(response, "status_code", 0) or 0) < 400:
                    html = str(getattr(response, "text", "") or "")
                    patterns = {
                        "client_version": r'data-build="([^"]+)"',
                        "client_build_number": r'data-seq="([^"]+)"',
                        "attestation": r'"webDeploymentAttestation":"([^"]+)"',
                        "session_id": r'"sessionId":"([0-9a-fA-F-]{36})"',
                    }
                    for key, pattern in patterns.items():
                        match = re.search(pattern, html)
                        if match:
                            metadata[key] = match.group(1)
            except Exception as exc:
                self.log(
                    f"payment bootstrap metadata skipped: {type(exc).__name__}"
                )
                if self.config.preflight and is_retryable_network_error(exc):
                    raise Mode8Error(
                        "预检提链节点连通性失败：ChatGPT 首页请求超时或连接被重置"
                    ) from exc

        metadata["session_id"] = metadata["session_id"] or str(uuid.uuid4())
        session.headers.update(
            {
                key: value
                for key, value in {
                    "OAI-Client-Build-Number": metadata["client_build_number"],
                    "OAI-Client-Version": metadata["client_version"],
                    "OAI-Session-Id": metadata["session_id"],
                }.items()
                if value
            }
        )
        try:
            setattr(session, "_oai_payment_attestation", metadata["attestation"])
        except Exception:
            pass
        cfg.client_build_number = metadata["client_build_number"]
        cfg.client_version = metadata["client_version"]
        cfg.oai_session_id = metadata["session_id"]
        cfg.web_deployment_attestation = metadata["attestation"]
        return metadata

    def _payment_headers(
        self,
        session: Any,
        headers: dict[str, Any],
        *,
        include_checkout_sentinel: bool = False,
    ) -> dict[str, Any]:
        output = dict(headers)
        attestation = str(
            getattr(session, "_oai_payment_attestation", "") or ""
        ).strip()
        if attestation:
            output["OAI-Web-Deployment-Attestation"] = attestation
        if include_checkout_sentinel:
            token = str(self.config.checkout_sentinel_token or "").strip()
            telemetry = str(self.config.checkout_telemetry or "").strip()
            if token:
                output["OpenAI-Sentinel-Token"] = token
                output["OAI-Telemetry"] = telemetry or "[1,null]"
        return output

    def _coupon_diagnostic(self, session: Any) -> dict[str, Any]:
        campaign = self.config.promo_campaign.strip()
        if not campaign:
            return {"campaign": "none", "checked": False}
        try:
            response = session.get(
                COUPON_CHECK_URL,
                params={
                    "coupon": campaign,
                    "is_coupon_from_query_param": "true",
                },
                headers={
                    "Referer": "https://chatgpt.com/",
                    "x-openai-target-path": "/backend-api/promo_campaign/check_coupon",
                    "x-openai-target-route": "/backend-api/promo_campaign/check_coupon",
                },
                timeout=self.config.timeout,
            )
            result: dict[str, Any] = {
                "campaign": campaign,
                "checked": True,
                "http_status": int(response.status_code),
            }
            if response.status_code < 400:
                data = response_json(response)
                redemption = (
                    data.get("redemption")
                    if isinstance(data.get("redemption"), dict)
                    else {}
                )
                result.update(
                    {
                        "state": str(data.get("state") or ""),
                        "redeemed": redemption.get("redeemed"),
                        "redeemed_by_user": redemption.get("redeemed_by_user"),
                    }
                )
            return result
        except Exception as exc:
            return {
                "campaign": campaign,
                "checked": False,
                "error": type(exc).__name__,
            }

    def _create_checkout(self, session: Any) -> dict[str, Any]:
        cfg = self.config
        body: dict[str, Any] = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {
                "country": cfg.country.upper(),
                "currency": cfg.currency.upper(),
            },
            "cancel_url": (
                f"{APP_BASE}/?promo_campaign="
                f"{cfg.promo_campaign.strip() or 'plus-1-month-free'}#pricing"
            ),
            "checkout_ui_mode": "custom",
            "locale": "zh-CN",
        }
        response = session.post(
            CHECKOUT_URL,
            json=body,
            headers=self._payment_headers(
                session,
                {
                    "Referer": "https://chatgpt.com/",
                    "x-openai-target-path": "/backend-api/payments/checkout",
                    "x-openai-target-route": "/backend-api/payments/checkout",
                },
                include_checkout_sentinel=True,
            ),
            timeout=cfg.timeout,
        )
        if response.status_code >= 400:
            raise Mode8Error(
                f"阶段1 checkout create failed：{summarize_http_error(response)}"
            )
        return response_json(response)

    def _update_checkout(
        self,
        session: Any,
        checkout_id: str,
        processor_entity: str,
    ) -> dict[str, Any]:
        cfg = self.config
        body = {
            "checkout_session_id": checkout_id,
            "processor_entity": processor_entity,
            "plan_name": "chatgptplusplan",
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": cfg.promo_campaign.strip(),
                "is_coupon_from_query_param": False,
            },
        }
        response = session.post(
            CHECKOUT_UPDATE_URL,
            json=body,
            headers=self._payment_headers(
                session,
                {
                    "Origin": "https://chatgpt.com",
                    "Referer": (
                        f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}"
                    ),
                    "x-openai-target-path": "/backend-api/payments/checkout/update",
                    "x-openai-target-route": "/backend-api/payments/checkout/update",
                },
            ),
            timeout=cfg.timeout,
        )
        if response.status_code >= 400:
            error = Mode8Error(
                f"阶段2 checkout update failed：{summarize_http_error(response)}"
            )
            setattr(error, "http_status", int(response.status_code))
            raise error
        return response_json(response)

    def _checkout_taxes(
        self,
        session: Any,
        checkout_id: str,
        processor_entity: str,
    ) -> dict[str, Any]:
        cfg = self.config
        response = session.post(
            CHECKOUT_TAXES_URL,
            json={
                "checkout_session_id": checkout_id,
                "processor_entity": processor_entity,
                "billing_country": cfg.country.upper(),
                "billing_currency": cfg.currency.upper(),
                "currency": cfg.currency.upper(),
            },
            headers=self._payment_headers(
                session,
                {
                    "Origin": "https://chatgpt.com",
                    "Referer": (
                        f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}"
                    ),
                    "x-openai-target-path": "/backend-api/payments/checkout/taxes",
                    "x-openai-target-route": "/backend-api/payments/checkout/taxes",
                },
            ),
            timeout=cfg.timeout,
        )
        if response.status_code >= 400:
            raise Mode8Error(
                f"checkout taxes failed：{summarize_http_error(response)}"
            )
        return response_json(response)

    def run(self, pool1_text: str = "", pool2_text: str = "") -> dict[str, Any]:
        token = normalize_access_token(self.config.token)
        if not token:
            raise Mode8Error("Access Token 为空")
        self.config.token = token

        pool1 = split_proxy_pool(pool1_text)
        pool2 = split_proxy_pool(pool2_text)
        stage1_proxy = random.choice(pool1) if pool1 else ""
        self.log("模式8两阶段流程开始")
        self.log(
            f"配置：country={self.config.country.upper()}, "
            f"currency={self.config.currency.upper()}, "
            f"promo={self.config.promo_campaign or 'none'}, ui=custom"
        )
        self.log(f"[阶段1/2] 建单出口：{mask_proxy(stage1_proxy)}")

        stage1_session = self._session(stage1_proxy)
        coupon = (
            self._coupon_diagnostic(stage1_session)
            if self.config.diagnose_coupon
            else {"campaign": self.config.promo_campaign.strip() or "none", "checked": False}
        )
        self.log(
            "[阶段1/2] 优惠资格诊断："
            + json.dumps(coupon, ensure_ascii=False, separators=(",", ":"))
        )
        create_payload = self._create_checkout(stage1_session)
        checkout_id = extract_checkout_id(create_payload)
        if not checkout_id:
            raise Mode8Error(
                "阶段1返回中没有 checkout_session_id："
                + json.dumps(create_payload, ensure_ascii=False)[:500]
            )
        if self.config.require_oaics and not checkout_id.startswith("oaics_"):
            raise Mode8Error(
                f"基线 Checkout 未返回 oaics_*；仅发现 {checkout_id[:20]}...，"
                "已停止向错误提供方会话应用优惠。"
            )
        processor_entity = extract_processor_entity(
            create_payload, self.config.country
        )
        self.log(
            f"[阶段1/2] 建单成功：id={checkout_id[:24]}..., "
            f"processor={processor_entity}"
        )
        self.log(
            "[阶段1/2] 返回摘要："
            + json.dumps(
                compact_checkout_diagnostic(create_payload),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        update_payload: dict[str, Any] = {}
        stage2_proxy = ""
        validation_session = stage1_session
        campaign = self.config.promo_campaign.strip()
        if campaign:
            candidates = list(pool2) if pool2 else [""]
            if stage1_proxy in candidates:
                candidates.remove(stage1_proxy)
                candidates.insert(0, stage1_proxy)
            elif len(candidates) > 1:
                start = random.randrange(len(candidates))
                candidates = candidates[start:] + candidates[:start]
            candidates = candidates[:4]
            last_error: Exception | None = None
            for index, candidate in enumerate(candidates, start=1):
                stage2_proxy = candidate
                self.log(
                    f"[阶段2/2] 激活优惠，第 {index}/{len(candidates)} 次，"
                    f"出口={mask_proxy(candidate)}"
                )
                try:
                    stage2_session = (
                        stage1_session if candidate == stage1_proxy else self._session(candidate)
                    )
                    validation_session = stage2_session
                    update_payload = self._update_checkout(
                        stage2_session, checkout_id, processor_entity
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    status = int(getattr(exc, "http_status", 0) or 0)
                    can_retry = (
                        index < len(candidates)
                        and (
                            is_retryable_status(status)
                            or is_retryable_network_error(exc)
                        )
                    )
                    self.log(f"[阶段2/2] 本次失败：{exc}")
                    if not can_retry:
                        raise
            if last_error is not None:
                raise last_error

            updated_id = extract_checkout_id(update_payload)
            if updated_id:
                checkout_id = updated_id
            updated_entity = extract_processor_entity(
                update_payload, self.config.country
            )
            if updated_entity:
                processor_entity = updated_entity
            self.log(
                f"[阶段2/2] 优惠更新成功：id={checkout_id[:24]}..."
            )
            self.log(
                "[阶段2/2] 返回摘要："
                + json.dumps(
                    compact_checkout_diagnostic(update_payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            self.log("[阶段2/2] promo 为空，跳过优惠更新")

        amount_payload = update_payload or create_payload
        amount, amount_source = amount_state(amount_payload)
        if not amount_source:
            taxes_payload = self._checkout_taxes(
                validation_session,
                checkout_id,
                processor_entity,
            )
            if taxes_payload:
                amount_payload = taxes_payload
                amount, amount_source = amount_state(amount_payload)
        if amount_source:
            self.log(f"金额校验：amount={amount}, source={amount_source}")
            if self.config.reject_nonzero and amount != 0:
                raise Mode8Error(
                    f"零元校验失败：amount={amount}, source={amount_source}"
                )
        else:
            self.log("金额校验：上游没有返回可验证的金额字段")
            if self.config.reject_nonzero:
                raise Mode8Error(
                    "金额校验失败：上游没有返回可验证的金额字段"
                )

        if self.config.require_oaics and not checkout_id.startswith("oaics_"):
            raise Mode8Error(
                f"短链类型校验失败：返回 {checkout_id[:20]}...，期望 oaics_"
            )

        result_url = (
            f"https://chatgpt.com/checkout/{processor_entity}/{checkout_id}"
        )
        result = {
            "ok": True,
            "checkout_id": checkout_id,
            "processor_entity": processor_entity,
            "country": self.config.country.upper(),
            "currency": self.config.currency.upper(),
            "promo_campaign": campaign or "none",
            "amount": str(amount) if amount_source else "unknown",
            "amount_source": amount_source,
            "stage1_proxy": mask_proxy(stage1_proxy),
            "stage2_proxy": mask_proxy(stage2_proxy),
            "url": result_url,
            # Internal handoff only. The app adapter removes this before JSON output.
            "_publishable_key": extract_publishable_key(update_payload)
            or extract_publishable_key(create_payload),
        }
        self.log(f"模式8结果：{result_url}")
        return result


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}

    def get(self, url: str, **_: Any) -> _FakeResponse:
        if url == COUPON_CHECK_URL:
            return _FakeResponse(
                200,
                {
                    "state": "eligible",
                    "redemption": {"redeemed": False},
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        body = kwargs.get("json") or {}
        if url == CHECKOUT_URL:
            assert body["checkout_ui_mode"] == "custom"
            assert "promo_campaign" not in body
            return _FakeResponse(
                200,
                {
                    "checkout_session_id": "cs_live_fixture123456",
                    "custom_checkout": {"id": "oaics_fixture123456789"},
                    "processor_entity": "openai_ie",
                    "billing_details": {"country": "PH", "currency": "PHP"},
                    "payment_method_types": ["card", "link"],
                    "status": "open",
                },
            )
        if url == CHECKOUT_UPDATE_URL:
            assert body["checkout_session_id"] == "oaics_fixture123456789"
            assert body["promo_campaign"]["promo_campaign_id"] == (
                "plus-1-month-free"
            )
            assert "billing_details" not in body
            assert "checkout_ui_mode" not in body
            return _FakeResponse(
                200,
                {
                    "checkout_session_id": "cs_live_fixture123456",
                    "custom_checkout": {"id": "oaics_fixture123456789"},
                    "processor_entity": "openai_ie",
                    "billing_details": {"country": "PH", "currency": "PHP"},
                    "total_summary": {"due": 0},
                    "payment_method_types": ["card", "link"],
                    "status": "open",
                },
            )
        raise AssertionError(f"unexpected POST {url}")


def run_self_test() -> None:
    logs: list[str] = []
    extractor = Mode8Extractor(
        Mode8Config(token="header.payload.signature"),
        logger=logs.append,
        session_factory=_FakeSession,
    )
    result = extractor.run("", "")
    expected = (
        "https://chatgpt.com/checkout/openai_ie/oaics_fixture123456789"
    )
    assert result["url"] == expected, result
    assert result["amount"] == "0", result
    assert result["amount_source"] == "total_summary.due", result
    assert split_proxy_pool(
        "host:8080:user:pass\nhttp://host2:8081"
    ) == [
        "http://user:pass@host:8080",
        "http://host2:8081",
    ]
    assert normalize_access_token(
        '{"accessToken":"header.payload.signature"}'
    ) == "header.payload.signature"
    print("SELF-TEST PASS")
    print(expected)


def _read_text_file(path: str) -> str:
    if not path:
        return ""
    return Path(path).expanduser().read_text(encoding="utf-8-sig")


def run_cli(args: argparse.Namespace) -> int:
    token = args.token or _read_text_file(args.token_file)
    pool1 = args.pool1 or _read_text_file(args.pool1_file)
    pool2 = args.pool2 or _read_text_file(args.pool2_file)
    config = Mode8Config(
        token=token,
        country=args.country,
        currency=args.currency,
        promo_campaign=args.promo,
        require_oaics=not args.allow_cs,
        reject_nonzero=not args.allow_nonzero,
        timeout=args.timeout,
    )
    try:
        result = Mode8Extractor(config).run(pool1, pool2)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def launch_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except Exception as exc:
        raise Mode8Error(f"Tkinter 不可用：{exc}") from exc

    class Mode8App(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("模式8独立提炼器")
            self.geometry("960x780")
            self.minsize(820, 680)
            self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
            self.running = False

            self.country_var = tk.StringVar(value="PH")
            self.currency_var = tk.StringVar(value="PHP")
            self.promo_var = tk.StringVar(value="plus-1-month-free")
            self.require_oaics_var = tk.BooleanVar(value=True)
            self.reject_nonzero_var = tk.BooleanVar(value=True)
            self.result_var = tk.StringVar()
            self.status_var = tk.StringVar(value="就绪")

            self.columnconfigure(0, weight=1)
            self.rowconfigure(4, weight=1)

            title = ttk.Label(
                self,
                text="模式8 · 两阶段独立提炼",
                font=("Microsoft YaHei UI", 16, "bold"),
            )
            title.grid(row=0, column=0, sticky="w", padx=16, pady=(14, 4))
            ttk.Label(
                self,
                text=(
                    "阶段1使用代理池1创建 checkout；阶段2使用代理池2更新同一 "
                    "checkout 并激活优惠。脚本没有内置代理。"
                ),
            ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 10))

            form = ttk.Frame(self, padding=(16, 0))
            form.grid(row=2, column=0, sticky="ew")
            form.columnconfigure(1, weight=1)
            form.columnconfigure(3, weight=1)

            ttk.Label(form, text="国家").grid(row=0, column=0, sticky="w")
            ttk.Entry(form, textvariable=self.country_var, width=10).grid(
                row=0, column=1, sticky="w", padx=(8, 20)
            )
            ttk.Label(form, text="货币").grid(row=0, column=2, sticky="w")
            ttk.Entry(form, textvariable=self.currency_var, width=10).grid(
                row=0, column=3, sticky="w", padx=(8, 20)
            )
            ttk.Label(form, text="优惠参数").grid(row=0, column=4, sticky="w")
            ttk.Entry(form, textvariable=self.promo_var, width=28).grid(
                row=0, column=5, sticky="ew", padx=(8, 0)
            )

            ttk.Label(form, text="Access Token").grid(
                row=1, column=0, sticky="nw", pady=(12, 0)
            )
            self.token_text = scrolledtext.ScrolledText(
                form, height=4, wrap="word", font=("Consolas", 9)
            )
            self.token_text.grid(
                row=1, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(12, 0)
            )

            pools = ttk.Frame(self, padding=(16, 12))
            pools.grid(row=3, column=0, sticky="nsew")
            pools.columnconfigure(0, weight=1)
            pools.columnconfigure(1, weight=1)

            left = ttk.LabelFrame(
                pools, text="代理池1（阶段1：创建 checkout）", padding=8
            )
            right = ttk.LabelFrame(
                pools, text="代理池2（阶段2：激活优惠）", padding=8
            )
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
            left.columnconfigure(0, weight=1)
            right.columnconfigure(0, weight=1)

            ttk.Label(left, text="每行一个；留空则该阶段直连").grid(
                row=0, column=0, sticky="w"
            )
            ttk.Label(right, text="每行一个；失败时最多轮换 4 个").grid(
                row=0, column=0, sticky="w"
            )
            self.pool1_text = scrolledtext.ScrolledText(
                left, height=7, wrap="none", font=("Consolas", 9)
            )
            self.pool2_text = scrolledtext.ScrolledText(
                right, height=7, wrap="none", font=("Consolas", 9)
            )
            self.pool1_text.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
            self.pool2_text.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

            output = ttk.Frame(self, padding=(16, 0, 16, 16))
            output.grid(row=4, column=0, sticky="nsew")
            output.columnconfigure(0, weight=1)
            output.rowconfigure(3, weight=1)

            options = ttk.Frame(output)
            options.grid(row=0, column=0, sticky="ew")
            ttk.Checkbutton(
                options,
                text="只接受 oaics_ 短链",
                variable=self.require_oaics_var,
            ).pack(side="left")
            ttk.Checkbutton(
                options,
                text="检测到非0金额时停止",
                variable=self.reject_nonzero_var,
            ).pack(side="left", padx=(18, 0))
            self.start_button = ttk.Button(
                options, text="开始提炼", command=self.start
            )
            self.start_button.pack(side="right")

            result_row = ttk.Frame(output)
            result_row.grid(row=1, column=0, sticky="ew", pady=(10, 8))
            result_row.columnconfigure(1, weight=1)
            ttk.Label(result_row, text="结果链接").grid(row=0, column=0)
            ttk.Entry(
                result_row, textvariable=self.result_var, state="readonly"
            ).grid(row=0, column=1, sticky="ew", padx=8)
            ttk.Button(result_row, text="复制", command=self.copy_result).grid(
                row=0, column=2
            )

            ttk.Label(output, textvariable=self.status_var).grid(
                row=2, column=0, sticky="w"
            )
            self.log_text = scrolledtext.ScrolledText(
                output,
                height=12,
                wrap="word",
                state="disabled",
                font=("Consolas", 9),
            )
            self.log_text.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
            self.after(100, self.drain_events)

        def append_log(self, line: str) -> None:
            timestamp = time.strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{timestamp}] {line}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def start(self) -> None:
            if self.running:
                return
            token = self.token_text.get("1.0", "end").strip()
            if not normalize_access_token(token):
                messagebox.showerror("模式8", "请先填写 Access Token")
                return
            self.running = True
            self.start_button.configure(state="disabled")
            self.result_var.set("")
            self.status_var.set("运行中")
            config = Mode8Config(
                token=token,
                country=self.country_var.get().strip() or "PH",
                currency=self.currency_var.get().strip() or "PHP",
                promo_campaign=self.promo_var.get().strip(),
                require_oaics=self.require_oaics_var.get(),
                reject_nonzero=self.reject_nonzero_var.get(),
            )
            pool1 = self.pool1_text.get("1.0", "end")
            pool2 = self.pool2_text.get("1.0", "end")

            def worker() -> None:
                try:
                    result = Mode8Extractor(
                        config,
                        logger=lambda line: self.events.put(("log", line)),
                    ).run(pool1, pool2)
                    self.events.put(("success", result))
                except Exception as exc:
                    self.events.put(("error", f"{type(exc).__name__}: {exc}"))

            threading.Thread(target=worker, daemon=True).start()

        def drain_events(self) -> None:
            try:
                while True:
                    kind, payload = self.events.get_nowait()
                    if kind == "log":
                        self.append_log(str(payload))
                    elif kind == "success":
                        self.running = False
                        self.start_button.configure(state="normal")
                        self.result_var.set(str(payload["url"]))
                        self.status_var.set("成功")
                        self.append_log("提炼完成")
                    elif kind == "error":
                        self.running = False
                        self.start_button.configure(state="normal")
                        self.status_var.set("失败")
                        self.append_log(str(payload))
                        messagebox.showerror("模式8提炼失败", str(payload))
            except queue.Empty:
                pass
            self.after(100, self.drain_events)

        def copy_result(self) -> None:
            value = self.result_var.get().strip()
            if not value:
                return
            self.clipboard_clear()
            self.clipboard_append(value)
            self.status_var.set("结果已复制")

    Mode8App().mainloop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="模式8独立两阶段短链提炼器")
    parser.add_argument("--token", default="", help="Access Token")
    parser.add_argument("--token-file", default="", help="Token 文件")
    parser.add_argument("--pool1", default="", help="代理池1，多项用逗号分隔")
    parser.add_argument("--pool1-file", default="", help="代理池1文件，每行一个")
    parser.add_argument("--pool2", default="", help="代理池2，多项用逗号分隔")
    parser.add_argument("--pool2-file", default="", help="代理池2文件，每行一个")
    parser.add_argument("--country", default="PH")
    parser.add_argument("--currency", default="PHP")
    parser.add_argument("--promo", default="plus-1-month-free")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--allow-cs",
        action="store_true",
        help="允许 cs_，不强制 oaics_",
    )
    parser.add_argument(
        "--allow-nonzero",
        action="store_true",
        help="金额非0时仍返回链接",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="运行离线自测，不访问网络",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.self_test:
        run_self_test()
        return 0
    cli_requested = any(
        (
            args.token,
            args.token_file,
            args.pool1,
            args.pool1_file,
            args.pool2,
            args.pool2_file,
        )
    )
    if cli_requested:
        return run_cli(args)
    launch_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
