"""Sticky per-account browser profiles shared by every standalone HTTP stage.

The profile implementation is vendored from the registration project so this
service remains independent from the parent application at runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .register_profile import (
    Profile,
    apply_region_fingerprint,
    choose_tls_impersonate,
    profile_from_json,
    profile_headers,
    profile_summary,
    profile_to_json,
    random_profile,
    validate_profile,
)


STORE_PATH = Path(
    os.environ.get("REG_FACTORY_PLUS_FINGERPRINT_STORE")
    or Path(__file__).resolve().parents[1] / "fingerprint_profiles.json"
).resolve()
_LOCK = threading.RLock()
_STORE_CACHE: dict[str, Any] | None = None
_STORE_CACHE_PATH = ""
_STORE_CACHE_MTIME_NS = -1


def _text(value: Any) -> str:
    return str(value or "").strip()


def _account_key(account_id: str) -> str:
    return hashlib.sha256(_text(account_id).encode("utf-8")).hexdigest()


def _device_id(account_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"direct-bind:{_text(account_id)}"))


def _profile_id(profile: Profile, device_id: str) -> str:
    material = f"{profile_to_json(profile)}\n{_text(device_id)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _device_identity(account_id: str, platform_os: str) -> tuple[str, str]:
    digest = hashlib.blake2s(
        f"{platform_os}:{_text(account_id)}".encode("utf-8"), digest_size=8
    ).digest()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    suffix = "".join(alphabet[item % len(alphabet)] for item in digest[:7])
    if platform_os == "macOS":
        name = f"iMac-{suffix[:5]}"
        separator = ":"
    else:
        prefix = ("DESKTOP", "LAPTOP", "USER-PC")[digest[7] % 3]
        name = f"{prefix}-{suffix}"
        separator = "-"
    mac = bytearray(digest[:6])
    mac[0] = (mac[0] | 0x02) & 0xFE
    return name, separator.join(f"{part:02X}" for part in mac)


def _read_store(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return None
    return {"version": 3, "accounts": accounts}


def _load_store() -> dict[str, Any]:
    global _STORE_CACHE, _STORE_CACHE_PATH, _STORE_CACHE_MTIME_NS
    cache_path = str(STORE_PATH.resolve())
    try:
        mtime_ns = STORE_PATH.stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    if (
        _STORE_CACHE is not None
        and cache_path == _STORE_CACHE_PATH
        and mtime_ns == _STORE_CACHE_MTIME_NS
    ):
        return _STORE_CACHE
    primary = _read_store(STORE_PATH)
    if primary is not None:
        payload = primary
    else:
        backup = _read_store(STORE_PATH.with_suffix(".json.bak"))
        payload = backup if backup is not None else {"version": 3, "accounts": {}}
    _STORE_CACHE = payload
    _STORE_CACHE_PATH = cache_path
    _STORE_CACHE_MTIME_NS = mtime_ns
    return payload


def _save_store(payload: dict[str, Any]) -> None:
    global _STORE_CACHE, _STORE_CACHE_PATH, _STORE_CACHE_MTIME_NS
    temporary = STORE_PATH.with_suffix(".json.tmp")
    backup = STORE_PATH.with_suffix(".json.bak")
    payload["version"] = 3
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    if STORE_PATH.exists() and _read_store(STORE_PATH) is not None:
        shutil.copy2(STORE_PATH, backup)
    temporary.replace(STORE_PATH)
    _STORE_CACHE = payload
    _STORE_CACHE_PATH = str(STORE_PATH.resolve())
    _STORE_CACHE_MTIME_NS = STORE_PATH.stat().st_mtime_ns


def audit_fingerprint(profile: Profile, device_id: str) -> dict[str, Any]:
    """Validate cross-layer fields before the first outbound request."""
    validation = validate_profile(profile)
    errors = list(validation.errors)
    try:
        uuid.UUID(_text(device_id))
    except (ValueError, AttributeError):
        errors.append("device id must be a UUID")
    headers = profile_headers(profile, high_entropy=True)
    if headers.get("user-agent") != profile.user_agent:
        errors.append("profile user-agent header drift")
    if headers.get("sec-ch-ua") != profile.sec_ch_ua:
        errors.append("profile sec-ch-ua header drift")
    if headers.get("accept-language") != profile.locale:
        errors.append("profile locale header drift")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": list(validation.warnings),
        "fingerprint_id": _profile_id(profile, device_id),
    }


def _ensure_account_fingerprint(
    store: dict[str, Any], account_id: str, region: str
) -> tuple[Profile, str, bool]:
    raw_account_id = _text(account_id)
    if not raw_account_id:
        raise ValueError("account id is required for fingerprint allocation")
    key = _account_key(raw_account_id)
    accounts = store["accounts"]
    old_entry = accounts.get(key)
    entry = dict(old_entry) if isinstance(old_entry, dict) else {}
    profile: Profile | None = None
    if entry:
        try:
            profile = profile_from_json(entry.get("profile"))
        except ValueError:
            profile = None
    requested_region = _text(region).upper() or "US"
    dirty = False
    if profile is None:
        profile = random_profile(region=requested_region)
        dirty = True
    elif profile.browser in {"chrome", "edge"} and profile.browser_major == 144:
        # The bundled curl transport has no chrome144 preset. Replace the old
        # mixed UA144/TLS142 snapshot once, then keep the upgraded identity sticky.
        device_name, mac_address = profile.device_name, profile.mac_address
        profile = random_profile(
            region=requested_region,
            browser=profile.browser,
            platform_os=profile.platform_os,
            chrome_major=146,
            hardware_profile=profile.hardware_profile,
        )
        profile.device_name = device_name
        profile.mac_address = mac_address
        dirty = True
    validation = validate_profile(profile)
    if not validation.valid:
        raise ValueError("fingerprint profile mismatch: " + "; ".join(validation.errors))
    device_id = _text(entry.get("device_id")) or _device_id(raw_account_id)
    # Account/browser identity belongs to bind/payment pool 1. Link pool 2 may
    # use another exit country, but must not rewrite this sticky profile.
    if profile.region != requested_region:
        apply_region_fingerprint(profile, requested_region)
        dirty = True
    expected_tls = choose_tls_impersonate(
        browser=profile.browser,
        chrome_major=profile.browser_major,
        platform_os=profile.platform_os,
    )
    if profile.tls_impersonate != expected_tls:
        profile.tls_impersonate = expected_tls
        dirty = True
    if not profile.device_name or not profile.mac_address:
        profile.device_name, profile.mac_address = _device_identity(
            raw_account_id, profile.platform_os
        )
        dirty = True
    audit = audit_fingerprint(profile, device_id)
    if not audit["ok"]:
        raise ValueError("fingerprint bundle mismatch: " + "; ".join(audit["errors"]))
    serialized = profile_to_json(profile)
    now = datetime.now(timezone.utc).isoformat()
    updated = {
        **entry,
        "created_at": entry.get("created_at") or now,
        "device_id": device_id,
        "fingerprint_id": audit["fingerprint_id"],
        "profile": serialized,
    }
    if (
        updated.get("device_id") != entry.get("device_id")
        or updated.get("fingerprint_id") != entry.get("fingerprint_id")
        or updated.get("profile") != entry.get("profile")
    ):
        dirty = True
    if dirty:
        updated["updated_at"] = now
        accounts[key] = updated
    return profile, device_id, dirty


def get_account_fingerprints(
    items: list[dict[str, Any]],
) -> list[tuple[Profile, str]]:
    """Allocate up to 500 sticky profiles with one store load and one atomic save."""
    if not isinstance(items, list) or not items:
        return []
    if len(items) > 500:
        raise ValueError("fingerprint batch cannot exceed 500 accounts")
    with _LOCK:
        store = _load_store()
        results: list[tuple[Profile, str]] = []
        dirty = False
        for item in items:
            profile, device_id, changed = _ensure_account_fingerprint(
                store,
                _text(item.get("account_id")),
                _text(item.get("region") or "US"),
            )
            results.append((profile, device_id))
            dirty = dirty or changed
        if dirty:
            _save_store(store)
        return results


def get_account_fingerprint(
    account_id: str,
    *,
    region: str = "US",
    proxy: str = "",
) -> tuple[Profile, str]:
    """Return one persistent registration Profile and device id per account."""
    del proxy  # Region is fixed by the bind/payment role, never inferred here.
    return get_account_fingerprints(
        [{"account_id": account_id, "region": region}]
    )[0]


def optimize_fingerprint_store(region: str = "US") -> dict[str, int]:
    """Migrate stored profiles in one pass and one atomic write."""
    requested_region = _text(region).upper() or "US"
    with _LOCK:
        store = _load_store()
        accounts = store["accounts"]
        scanned = migrated = invalid = 0
        for key, raw_entry in list(accounts.items()):
            scanned += 1
            if not isinstance(raw_entry, dict):
                invalid += 1
                continue
            entry = dict(raw_entry)
            try:
                profile = profile_from_json(entry.get("profile"))
            except ValueError:
                invalid += 1
                continue
            if profile is None:
                invalid += 1
                continue
            changed = False
            if profile.browser in {"chrome", "edge"} and profile.browser_major == 144:
                device_name, mac_address = profile.device_name, profile.mac_address
                profile = random_profile(
                    region=requested_region,
                    browser=profile.browser,
                    platform_os=profile.platform_os,
                    chrome_major=146,
                    hardware_profile=profile.hardware_profile,
                )
                profile.device_name = device_name
                profile.mac_address = mac_address
                changed = True
            if profile.region != requested_region:
                apply_region_fingerprint(profile, requested_region)
                changed = True
            expected_tls = choose_tls_impersonate(
                browser=profile.browser,
                chrome_major=profile.browser_major,
                platform_os=profile.platform_os,
            )
            if profile.tls_impersonate != expected_tls:
                profile.tls_impersonate = expected_tls
                changed = True
            device_id = _text(entry.get("device_id"))
            audit = audit_fingerprint(profile, device_id)
            if not audit["ok"]:
                invalid += 1
                continue
            serialized = profile_to_json(profile)
            if (
                serialized != entry.get("profile")
                or audit["fingerprint_id"] != entry.get("fingerprint_id")
            ):
                changed = True
            if changed:
                entry["profile"] = serialized
                entry["fingerprint_id"] = audit["fingerprint_id"]
                entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                accounts[key] = entry
                migrated += 1
        if migrated:
            _save_store(store)
        return {"scanned": scanned, "migrated": migrated, "invalid": invalid}


def extractor_profile(profile: Profile, device_id: str = "") -> dict[str, str]:
    """Adapt a registration Profile to the link extractor's header schema."""
    headers = profile_headers(profile, high_entropy=True)
    return {
        "name": f"register:{profile.browser}/{profile.browser_full_version}:{profile.hardware_profile}",
        "ua": profile.user_agent,
        "major": str(profile.browser_major),
        "full": profile.browser_full_version,
        "platform": profile.sec_ch_ua_platform.strip('"') or profile.platform_os,
        "arch": profile.architecture,
        "bitness": profile.bitness,
        "mobile": "?1" if profile.mobile else "?0",
        "platform_version": profile.platform_version,
        "sec_ch_ua": profile.sec_ch_ua,
        "sec_ch_ua_full_version_list": headers.get(
            "sec-ch-ua-full-version-list", profile.sec_ch_ua
        ),
        "accept_language": profile.locale,
        "oai_language": profile.language,
        "tls_impersonate": profile.tls_impersonate,
        "timezone": profile.timezone_id,
        "region": profile.region,
        "summary": profile_summary(profile),
        "fingerprint_id": _profile_id(
            profile,
            _text(device_id) or "profile-only",
        ),
    }
