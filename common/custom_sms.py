"""Persistent custom phone pool backed by SMS record URLs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from common.file_lock import file_lock


POOL_VERSION = 1
LEASE_SECONDS = 30 * 60
PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
CODE_RE = re.compile(r"(?<![\d-])(\d{4,8})(?![\d-])")
WAIT_STATUSES = {"", "0", "no", "none", "null", "wait", "waiting", "pending", "false"}


def _allowed_hosts() -> set[str]:
    """Return operator-approved record hosts that may use non-public DNS."""
    raw = str(os.environ.get("CUSTOM_SMS_ALLOWED_HOSTS") or "")
    return {
        item.strip().lower().rstrip(".")
        for item in re.split(r"[\s,;]+", raw)
        if item.strip()
    }


def pool_path() -> Path:
    configured = str(os.environ.get("CUSTOM_SMS_POOL_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path(__file__).resolve().parents[1])
    return root / "runtime" / "state" / "custom_sms_pool.json"


def _empty_pool() -> dict:
    return {"version": POOL_VERSION, "records": []}


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_pool()
    if not isinstance(value, dict) or not isinstance(value.get("records"), list):
        return _empty_pool()
    return value


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _record_id(phone: str) -> str:
    return hashlib.sha256(phone.encode("ascii")).hexdigest()[:20]


def _normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s()-]", "", str(value or "").strip())
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("phone must be an E.164 number")
    return "+" + phone.lstrip("+")


def _normalize_url(value: str) -> str:
    url = str(value or "").strip()
    if len(url) > 2048:
        raise ValueError("record URL is too long")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("record URL must use http or https")
    return url


def parse_import_line(line: str) -> tuple[str, str]:
    parts = str(line or "").strip().split("----", 1)
    if len(parts) != 2:
        raise ValueError("expected +phone----record_url")
    return _normalize_phone(parts[0]), _normalize_url(parts[1])


def import_text(text: str) -> dict:
    """Add or update custom phone records without exposing URL tokens."""
    parsed = []
    bad_samples = []
    bad = 0
    seen = set()
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        try:
            phone, url = parse_import_line(raw)
        except ValueError:
            bad += 1
            if len(bad_samples) < 5:
                bad_samples.append(raw.strip()[:80])
            continue
        if phone in seen:
            continue
        seen.add(phone)
        parsed.append((phone, url))

    path = pool_path()
    added = updated = skipped = 0
    now = time.time()
    with file_lock(path):
        pool = _load(path)
        by_phone = {item.get("phone"): item for item in pool["records"] if isinstance(item, dict)}
        for phone, url in parsed:
            current = by_phone.get(phone)
            if current is None:
                current = {
                    "id": _record_id(phone),
                    "phone": phone,
                    "url": url,
                    "status": "available",
                    "added_at": now,
                    "updated_at": now,
                    "attempts": 0,
                }
                pool["records"].append(current)
                by_phone[phone] = current
                added += 1
            elif current.get("url") != url:
                current.update({
                    "url": url,
                    "status": "available",
                    "updated_at": now,
                    "lease_id": "",
                    "leased_at": 0,
                })
                updated += 1
            else:
                skipped += 1
        _save(path, pool)
    result = summary()
    result.update({
        "ok": True,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "bad": bad,
        "bad_samples": bad_samples,
    })
    return result


def _release_stale(pool: dict, now: float) -> None:
    for item in pool["records"]:
        if item.get("status") != "leased":
            continue
        if now - float(item.get("leased_at") or 0) <= LEASE_SECONDS:
            continue
        item.update({"status": "available", "lease_id": "", "leased_at": 0})


def claim() -> tuple[str, str, str] | None:
    """Lease one number and return (digits, country_code, opaque_pkey)."""
    path = pool_path()
    now = time.time()
    with file_lock(path):
        pool = _load(path)
        _release_stale(pool, now)
        record = next((item for item in pool["records"] if item.get("status") == "available"), None)
        if record is None:
            _save(path, pool)
            return None
        lease_id = hashlib.sha256(f"{record['id']}:{now}:{os.getpid()}".encode()).hexdigest()[:16]
        record.update({
            "status": "leased",
            "lease_id": lease_id,
            "leased_at": now,
            "updated_at": now,
            "attempts": int(record.get("attempts") or 0) + 1,
        })
        _save(path, pool)
        return record["phone"].lstrip("+"), "", f"custom_{record['id']}_{lease_id}"


def _pkey_parts(pkey: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"custom_([0-9a-f]{20})_([0-9a-f]{16})", str(pkey or ""))
    return match.groups() if match else None


def _leased_record(pool: dict, pkey: str) -> dict | None:
    parts = _pkey_parts(pkey)
    if not parts:
        return None
    record_id, lease_id = parts
    return next((
        item for item in pool["records"]
        if item.get("id") == record_id
        and item.get("status") == "leased"
        and item.get("lease_id") == lease_id
    ), None)


def _record_url(pkey: str) -> str:
    path = pool_path()
    with file_lock(path):
        record = _leased_record(_load(path), pkey)
        return str(record.get("url") or "") if record else ""


def extract_code(body: str) -> str | None:
    text = str(body or "").strip()
    if not text:
        return None
    status, separator, payload = text.partition("|")
    if separator and status.strip().lower() in WAIT_STATUSES:
        return None
    search_text = payload if separator else text
    contextual = re.search(
        r"(?:openai|chatgpt|verification|verify|code|otp)"
        r"[^\d]{0,80}(\d{4,8})(?!\d)",
        search_text,
        flags=re.IGNORECASE,
    )
    if contextual:
        return contextual.group(1)
    matches = CODE_RE.findall(search_text)
    return matches[-1] if matches else None


def _require_public_url(url: str) -> None:
    hostname = urlsplit(url).hostname or ""
    if hostname.lower() == "localhost":
        raise ValueError("record URL host must be public")
    normalized = hostname.lower().rstrip(".")
    allowed = _allowed_hosts()
    if normalized in allowed or any(
        normalized.endswith("." + suffix) for suffix in allowed
    ):
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except OSError as exc:
        raise ValueError("record URL host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("record URL host must resolve only to public addresses")


def _fetch_record(url: str) -> str:
    current = url
    for _ in range(4):
        _require_public_url(current)
        response = requests.get(
            current,
            timeout=30,
            headers={"Accept": "text/plain,text/html"},
            allow_redirects=False,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            if not location:
                raise requests.RequestException("record redirect has no location")
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        return response.text
    raise requests.TooManyRedirects("record URL redirected too many times")


def get_code(pkey: str, max_wait: int = 180, interval: int = 5) -> str | None:
    url = _record_url(pkey)
    if not url:
        return None
    try:
        _require_public_url(url)
    except ValueError as exc:
        print(f"  [custom-sms] {exc}")
        return None
    started = time.time()
    while time.time() - started < max_wait:
        try:
            code = extract_code(_fetch_record(url))
            if code:
                _mark_used(pkey)
                print(f"  [custom-sms] code: {code}")
                return code
        except requests.RequestException as exc:
            print(f"  [custom-sms] record request failed: {str(exc)[:100]}")
        elapsed = int(time.time() - started)
        print(f"  [custom-sms] waiting... ({elapsed}s/{max_wait}s)")
        time.sleep(interval)
    return None


def _mark_used(pkey: str) -> bool:
    path = pool_path()
    with file_lock(path):
        pool = _load(path)
        record = _leased_record(pool, pkey)
        if not record:
            return False
        record.update({
            "status": "used",
            "used_at": time.time(),
            "updated_at": time.time(),
            "lease_id": "",
            "leased_at": 0,
        })
        _save(path, pool)
        return True


def release(pkey: str) -> bool:
    path = pool_path()
    with file_lock(path):
        pool = _load(path)
        record = _leased_record(pool, pkey)
        if not record:
            return False
        record.update({
            "status": "available",
            "lease_id": "",
            "leased_at": 0,
            "updated_at": time.time(),
        })
        pool["records"].remove(record)
        pool["records"].append(record)
        _save(path, pool)
        return True


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [(key, "***") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def summary() -> dict:
    path = pool_path()
    now = time.time()
    with file_lock(path):
        pool = _load(path)
        _release_stale(pool, now)
        _save(path, pool)
        records = [item for item in pool["records"] if isinstance(item, dict)]
    counts = {status: sum(item.get("status") == status for item in records) for status in ("available", "leased", "used")}
    return {
        "total": len(records),
        **counts,
        "records": [
            {
                "phone": item.get("phone", ""),
                "status": item.get("status", "available"),
                "attempts": int(item.get("attempts") or 0),
                "record_url": _redact_url(str(item.get("url") or "")),
            }
            for item in records
        ],
    }
