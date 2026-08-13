"""Coherent native-browser fingerprint policies for registration profiles."""

from __future__ import annotations

import hashlib
import os

from common.task_context import active_worker


_HARDWARE_PROFILES = (
    (4, 4),
    (8, 8),
    (12, 8),
    (16, 8),
)


def _seed_bytes(seed: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8", "ignore")).digest()


def browser_fingerprint(
    platform: str,
    core_version: str | int | None = None,
    seed: str | None = None,
) -> dict:
    """Return a small BitBrowser/AdsPower policy and let the browser stay native.

    Locale, timezone and geolocation follow the task's exit IP.  Hardware values
    are selected as a coherent pair per worker instead of being overwritten by
    a shared JavaScript stealth constant.
    """
    worker = active_worker()
    normalized = str(platform or "browser").strip().upper()
    chosen_seed = seed or (worker.fingerprint_seed if worker else "")
    if not chosen_seed:
        chosen_seed = os.urandom(16).hex()
    digest = _seed_bytes(f"{normalized}:{chosen_seed}")
    hardware, memory = _HARDWARE_PROFILES[digest[0] % len(_HARDWARE_PROFILES)]
    version = str(
        core_version
        or os.environ.get(f"{normalized}_BROWSER_CORE_VERSION")
        or os.environ.get("BB_CORE_VERSION")
        or "146"
    )
    return {
        "ostype": "PC",
        "os": "Win32",
        "coreVersion": version,
        "hardwareConcurrency": hardware,
        "deviceMemory": memory,
        "isIpCreateTimeZone": True,
        "isIpCreateLanguage": True,
        "isIpCreateDisplayLanguage": True,
        "isIpCreatePosition": True,
        "isIpCountry": True,
    }
