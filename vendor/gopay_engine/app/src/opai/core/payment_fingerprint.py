"""Stable browser-style payment fingerprints for GoPay web payment flows."""
from __future__ import annotations

import hashlib
import random
from copy import deepcopy
from typing import Any


_DEFAULT_LOCALE = "id-ID"
_DEFAULT_TIMEZONE = "Asia/Jakarta"

_DESKTOP_VIEWPORTS = [
    {"width": 1366, "height": 768, "device_scale_factor": 1},
    {"width": 1440, "height": 900, "device_scale_factor": 1},
    {"width": 1536, "height": 864, "device_scale_factor": 1},
    {"width": 1600, "height": 900, "device_scale_factor": 1},
    {"width": 1920, "height": 1080, "device_scale_factor": 1},
]

_WINDOWS_PLATFORMS = [
    ("Windows NT 10.0; Win64; x64", '"Windows"'),
    ("Windows NT 10.0; WOW64", '"Windows"'),
]


def _seed_from_parts(*parts: str) -> str:
    material = "|".join(str(part or "") for part in parts if str(part or ""))
    return material or "gopay-payment-profile"


def _profile_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def build_payment_fingerprint(*, seed: str = "", phone: str = "", local: str = "", account_id: str = "") -> dict[str, Any]:
    """Build one deterministic payment fingerprint from stable account data."""
    seed_value = _seed_from_parts(seed, account_id, phone, local)
    rng = random.Random(seed_value)
    chrome_major = rng.choice([120, 121, 122, 123, 124])
    platform_token, sec_ch_platform = rng.choice(_WINDOWS_PLATFORMS)
    viewport = deepcopy(rng.choice(_DESKTOP_VIEWPORTS))
    sec_ch_ua = f'"Not_A Brand";v="8", "Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}"'

    return {
        "version": 1,
        "profile_id": _profile_id(seed_value),
        "user_agent": (
            f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
        ),
        "locale": _DEFAULT_LOCALE,
        "timezone": _DEFAULT_TIMEZONE,
        "viewport": viewport,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": sec_ch_platform,
    }


def normalize_payment_fingerprint(profile: dict[str, Any] | None, **seed_parts: str) -> dict[str, Any]:
    """Return a complete profile, preserving valid saved values when present."""
    fallback = build_payment_fingerprint(**seed_parts)
    if not isinstance(profile, dict):
        return fallback

    normalized = deepcopy(fallback)
    for key in (
        "version",
        "profile_id",
        "user_agent",
        "locale",
        "timezone",
        "sec_ch_ua",
        "sec_ch_ua_mobile",
        "sec_ch_ua_platform",
    ):
        value = profile.get(key)
        if value not in (None, ""):
            normalized[key] = value

    viewport = profile.get("viewport")
    if isinstance(viewport, dict):
        merged_viewport = deepcopy(fallback["viewport"])
        for key in ("width", "height", "device_scale_factor"):
            value = viewport.get(key)
            if value not in (None, ""):
                try:
                    merged_viewport[key] = int(value)
                except (TypeError, ValueError):
                    pass
        normalized["viewport"] = merged_viewport

    return normalized


def ensure_account_payment_fingerprint(account: dict[str, Any]) -> dict[str, Any]:
    """Ensure an account dict carries exactly one reusable payment fingerprint."""
    profile = normalize_payment_fingerprint(
        account.get("payment_fingerprint"),
        phone=str(account.get("phone", "")),
        local=str(account.get("local", "")),
        account_id=str(account.get("account_id") or account.get("customer_id") or ""),
    )
    account["payment_fingerprint"] = profile
    return profile


def payment_fingerprint_headers(profile: dict[str, Any] | None) -> dict[str, str]:
    """Map a payment fingerprint to reusable browser request headers."""
    fp = normalize_payment_fingerprint(profile)
    viewport = fp.get("viewport") or {}
    width = str(viewport.get("width") or "")
    dpr = str(viewport.get("device_scale_factor") or "")
    locale = str(fp.get("locale") or _DEFAULT_LOCALE)
    timezone = str(fp.get("timezone") or _DEFAULT_TIMEZONE)

    headers = {
        "User-Agent": str(fp.get("user_agent") or ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": f"{locale},{locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-CH-UA": str(fp.get("sec_ch_ua") or ""),
        "Sec-CH-UA-Mobile": str(fp.get("sec_ch_ua_mobile") or "?0"),
        "Sec-CH-UA-Platform": str(fp.get("sec_ch_ua_platform") or '"Windows"'),
        "X-Timezone": timezone,
        "X-User-Locale": locale.replace("-", "_"),
    }
    if width:
        headers["Viewport-Width"] = width
        headers["Sec-CH-Viewport-Width"] = width
    if dpr:
        headers["DPR"] = dpr
        headers["Sec-CH-DPR"] = dpr
    return headers
