"""
SMS activation API helpers for GoPay protocol flows.

The live worker uses SMSBower-style ``handler_api.php`` endpoints.  Older
deployments named the same config ``OPAI_HEROSMS_*``; keep those names as
aliases so existing launch scripts still work while the canonical config is
``OPAI_SMSBOWER_*``.
"""
from __future__ import annotations

import logging
import json
import os
import re
import time
from pathlib import Path

import tls_client

log = logging.getLogger(__name__)

SMSBOWER_HANDLER_PATH = "/stubs/handler_api.php"
SMS_TIMEOUT = 120
_SMS_ENV_PREFIXES = ("OPAI_SMSBOWER_", "OPAI_HEROSMS_")


def load_selected_env_file(prefixes: tuple[str, ...], path: str = "") -> None:
    """Load selected KEY=VALUE entries without requiring python-dotenv."""
    configured = (path or os.environ.get("OPAI_GOPAY_SMS_ENV_FILE", "")).strip()
    candidates = [Path(configured).expanduser()] if configured else [
        Path.cwd() / "config" / "sms.env",
        Path(__file__).resolve().parents[4] / "config" / "sms.env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            with env_path.open(encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key.startswith(prefixes) and not os.environ.get(key):
                        os.environ[key] = value
        except OSError as exc:
            log.debug("Could not load SMS env file %s: %s", env_path, exc)
        if configured:
            break


def _load_env_file(path: str = "") -> None:
    load_selected_env_file(_SMS_ENV_PREFIXES, path)


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value
    return default


def get_sms_api_key(api_key: str = "") -> str:
    _load_env_file()
    if api_key:
        return api_key
    key = _env_first("OPAI_SMSBOWER_API_KEY", "OPAI_HEROSMS_API_KEY")
    if key:
        return key
    key_file = _env_first("OPAI_SMSBOWER_API_KEY_FILE", "OPAI_HEROSMS_API_KEY_FILE")
    if key_file and os.path.exists(key_file):
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("Could not read SMS API key file %s: %s", key_file, exc)
    return ""


def sms_api_base_url() -> str:
    _load_env_file()
    return _env_first(
        "OPAI_SMSBOWER_API_BASE_URL",
        "OPAI_HEROSMS_API_BASE_URL",
        default="https://smsbower.page",
    ).rstrip("/")


def sms_api_url() -> str:
    """Return the configured handler endpoint without duplicating its path."""
    base = sms_api_base_url()
    if base.lower().endswith("handler_api.php"):
        return base
    return f"{base}{SMSBOWER_HANDLER_PATH}"


def _json_response_payload(response: str) -> dict | None:
    try:
        value = json.loads(response)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _json_values(payload: object):
    """Yield scalar values from the provider's occasionally nested JSON body."""
    if isinstance(payload, dict):
        for value in payload.values():
            yield from _json_values(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _json_values(value)
    elif payload is not None:
        yield str(payload)


def sms_api(api_key: str, action: str, params: dict | None = None, retries: int = 3) -> str:
    api_key = get_sms_api_key(api_key)
    p = {"api_key": api_key, "action": action}
    if params:
        p.update(params)
    for i in range(1, retries + 1):
        try:
            s = tls_client.Session(client_identifier="chrome_120")
            r = s.get(sms_api_url(), params=p, timeout_seconds=30)
            body = str(getattr(r, "text", "") or "").strip()
            status_code = int(getattr(r, "status_code", 200) or 200)
            if 200 <= status_code < 300:
                return body
            if status_code not in {408, 425, 429} and status_code < 500:
                raise RuntimeError(f"HTTP {status_code}: {body[:200]}")
            raise RuntimeError(f"HTTP {status_code}: {body[:200]}")
        except Exception as e:
            log.warning("sms_api %s attempt %d/%d failed: %s", action, i, retries, e)
            if i < retries:
                time.sleep(3)
    raise RuntimeError(f"sms_api {action} failed after {retries} retries") from e


def sms_get_number(api_key: str) -> tuple[str | None, str | None]:
    _load_env_file()
    service = _env_first("OPAI_SMSBOWER_SERVICE", "OPAI_HEROSMS_SERVICE", default="ni")
    country = _env_first("OPAI_SMSBOWER_COUNTRY", "OPAI_HEROSMS_COUNTRY", default="6")
    resp = sms_api(api_key, "getNumber", {"service": service, "country": country})
    log.info("getNumber: %s", resp)
    if resp.startswith("ACCESS_NUMBER:"):
        parts = resp.split(":", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            number = parts[2].strip()
            return number if number.startswith("+") else f"+{number}", parts[1].strip()
    payload = _json_response_payload(resp)
    if payload:
        data = payload.get("data", payload)
        values = list(_json_values(data))
        aid = next((v for v in values if re.fullmatch(r"\d+", v)), "")
        number = next((v for v in values if re.search(r"\d{7,}", v) and v != aid), "")
        if aid and number:
            return number if number.startswith("+") else f"+{number}", aid
    log.warning("getNumber failed: %s", resp)
    return None, None


def sms_wait_code(
    api_key: str,
    aid: str,
    timeout: int = SMS_TIMEOUT,
    *,
    ignore_code: str = "",
) -> str | None:
    """Poll one activation while filtering stale OTPs from ``setStatus=3``.

    SMSBower/compatible gateways may return ``STATUS_WAIT_RETRY:<old-code>``
    after an activation is moved to retry.  The old code is not a new OTP and
    must not be handed to the next GoPay CVS/PIN step.
    """
    ignored = str(ignore_code or "").strip()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = sms_api(api_key, "getStatus", {"id": aid})
        except Exception:
            time.sleep(5)
            continue
        if resp.startswith("STATUS_OK:"):
            code = resp.split(":", 1)[1]
            m = re.search(r"\b(\d{4,6})\b", code)
            candidate = m.group(1) if m else code.strip()
            if candidate and candidate != ignored:
                return candidate
            log.debug("Ignoring stale SMS code for aid=%s", aid)
        if resp.startswith("STATUS_WAIT_RETRY:"):
            stale = resp.split(":", 1)[1].strip()
            if stale:
                log.debug("SMS provider returned stale retry code for aid=%s: %s", aid, stale)
        payload = _json_response_payload(resp)
        if payload:
            values = list(_json_values(payload.get("data", payload)))
            code = next((v for v in values if re.fullmatch(r"\d{4,8}", v) and v != ignored), "")
            if code:
                return code
            state = " ".join(values).upper()
            if "CANCEL" in state or "NO_ACTIVATION" in state:
                return None
        if resp == "STATUS_CANCEL":
            log.warning("SMS activation cancelled")
            return None
        time.sleep(5)
    return None


def sms_request_another(api_key: str, aid: str) -> bool:
    try:
        resp = sms_api(api_key, "setStatus", {"id": aid, "status": "3"})
        log.info("sms_request_another: %s", resp)
        return "ACCESS_RETRY_GET" in resp
    except Exception:
        return False


def sms_cancel(api_key: str, aid: str) -> None:
    try:
        resp = sms_api(api_key, "setStatus", {"id": aid, "status": "8"})
        log.info("sms_cancel %s: %s", aid, resp)
    except Exception:
        pass


def sms_done(api_key: str, aid: str) -> None:
    try:
        sms_api(api_key, "setStatus", {"id": aid, "status": "6"})
    except Exception:
        pass


# ========== API Error Helpers ==========

def is_waf_block(result: dict) -> bool:
    body = result.get("body", {})
    if isinstance(body, dict) and "raw" in body:
        return "WAF Block Page" in body["raw"]
    return False


def is_rate_limited(result: dict) -> bool:
    errors = result.get("body", {}).get("errors", [])
    if errors:
        code = errors[0].get("code", "")
        return "ratelimit" in code.lower() or "rate_limit" in code.lower()
    return result.get("status") == 429


def get_error_code(result: dict) -> str:
    body = result.get("body", {})
    if not isinstance(body, dict):
        return str(body)
    errors = body.get("errors", [])
    if errors:
        first = errors[0]
        return " ".join(str(first.get(k, "")) for k in ("code", "message") if first.get(k))
    error = body.get("error", {})
    if isinstance(error, dict):
        return " ".join(str(error.get(k, "")) for k in ("code", "description") if error.get(k))
    if "raw" in body:
        return str(body["raw"])
    return ""


def api_call_with_retry(fn, *args, max_retries: int = 2, **kwargs) -> dict:
    """Retry API call on WAF block or transient errors."""
    result = {}
    for attempt in range(max_retries + 1):
        result = fn(*args, **kwargs)
        if result["status"] in (200, 201, 204):
            return result
        if is_waf_block(result):
            if attempt < max_retries:
                wait = 5 * (attempt + 1)
                log.warning("WAF blocked, retrying in %ds... (%d/%d)", wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
        if is_rate_limited(result):
            if attempt < max_retries:
                wait = 30 * (attempt + 1)
                log.warning("Rate limited, retrying in %ds...", wait)
                time.sleep(wait)
                continue
        return result
    return result
