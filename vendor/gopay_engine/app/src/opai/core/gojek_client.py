"""
Gojek/GoPay Complete Protocol Client

Covers the full lifecycle:
  1. Registration (api.gojekapi.com /v7/customers/signup)
  2. Login        (accounts.goto-products.com /goto-auth)
  3. OTP          (accounts.goto-products.com /cvs  +  api.gojekapi.com /v6/customers)
  4. GoPay Register (customer.gopayapi.com /v1/customer/payment-options/register)
  5. PIN Setup      (customer.gopayapi.com /v2/users/pin + /api/v1/users/pins/setup)
  6. Wallet Ops     (customer.gopayapi.com)
  7. Envelope Claim (customer.gopayapi.com POST /v1/festivals/link)

Verification status:
  ✅ VERIFIED  — GoPay 2.8.0 app identity/header surface matches latest capture
  ✅ VERIFIED  — X-E1 HMAC canonical form reproduces /goto-auth/login/methods capture
  ✅ VERIFIED  — SignUp headers captured via Frida gadget (2026-05-14):
                  X-DeviceCheckToken = "LITMUS_DISABLED" (Play Integrity OFF)
                  X-Signature = "1003" (SDK version, not crypto)
                  X-Signature-Time = unix timestamp
  ✅ VERIFIED  — Envelope claim: POST /v1/festivals/link body={"link_id":"..."}
                  Captured via VM memory scan on BlueStacks (2026-05-16)
                  Response: 422 GoPay-36006 = expired, 200 = claimed
  ⚠️ UNVERIFIED — SSO, CVS, PIN endpoints (decompiled, not live-tested yet)

Device tokens (ALL can be generated/hardcoded, NO real device needed):
  D1            — DexGuard cert fingerprint, STATIC per APK version (hardcoded)
  X-UniqueId    — random hex, os.urandom(8).hex()
  X-M1          — device telemetry, format known, construct from template
  X-DeviceToken — FCM push token, can be empty
  X-DeviceCheckToken — "LITMUS_DISABLED" (Play Integrity disabled by RemoteConfig)
  X-Signature   — "1003" (SDK version number, not a signature)

RE source: jadx_classes (SignUpApi), jadx_c2 (PinApi), jadx_c4 (SCP Login SDK),
           jadx_c11 (CVS Verification), jadx_hi (PaymentWidgetCardService)
Signing: gopay_signer_v2.py (HMAC-SHA256, verified via Frida 2026-05-07)
"""

import base64
import hashlib
import json
import logging
import os
import random
import re
import struct
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Optional

import tls_client

from .gopay_signer_v2 import sign_v2
from .gopay_support_sdk import get_support_body_provider, support_body_lengths

log = logging.getLogger(__name__)

CLIENT_ID = os.environ.get("OPAI_GOPAY_CLIENT_ID", "gopay:consumer:app")
CLIENT_SECRET = os.environ.get("OPAI_GOPAY_CLIENT_SECRET", "raOUumeMRBNifqvZRFjvsgTnjAlaA9")
SIGNUP_CLIENT_NAME = os.environ.get("OPAI_GOPAY_SIGNUP_CLIENT_NAME", "gopay_consumer_app")
SIGNUP_SIGNED_UP_COUNTRY = os.environ.get("OPAI_GOPAY_SIGNED_UP_COUNTRY", "62")
SIGNUP_BASIC_AUTH_ID = os.environ.get("OPAI_GOPAY_SIGNUP_BASIC_AUTH_ID", "bb648413-b637-443a-8ebf-176cf9b5dc32")
SIGNUP_ONBOARDING_PARTNER = os.environ.get("OPAI_GOPAY_SIGNUP_ONBOARDING_PARTNER", "gopay_consumer_app")
CVS_DOUBLE_SLASH_INITIATE = os.environ.get("OPAI_GOPAY_CVS_DOUBLE_SLASH_INITIATE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# Original APK signing cert D1 (same for all installs of this APK version)
ORIGINAL_D1 = "CF:43:60:94:46:9C:A0:8F:CB:5C:95:05:97:E9:03:51:40:0A:C7:33:EC:BA:40:71:F1:94:DC:CE:BA:AE:4C:A8"

SSO_BASE = "https://accounts.goto-products.com"
GOPAY_BASE = "https://customer.gopayapi.com"
GOJEK_API_BASE = "https://api.gojekapi.com"
GOFIN_GATEWAY_BASE = "https://gateway.paylater.gofin.co.id"
GOPAY_APP_ID = os.environ.get("OPAI_GOPAY_APP_ID", "com.gojek.gopay")
GOPAY_APP_VERSION = os.environ.get("OPAI_GOPAY_APP_VERSION", "2.8.0")
GOPAY_APP_BUILD = os.environ.get("OPAI_GOPAY_APP_BUILD", "2080")
SUPPORT_APP_ID = os.environ.get("OPAI_GOPAY_SUPPORT_APP_ID", "com.gojek.gopay")
SUPPORT_APP_VERSION = os.environ.get("OPAI_GOPAY_SUPPORT_APP_VERSION", "2.8.0")
SUPPORT_APP_BUILD = os.environ.get("OPAI_GOPAY_SUPPORT_APP_BUILD", "2080")
SUPPORT_SDK_VERSION = os.environ.get("OPAI_GOPAY_SUPPORT_SDK_VERSION", "0.44.0")
DEVICE_PROFILES_FILE = os.environ.get("OPAI_GOPAY_DEVICE_PROFILES_FILE", "").strip()
DEVICE_PROFILE_ALLOCATIONS_FILE = os.environ.get("OPAI_GOPAY_DEVICE_PROFILE_ALLOCATIONS_FILE", "").strip()


def _json_body(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _normalize_signup_phone(phone: str, country_code: str = "+62") -> str:
    country = country_code if str(country_code or "").startswith("+") else f"+{country_code}"
    digits = re.sub(r"\D+", "", str(phone or ""))
    country_digits = country.lstrip("+")
    if digits.startswith(country_digits):
        return f"+{digits}"
    return f"{country}{digits}"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


SSO_TIMEOUT_SEC = _env_int("OPAI_GOPAY_SSO_TIMEOUT_SEC", 30)
SSO_RETRIES = max(1, _env_int("OPAI_GOPAY_SSO_RETRIES", 3))
SSO_RETRY_SLEEP_SEC = _env_float("OPAI_GOPAY_SSO_RETRY_SLEEP_SEC", 5.0)
PROXY_PROBE_TIMEOUT_SEC = _env_int("OPAI_GOPAY_PROXY_PROBE_TIMEOUT_SEC", 12)
PROXY_PROBE_URL = os.environ.get("OPAI_GOPAY_PROXY_PROBE_URL", "https://api.ipify.org?format=json")


def normalize_proxy_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if "@" in value:
        return f"http://{value}"
    parts = value.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = parts[0], parts[1]
        user = parts[2]
        password = ":".join(parts[3:])
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{value}"


def mask_proxy_url(raw: str) -> str:
    proxy = normalize_proxy_url(raw)
    if not proxy:
        return "direct"
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return urllib.parse.urlunsplit((parsed.scheme, f"***:***@{host}{port}", parsed.path, parsed.query, parsed.fragment))
    return proxy


def probe_proxy_egress(proxy: str, timeout_sec: float | None = None) -> dict:
    """Verify proxy connectivity before sending state-changing API requests."""
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return {"ok": True, "proxy": "direct", "ip": "direct", "status": 0}

    timeout = int(PROXY_PROBE_TIMEOUT_SEC if timeout_sec is None else timeout_sec)
    session = tls_client.Session(
        client_identifier="okhttp4_android_13",
        random_tls_extension_order=True,
        force_http1=True,
    )
    session.proxies = {"http": proxy, "https": proxy}
    try:
        resp = session.get(PROXY_PROBE_URL, timeout_seconds=timeout)
        status = int(getattr(resp, "status_code", 0) or 0)
        text = (getattr(resp, "text", "") or "").strip()
        ip = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                ip = str(data.get("ip") or "").strip()
        except Exception:
            ip = text[:80]
        return {
            "ok": 200 <= status < 300,
            "proxy": mask_proxy_url(proxy),
            "ip": ip,
            "status": status,
            "raw": text[:160],
        }
    except Exception as exc:
        return {
            "ok": False,
            "proxy": mask_proxy_url(proxy),
            "ip": "",
            "status": 0,
            "error": str(exc),
        }


def looks_like_network_timeout(exc: Exception | str) -> bool:
    text = str(exc).lower()
    needles = (
        "timeout",
        "timed out",
        "client.timeout",
        "context deadline exceeded",
        "request canceled",
        "awaiting headers",
        "connection refused",
        "proxyconnect",
        "no route to host",
    )
    return any(x in text for x in needles)

# Indonesian-market Android device profiles for deterministic account identity.
_DEVICE_PROFILES = [
    # brand, manufacturer, model, board_platform, cpu_freq_mhz, cpu_cores, screen, android_versions, disk_mb
    ("samsung", "samsung", "SM-A546E", "exynos1380", 2400, 8, "1080x2340", ("13", "14"), (128000, 131072, 262144)),
    ("samsung", "samsung", "SM-A536E", "exynos1280", 2400, 8, "1080x2400", ("12", "13", "14"), (128000, 131072)),
    ("samsung", "samsung", "SM-A346E", "mt6877", 2600, 8, "1080x2340", ("13", "14"), (128000, 131072)),
    ("samsung", "samsung", "SM-A256E", "exynos1280", 2400, 8, "1080x2340", ("14",), (128000, 131072)),
    ("samsung", "samsung", "SM-M336BU", "exynos1280", 2400, 8, "1080x2408", ("12", "13"), (128000,)),
    ("Xiaomi", "Xiaomi", "2201117TY", "taro", 3000, 8, "1080x2400", ("12", "13"), (128000, 256000)),
    ("Xiaomi", "Xiaomi", "23053RN02A", "mt6768", 2000, 8, "1080x2400", ("13",), (128000,)),
    ("Xiaomi", "Xiaomi", "2312DRA50G", "garnet", 2800, 8, "1220x2712", ("14",), (256000, 262144)),
    ("Redmi", "Xiaomi", "2209116AG", "mt8781", 2200, 8, "1080x2400", ("12", "13"), (128000,)),
    ("Redmi", "Xiaomi", "23090RA98G", "mt6789", 2200, 8, "1080x2460", ("13", "14"), (128000, 256000)),
    ("POCO", "Xiaomi", "23049PCD8G", "mt6833", 2200, 8, "1080x2400", ("13", "14"), (128000, 256000)),
    ("POCO", "Xiaomi", "22101320G", "taro", 3200, 8, "1080x2400", ("12", "13", "14"), (256000,)),
    ("OPPO", "OPPO", "CPH2565", "mt6833", 2200, 8, "720x1612", ("13",), (128000,)),
    ("OPPO", "OPPO", "CPH2387", "mt6833", 2200, 8, "1080x2400", ("12", "13"), (128000,)),
    ("OPPO", "OPPO", "CPH2529", "mt6769", 2000, 8, "720x1612", ("13",), (128000,)),
    ("vivo", "vivo", "V2248", "mt6769", 2000, 8, "720x1612", ("13",), (128000,)),
    ("vivo", "vivo", "V2204", "mt6833", 2200, 8, "1080x2404", ("12", "13"), (128000,)),
    ("vivo", "vivo", "V2310", "mt6769", 2000, 8, "720x1612", ("13", "14"), (128000,)),
    ("realme", "realme", "RMX3710", "mt6833", 2200, 8, "1080x2400", ("13",), (128000, 256000)),
    ("realme", "realme", "RMX3630", "mt6833", 2200, 8, "1080x2408", ("12", "13"), (128000,)),
    ("realme", "realme", "RMX3830", "mt6769", 2000, 8, "720x1604", ("13", "14"), (128000,)),
    ("Infinix", "INFINIX", "X6833B", "mt6789", 2200, 8, "1080x2460", ("13",), (128000, 256000)),
    ("Infinix", "INFINIX", "X6711", "mt6769", 2000, 8, "1080x2460", ("12", "13"), (128000,)),
    ("TECNO", "TECNO", "CK8n", "mt6769", 2000, 8, "720x1612", ("13",), (128000,)),
]


def _coerce_device_profile(item) -> tuple:
    if isinstance(item, dict):
        android_versions = item.get("android_versions") or item.get("android")
        disk_choices = item.get("disk_mb") or item.get("disk_choices")
        values = (
            item.get("brand"),
            item.get("manufacturer"),
            item.get("model"),
            item.get("platform") or item.get("board_platform"),
            item.get("cpu_freq_mhz"),
            item.get("cpu_cores"),
            item.get("screen"),
            android_versions,
            disk_choices,
        )
    elif isinstance(item, (list, tuple)) and len(item) >= 9:
        values = tuple(item[:9])
    else:
        raise ValueError("profile must be an object or 9-item list")

    brand, manufacturer, model, platform, cpu_freq, cpu_cores, screen, android_versions, disk_choices = values
    text_fields = {
        "brand": brand,
        "manufacturer": manufacturer,
        "model": model,
        "platform": platform,
        "screen": screen,
    }
    for name, value in text_fields.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not re.fullmatch(r"\d+x\d+", screen.strip()):
        raise ValueError("screen must look like 1080x2400")
    try:
        cpu_freq = int(cpu_freq)
        cpu_cores = int(cpu_cores)
    except (TypeError, ValueError) as exc:
        raise ValueError("cpu fields must be integers") from exc
    if cpu_freq <= 0 or cpu_cores <= 0:
        raise ValueError("cpu fields must be positive")

    if not isinstance(android_versions, (list, tuple)) or not android_versions:
        raise ValueError("android_versions must be a non-empty list")
    android_versions = tuple(str(x).strip() for x in android_versions if str(x).strip())
    if not android_versions:
        raise ValueError("android_versions must contain non-empty values")

    if not isinstance(disk_choices, (list, tuple)) or not disk_choices:
        raise ValueError("disk_mb must be a non-empty list")
    try:
        disk_choices = tuple(int(x) for x in disk_choices)
    except (TypeError, ValueError) as exc:
        raise ValueError("disk_mb values must be integers") from exc
    if any(x <= 0 for x in disk_choices):
        raise ValueError("disk_mb values must be positive")

    return (
        brand.strip(),
        manufacturer.strip(),
        model.strip(),
        platform.strip(),
        cpu_freq,
        cpu_cores,
        screen.strip(),
        android_versions,
        disk_choices,
    )


def _load_device_profiles_file(path: str) -> list[tuple]:
    if not path:
        return []
    full_path = path if os.path.isabs(path) else os.path.abspath(path)
    try:
        with open(full_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        log.warning("Device profiles file not found: %s; using built-in profiles", full_path)
        return []
    except Exception as exc:
        log.warning("Failed to read device profiles file %s: %s; using built-in profiles", full_path, exc)
        return []

    profiles_payload = payload.get("profiles") if isinstance(payload, dict) else payload
    if not isinstance(profiles_payload, list):
        log.warning("Device profiles file %s must contain a profiles list; using built-in profiles", full_path)
        return []

    profiles = []
    for idx, item in enumerate(profiles_payload):
        try:
            profiles.append(_coerce_device_profile(item))
        except Exception as exc:
            log.warning("Skipping invalid device profile #%s in %s: %s", idx, full_path, exc)
    if profiles:
        log.info("Loaded %s GoPay device profiles from %s", len(profiles), full_path)
    else:
        log.warning("No valid device profiles loaded from %s; using built-in profiles", full_path)
    return profiles


_EXTERNAL_DEVICE_PROFILES = _load_device_profiles_file(DEVICE_PROFILES_FILE)
if _EXTERNAL_DEVICE_PROFILES:
    _DEVICE_PROFILES = _EXTERNAL_DEVICE_PROFILES

_INSTALL_START_MS = 1704067200000  # 2024-01-01T00:00:00Z
_INSTALL_WINDOW_MS = 630 * 24 * 60 * 60 * 1000


def _resolve_local_path(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.abspath(path)


def _stable_int(seed: bytes, start: int, length: int) -> int:
    chunk = seed[start:start + length]
    if len(chunk) < length:
        chunk = hashlib.sha256(seed + bytes([start, length])).digest()[:length]
    return int.from_bytes(chunk, "big")


def _choose_device_profile(raw_seed: str, h: bytes) -> tuple:
    fallback_idx = _stable_int(h, 0, 2) % len(_DEVICE_PROFILES)
    allocations_path = _resolve_local_path(DEVICE_PROFILE_ALLOCATIONS_FILE)
    if not allocations_path:
        return _DEVICE_PROFILES[fallback_idx]

    key = hashlib.sha256(raw_seed.encode()).hexdigest()
    try:
        import fcntl

        os.makedirs(os.path.dirname(allocations_path) or ".", exist_ok=True)
        with open(allocations_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.seek(0)
            raw = fh.read().strip()
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}

            assignments = payload.get("assignments")
            if not isinstance(assignments, dict):
                assignments = {}

            assigned_idx = assignments.get(key)
            if isinstance(assigned_idx, int) and 0 <= assigned_idx < len(_DEVICE_PROFILES):
                return _DEVICE_PROFILES[assigned_idx]

            used = {idx for idx in assignments.values() if isinstance(idx, int) and 0 <= idx < len(_DEVICE_PROFILES)}
            selected_idx = fallback_idx
            if len(used) < len(_DEVICE_PROFILES):
                for offset in range(len(_DEVICE_PROFILES)):
                    candidate = (fallback_idx + offset) % len(_DEVICE_PROFILES)
                    if candidate not in used:
                        selected_idx = candidate
                        break

            assignments[key] = selected_idx
            payload = {
                "version": 1,
                "profile_count": len(_DEVICE_PROFILES),
                "assignments": assignments,
            }
            fh.seek(0)
            fh.truncate()
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            return _DEVICE_PROFILES[selected_idx]
    except Exception as exc:
        log.warning("Device profile allocation failed: %s; using hash fallback", exc)
        return _DEVICE_PROFILES[fallback_idx]


def generate_device_identity(seed: str) -> dict:
    """Generate a deterministic, unique device identity from a seed (e.g. phone number).

    Same seed always produces the same identity. Different seeds produce
    different identities. All fields match real Android device patterns.

    Returns dict with all fields needed for GojekClient constructor.
    """
    raw_seed = str(seed or "gopay-device")
    normalized_seed = re.sub(r"\D+", "", raw_seed) or raw_seed
    h = hashlib.sha256(raw_seed.encode()).digest()
    profile = _choose_device_profile(raw_seed, h)
    brand, manufacturer, model_name, platform, cpu_freq, cpu_cores, screen, android_versions, disk_choices = profile

    # Keep the legacy android_id derivation so already saved accounts do not rotate X-UniqueId.
    android_id = h[:8].hex()

    drm_id = hashlib.sha256(b"widevine:" + normalized_seed.encode()).digest()
    drm_id_b64 = base64.b64encode(drm_id).decode().rstrip("=")

    # WiFi MAC: locally-administered (bit 1 of first octet set)
    mac_bytes = h[8:14]
    mac_first = (mac_bytes[0] | 0x02) & 0xFE  # locally administered, unicast
    mac = f"{mac_first:02X}:{mac_bytes[1]:02X}:{mac_bytes[2]:02X}:{mac_bytes[3]:02X}:{mac_bytes[4]:02X}:{mac_bytes[5]:02X}"

    install_offset = _stable_int(h, 14, 6) % _INSTALL_WINDOW_MS
    base_ts = _INSTALL_START_MS + install_offset
    install_random = struct.unpack(">Q", h[14:22])[0]

    disk_mb = disk_choices[_stable_int(h, 22, 2) % len(disk_choices)]

    session_bytes = hashlib.sha256(b"session:" + normalized_seed.encode()).digest()[:16]
    session_id = str(uuid.UUID(bytes=session_bytes))
    android_ver = android_versions[_stable_int(h, 24, 2) % len(android_versions)]

    xm1 = (
        f"1:UNKNOWN,2:UNKNOWN"
        f",3:{base_ts}-{install_random}"
        f",4:{disk_mb}"
        f",5:{platform}|{cpu_freq}|{cpu_cores}"
        f",6:{mac}"
        f',7:<unknown ssid>'
        f",8:{screen}"
        r",9:passive\,fused\,gps"
        f",10:0"
        f",11:{drm_id_b64}"
        f",12:VKEY_DISABLED"
        f",13:1003"
        f",14:{int(time.time())}"
        f",16:0,17:1"
    )

    return {
        "d1": ORIGINAL_D1,
        "model": f"{brand},{model_name}",
        "uniqueid": android_id,
        "xm1": xm1,
        "phone_make": manufacturer,
        "os_info": f"Android,{android_ver}",
        "appid": GOPAY_APP_ID,
        "version": GOPAY_APP_VERSION,
        "session_id": session_id,
    }


@dataclass
class AuthState:
    """Mutable auth state accumulated across the login flow."""

    transaction_id: str = ""
    verification_id: str = ""
    otp_token: str = ""
    otp_length: int = 4
    otp_channel: str = ""
    verification_token: str = ""
    onefa_token: str = ""
    account_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    twofa_token: str = ""
    twofa_methods: list = field(default_factory=list)
    user_registered: bool = True
    methods: list = field(default_factory=list)

    # PIN flow state
    pin_otp_auth_token: str = ""
    pin_challenge_id: str = ""
    pin_client_id: str = ""
    pin_token: str = ""


class GojekClient:
    """Complete Gojek/GoPay protocol client."""

    def __init__(
        self,
        *,
        d1: str,
        model: str,
        uniqueid: str,
        xm1: str,
        phone_make: str = "Google",
        os_info: str = "Android,13",
        appid: str = GOPAY_APP_ID,
        version: str = GOPAY_APP_VERSION,
        user_uuid: str = "",
        session_id: str = "",
        device_token: str = "",
        access_token: str = "",
        refresh_token: str = "",
        proxy: str = "",
    ):
        self.d1 = d1
        self.model = model
        self.uniqueid = uniqueid
        self.xm1_template = xm1
        self.phone_make = phone_make
        self.os_info = os_info
        self.appid = appid
        self.version = version
        self.user_uuid = user_uuid
        self.session_id = session_id or str(uuid.uuid4())
        self.device_token = device_token
        self.proxy = normalize_proxy_url(proxy)

        self.auth = AuthState(
            access_token=access_token,
            refresh_token=refresh_token,
            transaction_id=str(uuid.uuid4()),
        )

        self._session = self._create_session()

    def _create_session(self) -> tls_client.Session:
        """Create TLS session with optional SOCKS5/HTTP proxy."""
        s = tls_client.Session(
            client_identifier="okhttp4_android_13",
            random_tls_extension_order=True,
            force_http1=True,
        )
        if self.proxy:
            s.proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }
        return s

    @classmethod
    def from_phone(cls, phone: str, proxy: str = "", device_seed: str = "") -> "GojekClient":
        """Create client with deterministic device identity derived from phone number.

        Same phone always gets the same device fingerprint (android_id, MAC, DRM ID, etc).
        Different phones get completely different identities.

        Args:
            phone: Phone number as seed for device identity
            proxy: SOCKS5/HTTP proxy URL, e.g. "socks5://user:pass@host:port"
                   or "http://user:pass@host:port"
        """
        seed_suffix = device_seed or os.environ.get("OPAI_GOPAY_DEVICE_SEED_SUFFIX", "").strip()
        identity_seed = f"{phone}:{seed_suffix}" if seed_suffix else phone
        identity = generate_device_identity(identity_seed)
        identity["proxy"] = proxy
        return cls(**identity)

    @classmethod
    def from_device_info(
        cls,
        appinfo_path: str,
        headers_path: Optional[str] = None,
    ) -> "GojekClient":
        """Create from captured device appinfo + headers files."""
        with open(appinfo_path) as f:
            lines = f.read().strip().split("\n")
        fields = {}
        for line in lines:
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v

        hdrs = {}
        if headers_path:
            with open(headers_path) as f:
                for line in f.read().strip().split("\n"):
                    if (
                        ": " in line
                        and not line.startswith("URL:")
                        and not line.startswith("TIME:")
                        and line != "---END---"
                    ):
                        k, v = line.split(": ", 1)
                        hdrs[k] = v

        return cls(
            d1=fields.get("supportPdam", ""),
            model=fields.get("supportBpjs", "google,sdk_gphone64_x86_64"),
            uniqueid=fields.get("supportInsurance", ""),
            xm1=fields.get("supportInternetCable", ""),
            phone_make=hdrs.get("X-PhoneMake", "Google"),
            user_uuid=hdrs.get("User-uuid", ""),
            session_id=hdrs.get("X-Session-ID", ""),
            device_token=hdrs.get("X-DeviceToken", ""),
            access_token=fields.get("supportPulsa", ""),
        )

    # ========================================================================
    # Internal: header builders
    # ========================================================================

    def _build_xm1(self) -> str:
        ts_sec = str(int(time.time()))
        return re.sub(r"14:\d+", f"14:{ts_sec}", self.xm1_template)

    def _user_agent(self) -> str:
        return f"GoPay/{self.version} ({self.appid}; build:{GOPAY_APP_BUILD}; {self.os_info})"

    def _sso_headers(self, extra: Optional[dict] = None) -> dict:
        """Headers for SSO / CVS endpoints (no WibbleDazzle signing)."""
        h = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-AppType": "GOPAY",
            "X-UniqueId": self.uniqueid,
            "X-Platform": "Android",
            "X-DeviceOS": self.os_info,
            "X-DeviceToken": self.device_token,
            "X-PhoneMake": self.phone_make,
            "X-PhoneModel": self.model,
            "X-User-Type": "customer",
            "X-User-Locale": "en_ID",
            "X-Help-Version": self.version,
            "X-AuthSDK-Version": "1.0.0",
            "Transaction-ID": self.auth.transaction_id,
            "User-Agent": self._user_agent(),
            "Accept-Language": "en-ID",
            "Accept-Encoding": "gzip, deflate, br",
        }
        if self.auth.access_token:
            tok = self.auth.access_token
            if not tok.startswith("Bearer "):
                tok = f"Bearer {tok}"
            h["Authorization"] = tok
        if extra:
            h.update(extra)
        return h

    def _gopay_signed_headers(
        self,
        path: str,
        method: str = "GET",
        body: str = "",
        extra: Optional[dict] = None,
        host: str = "customer.gopayapi.com",
    ) -> dict:
        """Headers for GoPay customer API (with WibbleDazzle signing).

        Header set verified via VM memory scan (2026-05-16) against GoPay 2.7.0 app.
        """
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))

        sig = sign_v2(
            token=self.auth.access_token,
            timestamp_ms=ts,
            url=f"{host}{path}",
            method=method,
            body=body,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )

        tok = self.auth.access_token
        if tok and not tok.startswith("Bearer "):
            tok = f"Bearer {tok}"

        h = {
            "User-Agent": self._user_agent(),
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "Authorization": tok,
            "X-User-Type": "customer",
            "X-AppType": "GOPAY",
            "X-DeviceOS": self.os_info,
            "User-uuid": self.user_uuid,
            "X-DeviceToken": self.device_token,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "Accept-Language": "id-ID",
            "X-User-Locale": "id_ID",
            "X-Location": "-6.2088,106.8456",
            "X-Location-Accuracy": "5.0",
            "Gojek-Country-Code": "ID",
            "Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "br,gzip",
            "X-Dark-Mode": "false",
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "support-sdk-version": "0.49.1",
        }
        if extra:
            h.update(extra)
        return h

    def _gojek_api_signed_headers(
        self, path: str, method: str = "POST", body: str = "", extra: Optional[dict] = None
    ) -> dict:
        """Headers for api.gojekapi.com — needs WibbleDazzle signing like GoPay."""
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))

        sig = sign_v2(
            token=self.auth.access_token,
            timestamp_ms=ts,
            url=f"api.gojekapi.com{path}",
            method=method,
            body=body,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )

        tok = self.auth.access_token
        if tok and not tok.startswith("Bearer "):
            tok = f"Bearer {tok}"

        h = {
            "User-Agent": self._user_agent(),
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "User-uuid": self.user_uuid,
            "X-DeviceToken": self.device_token,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "Accept-Language": "en-ID",
            "X-User-Locale": "en_ID",
            "X-Location": "-6.2088,106.8456",
            "X-Location-Accuracy": "5.0",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "br,gzip",
            "X-Dark-Mode": "false",
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "support-sdk-version": "0.49.1",
        }
        if tok:
            h["Authorization"] = tok
        if extra:
            h.update(extra)
        return h

    def _support_signed_headers(
        self,
        path: str,
        method: str,
        body: str,
        support_request_id: str,
        extra: Optional[dict] = None,
    ) -> dict:
        """Headers for unauthenticated support SDK calls.

        Latest capture sends these through the standalone GoPay SDK surface:
        GoPay/2.8.0, support-sdk-version 0.44.0, no Authorization header.
        """
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))
        sig = sign_v2(
            token="",
            timestamp_ms=ts,
            url=f"customer.gopayapi.com{path}",
            method=method,
            body=body,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=SUPPORT_APP_ID,
            version=SUPPORT_APP_VERSION,
            phone_make=self.phone_make,
        )
        h = {
            "User-Agent": f"GoPay/{SUPPORT_APP_VERSION} ({SUPPORT_APP_ID}; build:{SUPPORT_APP_BUILD}; {self.os_info})",
            "D1": self.d1,
            "X-Session-ID": self.session_id,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-AppVersion": SUPPORT_APP_VERSION,
            "X-AppId": SUPPORT_APP_ID,
            "X-User-Type": "customer",
            "X-AppType": "GOPAY",
            "X-DeviceOS": self.os_info,
            "User-uuid": self.user_uuid,
            "X-DeviceToken": self.device_token,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "Accept-Language": "id-ID",
            "X-User-Locale": "id_ID",
            "X-Location": "-6.2088,106.8456",
            "X-Location-Accuracy": "5.0",
            "Gojek-Country-Code": "ID",
            "Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "br,gzip",
            "X-Dark-Mode": "false",
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "support-request-id": support_request_id,
            "support-sdk-version": SUPPORT_SDK_VERSION,
        }
        if extra:
            h.update(extra)
        h.pop("Authorization", None)
        return h

    # ========================================================================
    # Internal: HTTP helpers
    # ========================================================================

    def _gojek_api_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        body_str = _json_body(body) if body is not None else ""
        headers = self._gojek_api_signed_headers(path, method, body_str, extra_headers)
        log.debug("%s %s Authorization=%s", method, path, headers.get("Authorization", "(MISSING)"))
        fn = getattr(self._session, method.lower())
        kwargs = {"headers": headers, "timeout_seconds": 15}
        if body_str:
            kwargs["data"] = body_str
        resp = fn(f"{GOJEK_API_BASE}{path}", **kwargs)
        log.debug("GojekAPI %s %s → %d", method, path, resp.status_code)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _gojek_api_get(self, path: str, extra_headers: Optional[dict] = None) -> dict:
        return self._gojek_api_request("GET", path, None, extra_headers)

    def _gojek_api_post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        return self._gojek_api_request("POST", path, body, extra_headers)

    def _gojek_api_put(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        return self._gojek_api_request("PUT", path, body, extra_headers)

    def _sso_post(
        self,
        path: str,
        body: dict,
        extra_headers: Optional[dict] = None,
        sign_path: Optional[str] = None,
    ) -> dict:
        body_str = _json_body(body)
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))
        canonical_path = sign_path or path
        sig = sign_v2(
            token=self.auth.access_token,
            timestamp_ms=ts,
            url=f"accounts.goto-products.com{canonical_path}",
            method="POST",
            body=body_str,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )
        headers = self._sso_headers(extra_headers)
        headers.update({
            "X-M1": xm1,
            "X-CVSDK-Version": "1.0.0",
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-Request-ID": str(uuid.uuid1()),
            "Gojek-Country-Code": "ID",
            "Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "gzip",
        })
        for _retry in range(SSO_RETRIES):
            try:
                resp = self._session.post(
                    f"{SSO_BASE}{path}",
                    headers=headers,
                    data=body_str,
                    timeout_seconds=SSO_TIMEOUT_SEC,
                )
                break
            except Exception as e:
                if _retry < SSO_RETRIES - 1:
                    log.warning("SSO POST %s retry %d: %s", path, _retry + 1, e)
                    time.sleep(SSO_RETRY_SLEEP_SEC)
                else:
                    raise
        log.debug("SSO POST %s → %d", path, resp.status_code)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _sso_cvs_initiate(self, body: dict, extra_headers: Optional[dict] = None) -> dict:
        if CVS_DOUBLE_SLASH_INITIATE:
            extra = dict(extra_headers or {})
            extra.setdefault("key", "value")
            return self._sso_post(
                "/cvs/v1//initiate",
                body,
                extra,
                sign_path="/cvs/v1/initiate",
            )
        return self._sso_post("/cvs/v1/initiate", body, extra_headers)

    def _gopay_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        body_str = _json_body(body) if body else ""
        headers = self._gopay_signed_headers(path, method, body_str, extra_headers)
        url = f"{GOPAY_BASE}{path}"
        fn = getattr(self._session, method.lower())
        kwargs = {"headers": headers, "timeout_seconds": 15}
        if body_str:
            kwargs["data"] = body_str
        for _retry in range(3):
            try:
                resp = fn(url, **kwargs)
                break
            except Exception as e:
                if _retry < 2:
                    log.warning("GoPay %s %s retry %d: %s", method, path, _retry + 1, e)
                    time.sleep(2)
                else:
                    raise
        log.debug("GoPay %s %s → %d", method, path, resp.status_code)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _external_signed_request(
        self,
        base_url: str,
        host: str,
        method: str,
        path: str,
        body: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        body_str = _json_body(body) if body else ""
        headers = self._gopay_signed_headers(path, method, body_str, extra_headers, host=host)
        fn = getattr(self._session, method.lower())
        kwargs = {"headers": headers, "timeout_seconds": 15}
        if body_str:
            kwargs["data"] = body_str
        resp = fn(f"{base_url}{path}", **kwargs)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def _gopay_get(self, path: str) -> dict:
        return self._gopay_request("GET", path)

    def _gopay_post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        return self._gopay_request("POST", path, body, extra_headers)

    def _gopay_put(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        return self._gopay_request("PUT", path, body, extra_headers)

    def _gopay_patch(self, path: str, body: dict) -> dict:
        return self._gopay_request("PATCH", path, body)

    def _gopay_delete(self, path: str) -> dict:
        return self._gopay_request("DELETE", path)

    def _support_post(self, path: str, *, prefer_shortest: bool = False) -> dict:
        body = get_support_body_provider().next_body(path, prefer_shortest=prefer_shortest)
        body_str = json.dumps(body, separators=(",", ":"))
        support_request_id = str(uuid.uuid1())
        headers = self._support_signed_headers(path, "POST", body_str, support_request_id)
        resp = self._session.post(
            f"{GOPAY_BASE}{path}",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        log.debug("Support SDK POST %s → %d body_lengths=%s", path, resp.status_code, support_body_lengths(body))
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def support_customer_initiate(self, *, prefer_shortest: bool = False) -> dict:
        """POST /v1/support/customer/initiate with captured SDK encrypted body."""
        return self._support_post("/v1/support/customer/initiate", prefer_shortest=prefer_shortest)

    def support_customer_actions(self) -> dict:
        """POST /v1/support/customer/actions with captured SDK encrypted body."""
        return self._support_post("/v1/support/customer/actions")

    def support_customer_session(self) -> dict:
        """POST /v1/support/customer/session with captured SDK encrypted body."""
        return self._support_post("/v1/support/customer/session")

    def support_customer_activity(self) -> dict:
        """POST /v1/support/customer/activity with captured SDK encrypted body."""
        return self._support_post("/v1/support/customer/activity")

    # ========================================================================
    # Phase 0: Signup — Legacy registration (api.gojekapi.com)
    #   Source: jadx_classes/com/gojek/app/api/signup/SignUpApi.java
    #   Status: ⚠️ UNVERIFIED — X-DeviceCheckToken + X-Signature generation TBD
    # ========================================================================

    def _signup_headers(self, extra: Optional[dict] = None) -> dict:
        """Headers for signup endpoints — NO WibbleDazzle signing."""
        xm1 = self._build_xm1()
        h = {
            "D1": self.d1,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "User-Agent": self._user_agent(),
            "X-Session-ID": self.session_id,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PushTokenType": "FCM",
            "X-PhoneModel": self.model,
            "X-M1": xm1,
            "X-CVSDK-Version": "3.73.0",
            "X-AuthSDK-Version": "3.103.0",
            "Accept-Language": "en-ID",
            "X-User-Locale": "en_ID",
            "X-DeviceCheckToken": "LITMUS_DISABLED",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "gzip",
        }
        if extra:
            h.update(extra)
        return h

    def _signup_post(self, url: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        headers = self._signup_headers(extra_headers)
        body_str = _json_body(body)
        resp = self._session.post(url, headers=headers, data=body_str, timeout_seconds=15)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    def signup_request_otp(self, phone: str, country_code: str = "+62") -> dict:
        """Full SSO signup OTP flow: login/methods → cvs/v1/methods → cvs/v1/initiate.

        HAR-verified flow (2026-05-15):
          1. login/methods → 401 user:not_found (expected for new number)
          2. cvs/v1/methods (flow="signup") → verification_id + methods
          3. cvs/v1/initiate (flow="signup") → otp_token
        """
        country_code = country_code if country_code.startswith("+") else f"+{country_code}"
        country_digits = country_code.lstrip("+")
        local = phone.lstrip("+")
        if local.startswith(country_digits):
            local = local[len(country_digits):]

        self.auth.transaction_id = str(uuid.uuid4())

        # Step 1: cvs/v1/methods to get verification_id
        methods_body = {
            "country_code": country_code,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": local,
            "client_secret": CLIENT_SECRET,
            "flow": "signup",
            "device_verification_token_id": None,
        }
        methods_result = self._sso_post("/cvs/v1/methods", methods_body)
        if methods_result["status"] not in (200, 201):
            return methods_result
        data = methods_result["body"].get("data", methods_result["body"])
        self.auth.verification_id = data.get("verification_id", "")
        self.auth.methods = data.get("methods", [])
        log.info("CVS methods: %s, vid=%s", self.auth.methods, self.auth.verification_id)

        # Step 2: cvs/v1/initiate to send OTP
        initiate_body = {
            "verification_id": self.auth.verification_id,
            "flow": "signup",
            "verification_method": "otp_sms",
            "country_code": country_code,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": local,
            "client_secret": CLIENT_SECRET,
            "is_multiple_method": None,
            "device_verification_token_id": None,
        }
        result = self._sso_cvs_initiate(initiate_body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.otp_token = inner.get("otp_token", "")
            self.auth.otp_length = inner.get("otp_length", 4)
            log.info("Signup OTP sent: otp_length=%d, otp_token=%s...",
                     self.auth.otp_length, self.auth.otp_token[:20])
        return result

    def signup_verify_otp(self, otp: str, phone: str = "") -> dict:
        """POST /cvs/v1/verify (flow=signup) → returns JWE verification_token.

        HAR-verified: uses same flow/verification_method as initiate.
        """
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": "signup",
            "verification_method": "otp_sms",
            "verification_id": self.auth.verification_id,
            "data": {
                "otp": otp,
                "otp_token": self.auth.otp_token,
            },
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            data = result["body"]
            inner = data.get("data", data)
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("Signup verified, token=%s...", self.auth.verification_token[:40])
        return result

    def signup_create_account(
        self,
        name: str,
        phone: str,
        email: str = "",
        country: str = "",
    ) -> dict:
        """POST /v7/customers/signup → create Gojek account.

        HAR-verified (2026-05-15): uses JWE from cvs/v1/verify as Verification-Token,
        Basic auth for gateway, GoPay 2.8.0 enhanced signing (X-E1/X-E2).
        """
        signed_up_country = str(country or SIGNUP_SIGNED_UP_COUNTRY or "62").lstrip("+") or "62"
        country_code = f"+{signed_up_country}" if signed_up_country.isdigit() else "+62"
        body = {
            "client_name": SIGNUP_CLIENT_NAME,
            "client_secret": CLIENT_SECRET,
            "data": {
                "name": name,
                "phone": _normalize_signup_phone(phone, country_code),
                "email": email,
                "signed_up_country": signed_up_country,
                "onboarding_partner": SIGNUP_ONBOARDING_PARTNER,
            },
        }
        client_auth = base64.b64encode(SIGNUP_BASIC_AUTH_ID.encode()).decode()
        xm1 = self._build_xm1()
        body_str = _json_body(body)
        ts = str(int(time.time() * 1000))
        sig = sign_v2(
            token=f"Basic {client_auth}",
            timestamp_ms=ts,
            url="api.gojekapi.com/v7/customers/signup",
            method="POST",
            body=body_str,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )
        vtoken = self.auth.verification_token
        if vtoken.startswith("Bearer "):
            vtoken = vtoken[7:]
        headers = {
            "X-DeviceCheckToken": "LITMUS_DISABLED",
            "X-Signature": "1003",
            "X-Signature-Time": str(int(time.time())),
            "Verification-Token": f"Bearer {vtoken}",
            "Authorization": f"Basic {client_auth}",
            "User-Agent": self._user_agent(),
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-AppType": "GOPAY",
            "X-Help-Version": self.version,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "Accept": "application/json",
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PhoneModel": self.model,
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-DeviceToken": self.device_token,
            "Content-Type": "application/json",
            "Gojek-Country-Code": "ID",
            "Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "X-User-Locale": "en_ID",
            "Accept-Language": "en-ID",
            "Accept-Encoding": "gzip, deflate, br",
        }
        resp = self._session.post(
            f"{GOJEK_API_BASE}/v7//customers/signup",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        result = {"status": resp.status_code, "body": data}
        if result["status"] in (200, 201, 206):
            data = result["body"].get("data", result["body"])
            self.auth.access_token = data.get("access_token", "")
            self.auth.refresh_token = data.get("refresh_token", "")
            customer = data.get("customer", {}) if isinstance(data, dict) else {}
            uid = data.get("resource_owner_id", "") or customer.get("id", "")
            if uid:
                self.user_uuid = str(uid)
            if result["status"] in (200, 201):
                log.info("Signup success: uid=%s, access_token=%s...", self.user_uuid, self.auth.access_token[:30])
            else:
                log.warning("Signup created without token: uid=%s", self.user_uuid)
        return result

    def signup_create_account_v2(
        self,
        name: str,
        phone: str,
        email: str = "",
        country: str = "",
    ) -> dict:
        """POST /v6/customers/register → create Gojek account (V2 / legacy).

        Uses PVToken header with JWT from /v6/customers/phone/verify.
        This is the correct endpoint for the newrequest→phone/verify flow.
        """
        body = {
            "client_name": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "name": name,
                "phone": phone,
                "email": email,
                "signed_up_country": country,
                "onboarding_partner": SIGNUP_ONBOARDING_PARTNER,
            },
        }
        xm1 = self._build_xm1()
        ts = str(int(time.time() * 1000))
        body_str = _json_body(body)
        sig = sign_v2(
            token="",
            timestamp_ms=ts,
            url=f"api.gojekapi.com/v6/customers/register",
            method="POST",
            body=body_str,
            d1=self.d1,
            model=self.model,
            xm1=xm1,
            uniqueid=self.uniqueid,
            os_info=self.os_info,
            appid=self.appid,
            version=self.version,
            phone_make=self.phone_make,
        )
        headers = {
            "X-DeviceCheckToken": "LITMUS_DISABLED",
            "X-Signature": "1003",
            "X-Signature-Time": str(int(time.time())),
            "PVToken": self.auth.verification_token,
            "D1": self.d1,
            "X-Platform": "Android",
            "X-UniqueId": self.uniqueid,
            "User-Agent": self._user_agent(),
            "X-Session-ID": self.session_id,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "X-AppVersion": self.version,
            "X-AppId": self.appid,
            "X-User-Type": "customer",
            "X-DeviceOS": self.os_info,
            "X-PhoneMake": self.phone_make,
            "X-PhoneModel": self.model,
            "X-M1": xm1,
            "X-E1": sig["X-E1"],
            "X-E2": sig["X-E2"],
            "X-CVSDK-Version": "1.0.0",
            "X-AuthSDK-Version": "1.0.0",
            "Accept-Language": "en-ID",
            "Gojek-Country-Code": "ID",
            "Gojek-Service-Area": "1",
            "Gojek-Timezone": "Asia/Jakarta",
            "Accept-Encoding": "gzip",
        }
        resp = self._session.post(
            f"{GOJEK_API_BASE}/v6/customers/register",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        result = {"status": resp.status_code, "body": data}
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.access_token = data.get("access_token", "")
            self.auth.refresh_token = data.get("refresh_token", "")
            log.info("Signup V2 success: access_token=%s...", self.auth.access_token[:30])
        return result

    # ========================================================================
    # Phase 0b: GoPay Initialization (HAR-verified 2026-05-15)
    #
    # After signup + refresh_token:
    #   1. PUT customers/v1/country-change (empty body) → triggers GoPay wallet creation
    #   2. GET v1/payment-options/profiles → verify wallet exists
    #   3. GET v1/users/profile → check is_pin_setup
    #
    # NOTE: GoPay wallet is auto-created after country-change, no explicit register needed.
    # The old gopay_register endpoint may still work but is not used by the app.
    # ========================================================================

    def gopay_init(self) -> dict:
        """PUT /customers/v1/country-change → initialize GoPay wallet.

        HAR-verified: PUT with empty body, triggers wallet auto-creation.
        Must be called AFTER refresh_token (needs JWE access_token, not RS256).
        """
        return self._gopay_request("PUT", "/customers/v1/country-change")

    def gopay_get_profiles(self) -> dict:
        """GET /v1/payment-options/profiles → check GoPay wallet status."""
        return self._gopay_request("GET", "/v1/payment-options/profiles")

    def gopay_get_balances(self) -> dict:
        """GET /v1/payment-options/balances → get wallet balances."""
        return self._gopay_request("GET", "/v1/payment-options/balances")

    def gojek_customer_profile(self) -> dict:
        """GET api.gojekapi.com/gojek/v2/customer → refresh Gojek customer profile."""
        return self._gojek_api_get("/gojek/v2/customer")

    def litmus_public_experiments(self) -> dict:
        """GET /litmus/public/run/experiments with the latest device-scoped query shape."""
        query = urllib.parse.urlencode({"unit_id": self.uniqueid, "unit_type": "device"})
        return self._gojek_api_get(f"/litmus/public/run/experiments?{query}")

    def litmus_experiments(self) -> dict:
        """GET /litmus/run/experiments with the latest device-scoped query shape."""
        query = urllib.parse.urlencode({"unit_id": self.uniqueid, "unit_type": "device"})
        return self._gojek_api_get(f"/litmus/run/experiments?{query}")

    def chat_profile(self) -> dict:
        """GET /v2/chat/profile → initialize the post-login chat identity."""
        return self._gojek_api_get("/v2/chat/profile")

    def accept_signup_consents(self) -> dict:
        """POST /api/v2/consents/accept → accept the app signup consents captured on real device."""
        body = {
            "consents": [
                {"consent_name": "gopay_app_tnc", "user_type": "CUSTOMER", "flow": "signUp"},
                {"consent_name": "gopay_app_privacy_note", "user_type": "CUSTOMER", "flow": "signUp"},
                {"consent_name": "gojek_app_tnc", "user_type": "CUSTOMER", "flow": "signUp"},
                {"consent_name": "gojek_app_privacy_note", "user_type": "CUSTOMER", "flow": "signUp"},
            ]
        }
        return self._gopay_post("/api/v2/consents/accept", body)

    def gopay_home_v3(self) -> dict:
        """GET /bff/v1/screens/gopay-home-v3 → load the GoPay home screen payload."""
        return self._gopay_get("/bff/v1/screens/gopay-home-v3")

    def festivals_assets(self) -> dict:
        """GET /v1/festivals/assets → warm festival/envelope assets used by the app home."""
        return self._gopay_get("/v1/festivals/assets")

    def red_badges(self) -> dict:
        """GET /v1/users/red-badges → warm badge counters shown by the app."""
        return self._gopay_get("/v1/users/red-badges")

    def cross_sells(self) -> dict:
        """GET /v2/users/cross-sells → warm app cross-sell configuration."""
        return self._gopay_get("/v2/users/cross-sells")

    def kyc_status(self) -> dict:
        """GET /v2/users/kyc/status → warm KYC status used by the wallet home."""
        extra = {
            "X-OneKyc-Sdk-Host": "gojek",
            "X-OneKyc-Sdk-Host-AppId": self.appid,
            "X-OneKyc-Sdk-Host-Version": self.version,
            "X-OneKyc-Sdk-Platform": "android",
            "X-OneKyc-Sdk-Version": "1.0.0",
        }
        return self._gopay_request("GET", "/v2/users/kyc/status", extra_headers=extra)

    def wallet_card_balance(self, screen: str = "home_3_1") -> dict:
        """GET /v1/user/wallet-card/balance?screen=... → home wallet balance widget."""
        query = urllib.parse.urlencode({"screen": screen})
        return self._gopay_get(f"/v1/user/wallet-card/balance?{query}")

    def wallet_card_widget(self, screen: str = "home_3_1") -> dict:
        """GET /v1/user/wallet-card/widget?screen=... → wallet widget state."""
        query = urllib.parse.urlencode({"screen": screen})
        return self._gopay_get(f"/v1/user/wallet-card/widget?{query}")

    def security_meter(
        self,
        source: str = "gopay_home",
        *,
        view_count: Optional[int] = None,
        click_count: Optional[int] = None,
        security_aware_identifier: str = "",
    ) -> dict:
        """GET /v1/users/security-meter with the real-device query shape."""
        params: dict[str, str | int] = {
            "biometric-enrolled": "false",
            "biometric-supported": "true",
            "source": source,
        }
        if view_count is not None:
            params["view-count"] = view_count
        if click_count is not None:
            params["click-count"] = click_count
        if security_aware_identifier:
            params["security-aware-identifier"] = security_aware_identifier
        return self._gopay_get(f"/v1/users/security-meter?{urllib.parse.urlencode(params)}")

    def courier_token(self) -> dict:
        """GET api.gojekapi.com/courier/v1/token → open the app courier channel token."""
        return self._gojek_api_get("/courier/v1/token")

    def _stable_push_token(self) -> str:
        if self.device_token:
            return self.device_token
        seed = hashlib.sha256(f"fcm:{self.uniqueid}:{self.session_id}".encode()).digest()
        token = base64.urlsafe_b64encode(seed + hashlib.sha256(seed).digest()).decode().rstrip("=")
        self.device_token = f"{self.uniqueid}:APA91b{token[:140]}"
        return self.device_token

    def update_push_token(self) -> dict:
        """PUT api.gojekapi.com/v1/devices/push_token with a stable per-device FCM-shaped token."""
        token = self._stable_push_token()
        return self._gojek_api_put(
            "/v1/devices/push_token",
            {"push_token_type": "FCM", "push_token": token},
        )

    def gofin_token(self) -> dict:
        """POST /paylater/auth/partner/v1/auth/gofin-token → app post-login warmup token."""
        return self._gopay_request("POST", "/paylater/auth/partner/v1/auth/gofin-token")

    def paylater_profile(self) -> dict:
        """GET gateway.paylater.gofin.co.id/paylater-user/v2/profile → warm PayLater profile."""
        extra = {
            "Application-Source": "gojek",
            "Application-Version": self.version,
        }
        return self._external_signed_request(
            GOFIN_GATEWAY_BASE,
            "gateway.paylater.gofin.co.id",
            "GET",
            "/paylater-user/v2/profile?initialRoute=",
            extra_headers=extra,
        )

    # ========================================================================
    # Phase 1: Login / Registration (SSO — accounts.goto-products.com)
    #   HAR-verified 2026-05-16 (ProxyPin5-16_13_05_54.har)
    #
    #   Login flow (existing user with PIN):
    #     1. login/methods → methods=[goto_pin, otp_wa, otp_sms], verification_id
    #     2. cvs/v1/initiate (flow=login_1fa, method=goto_pin) → challenge_id
    #     3. pin/tokens/nb (challenge_id, client_id, pin) → pin_token JWT
    #     4. cvs/v1/verify (data={challenge_id, validation_jwt=pin_token}) → JWE
    #     5. accountlist → account_id, 1fa_token
    #     6. goto-auth/token (grant_type=cvs, token=1fa_token) → 403 + 2fa_token
    #     7. cvs/v1/initiate (flow=login_2fa, method=otp_sms) → otp_token
    #     8. cvs/v1/verify (flow=login_2fa, otp) → JWE
    #     9. goto-auth/token (grant_type=challenge, token=2fa_token) → access_token!
    # ========================================================================

    LOGIN_PIN_CLIENT_ID = "6d11d261d7ae462dbd4be0dc5f36a697-MFAGOJEK"

    def get_login_methods(self, country_code: str, phone: str) -> dict:
        """Step 1: POST /goto-auth/login/methods → available auth methods."""
        self.auth.transaction_id = str(uuid.uuid4())
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "country_code": country_code,
            "device_verification_token_id": "",
            "email": "",
            "phone_number": phone,
        }
        result = self._sso_post("/goto-auth/login/methods", body)
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.verification_id = data.get("verification_id", "")
            self.auth.methods = data.get("methods", [])
            log.info("Login methods: %s, vid=%s", self.auth.methods, self.auth.verification_id)
        return result

    def initiate_otp(
        self,
        country_code: str = "",
        phone: str = "",
        method: str = "",
        flow: str = "login_1fa",
        is_multiple_method: bool = True,
    ) -> dict:
        """POST /cvs/v1/initiate → trigger verification (PIN, OTP SMS, OTP WA).

        HAR-verified: body includes is_multiple_method for login flows.
        For goto_pin: returns challenge_id (not otp_token).
        For otp_sms/otp_wa: returns otp_token.
        """
        if not method:
            method = self.auth.methods[0] if self.auth.methods else "otp_sms"
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": flow,
            "verification_id": self.auth.verification_id,
            "verification_method": method,
        }
        if country_code:
            body["country_code"] = country_code
        if phone:
            body["phone_number"] = phone
        if is_multiple_method:
            body["is_multiple_method"] = True
        extra = {}
        if self.auth.access_token:
            tok = self.auth.access_token
            if not tok.startswith("Bearer "):
                tok = f"Bearer {tok}"
            extra["Authorization"] = tok
        result = self._sso_cvs_initiate(body, extra)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.otp_token = inner.get("otp_token", "")
            self.auth.otp_length = inner.get("otp_length", 4)
            self.auth.pin_challenge_id = inner.get("challenge_id", "")
            log.info("CVS initiate: otp_token=%s challenge_id=%s",
                     self.auth.otp_token[:20] if self.auth.otp_token else "(none)",
                     self.auth.pin_challenge_id or "(none)")
        return result

    def login_pin_verify(self, pin: str) -> dict:
        """POST /api/v1/users/pin/tokens/nb → verify PIN for login.

        HAR-verified: uses challenge_id from initiate(goto_pin), returns pin_token JWT.
        """
        body = {
            "challenge_id": self.auth.pin_challenge_id,
            "client_id": self.LOGIN_PIN_CLIENT_ID,
            "pin": pin,
        }
        body_str = _json_body(body)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_2_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://pin-web-client.gopayapi.com",
            "Referer": "https://pin-web-client.gopayapi.com/",
            "x-appversion": "1.0.0",
            "x-correlation-id": str(uuid.uuid4()),
            "x-is-mobile": "false",
            "x-platform": "Mac OS 12.2.1",
            "x-request-id": str(uuid.uuid4()),
            "x-user-locale": "id",
        }
        resp = self._session.post(
            f"{GOPAY_BASE}/api/v1/users/pin/tokens/nb",
            headers=headers,
            data=body_str,
            timeout_seconds=15,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        result = {"status": resp.status_code, "body": data}
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
            log.info("Login PIN verified, token=%s...", self.auth.pin_token[:40] if self.auth.pin_token else "(empty)")
        return result

    def verify_pin_via_cvs(self) -> dict:
        """POST /cvs/v1/verify with PIN token → JWE verification_token.

        HAR-verified: data={challenge_id, validation_jwt=pin_token}, flow=login_1fa.
        """
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "challenge_id": self.auth.pin_challenge_id,
                "validation_jwt": self.auth.pin_token,
            },
            "flow": "login_1fa",
            "verification_id": self.auth.verification_id,
            "verification_method": "goto_pin",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("PIN CVS verified, token=%s...", self.auth.verification_token[:30])
        return result

    def verify_otp(self, otp: str, flow: str = "login_2fa") -> dict:
        """POST /cvs/v1/verify → submit OTP code.

        Returns verification_token (JWE).
        """
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "otp": otp,
                "otp_token": self.auth.otp_token,
            },
            "flow": flow,
            "verification_id": self.auth.verification_id,
            "verification_method": self.auth.otp_channel or "otp_sms",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("OTP verified, token=%s...", self.auth.verification_token[:30])
        return result

    def retry_otp(self, flow: str = "login_2fa") -> dict:
        """POST /cvs/v2/retry → resend OTP."""
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {"otp_token": self.auth.otp_token},
            "flow": flow,
            "verification_method": "OTP",
            "verification_id": self.auth.verification_id,
        }
        return self._sso_post("/cvs/v2/retry", body)

    def check_otp_status(self) -> dict:
        """POST /cvs/v1/fallback-status → poll OTP delivery status."""
        body = {
            "otp_token": self.auth.otp_token,
            "verification_id": self.auth.verification_id,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        return self._sso_post("/cvs/v1/fallback-status", body)

    def get_account_list(self) -> dict:
        """POST /goto-auth/accountlist → account list + 1FA token."""
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        vtoken = self.auth.verification_token
        if vtoken and not vtoken.startswith("Bearer "):
            vtoken = f"Bearer {vtoken}"
        extra = {"verification-token": vtoken}
        result = self._sso_post("/goto-auth/accountlist", body, extra)
        if result["status"] in (200, 201):
            data = result["body"].get("data", result["body"])
            self.auth.onefa_token = data.get("1fa_token", "")
            accounts = data.get("account_list", [])
            if accounts:
                self.auth.account_id = str(accounts[0].get("account_id", ""))
                if self.auth.account_id:
                    self.user_uuid = self.auth.account_id
            log.info("Account list: %d accounts, account_id=%s", len(accounts), self.auth.account_id)
        return result

    def issue_token(self, grant_type: str = "cvs", token_value: str = "") -> dict:
        """POST /goto-auth/token → access_token + refresh_token.

        HAR-verified grant_type flow:
          1FA: grant_type="cvs", token=1fa_token → 403 (needs 2FA) → returns 2fa_token
          2FA: grant_type="challenge", token=2fa_token → 201 → access_token!
        """
        if not token_value:
            token_value = self.auth.onefa_token or self.auth.verification_token

        if grant_type == "refresh_token":
            body = {
                "grant_type": "refresh_token",
                "token": token_value,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }
        else:
            body = {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": grant_type,
                "token": token_value,
            }

        extra = {}
        if grant_type in {"cvs", "challenge"} and self.auth.account_id:
            body["account_id"] = self.auth.account_id
        if grant_type in {"cvs", "challenge"}:
            body["ext_user_token"] = None

        if grant_type == "challenge" and self.auth.verification_token:
            vtoken = self.auth.verification_token
            if not vtoken.startswith("Bearer "):
                vtoken = f"Bearer {vtoken}"
            extra["verification-token"] = vtoken
        elif grant_type == "cvs" and self.auth.onefa_token:
            extra["verification-token"] = self.auth.onefa_token
        elif grant_type not in {"refresh_token"} and self.auth.verification_token:
            vtoken = self.auth.verification_token
            if not vtoken.startswith("Bearer "):
                vtoken = f"Bearer {vtoken}"
            extra["verification-token"] = vtoken
        if grant_type not in {"refresh_token"} and self.auth.access_token:
            tok = self.auth.access_token
            if not tok.startswith("Bearer "):
                tok = f"Bearer {tok}"
            extra["Authorization"] = tok

        result = self._sso_post("/goto-auth/token", body, extra)
        status = result["status"]
        data = result["body"]

        if status in (200, 201):
            inner = data.get("data", data)
            self.auth.access_token = inner.get("access_token", "")
            self.auth.refresh_token = inner.get("refresh_token", "")
            log.info(
                "Token issued: access=%s..., refresh=%s...",
                self.auth.access_token[:30],
                self.auth.refresh_token[:30] if self.auth.refresh_token else "(none)",
            )
        elif status == 403:
            inner = data.get("data", data) if isinstance(data, dict) else {}
            self.auth.twofa_token = inner.get("2fa_token", "")
            self.auth.twofa_methods = inner.get("methods", [])
            vid = inner.get("verification_id", "")
            if vid:
                self.auth.verification_id = vid
            log.info("2FA required: methods=%s, 2fa_token=%s...",
                     self.auth.twofa_methods,
                     self.auth.twofa_token[:30] if self.auth.twofa_token else "(none)")
        else:
            log.warning("Token issue failed: %d %s", status, data)
        return result

    def login(self, country_code: str, phone: str, pin: str, otp_callback=None, progress_callback=None) -> dict:
        """Complete login flow: PIN (1FA) → OTP (2FA) → access_token.

        HAR-verified flow (2026-05-16):
          1. login/methods → goto_pin + otp_sms
          2. cvs/initiate(login_1fa, goto_pin) → challenge_id
          3. pin/tokens/nb → pin_token
          4. cvs/verify(login_1fa, pin_token) → JWE
          5. accountlist → 1fa_token
          6. goto-auth/token(cvs) → 403 + 2fa_token
          7. cvs/initiate(login_2fa, otp_sms) → otp_token
          8. [wait for OTP via otp_callback]
          9. cvs/verify(login_2fa, otp) → JWE
         10. goto-auth/token(challenge) → access_token!

        Args:
            otp_callback: function() -> str that returns OTP code (blocking wait)
        """
        def progress(message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(message)
            except Exception:
                pass

        # Step 1: login/methods
        progress("登录步骤 1/10：查询账号登录方式")
        methods = self.get_login_methods(country_code, phone)
        if methods["status"] not in (200, 201):
            return methods

        # Step 2: initiate PIN (1FA)
        has_pin = "goto_pin" in self.auth.methods
        if has_pin:
            progress("登录步骤 2/10：创建 PIN 验证 challenge")
            init1 = self.initiate_otp(country_code, phone, method="goto_pin", flow="login_1fa")
            if init1["status"] not in (200, 201):
                return init1

            # Step 3: verify PIN
            progress("登录步骤 3/10：提交 PIN 验证")
            pin_result = self.login_pin_verify(pin)
            if pin_result["status"] not in (200, 201):
                return pin_result

            # Step 4: CVS verify with PIN token
            progress("登录步骤 4/10：换取 PIN 验证令牌")
            cvs_pin = self.verify_pin_via_cvs()
            if cvs_pin["status"] not in (200, 201):
                return cvs_pin
        else:
            # No PIN, use OTP directly for 1FA
            progress("登录步骤 2/10：账号未要求 PIN，发送一阶段 OTP")
            init1 = self.initiate_otp(country_code, phone, method="otp_sms", flow="login_1fa")
            if init1["status"] not in (200, 201):
                return init1
            if otp_callback:
                otp = otp_callback()
                if not otp:
                    return {"status": 0, "body": {"error": "OTP not received"}}
                verify1 = self.verify_otp(otp, flow="login_1fa")
                if verify1["status"] not in (200, 201):
                    return verify1

        # Step 5: accountlist
        progress("登录步骤 5/10：读取账号列表")
        acct = self.get_account_list()
        if acct["status"] not in (200, 201):
            return acct

        # Step 6: issue token (1FA) → likely 403 needing 2FA
        progress("登录步骤 6/10：申请登录 token")
        token1 = self.issue_token(grant_type="cvs", token_value=self.auth.onefa_token)
        if token1["status"] in (200, 201):
            return token1  # Done! (no 2FA needed)

        if token1["status"] != 403 or not self.auth.twofa_token:
            return token1  # Unexpected error

        # Step 7: initiate OTP for 2FA
        progress("登录步骤 7/10：发送登录 OTP")
        self.auth.otp_channel = "otp_sms"
        init2 = self.initiate_otp(country_code, phone, method="otp_sms", flow="login_2fa")
        if init2["status"] not in (200, 201):
            return init2

        # Step 8: wait for OTP
        if not otp_callback:
            return {"status": 0, "body": {"error": "2FA OTP required but no callback", "otp_token": self.auth.otp_token}}
        otp = otp_callback()
        if not otp:
            return {"status": 0, "body": {"error": "2FA OTP not received"}}

        # Step 9: verify 2FA OTP
        progress("登录步骤 9/10：验证登录 OTP")
        verify2 = self.verify_otp(otp, flow="login_2fa")
        if verify2["status"] not in (200, 201):
            return verify2

        # Step 10: issue token with 2fa_token
        progress("登录步骤 10/10：换取最终 access token")
        return self.issue_token(grant_type="challenge", token_value=self.auth.twofa_token)

    def refresh_token(self) -> dict:
        """Refresh access_token using refresh_token."""
        return self.issue_token(
            grant_type="refresh_token",
            token_value=self.auth.refresh_token,
        )

    def logout(self) -> dict:
        """DELETE /goto-auth/token → revoke tokens."""
        tok = self.auth.access_token
        if tok and not tok.startswith("Bearer "):
            tok = f"Bearer {tok}"
        headers = self._sso_headers({"Authorization": tok})
        resp = self._session.delete(
            f"{SSO_BASE}/goto-auth/token", headers=headers, timeout_seconds=15
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return {"status": resp.status_code, "body": data}

    # ========================================================================
    # Convenience: full login/register flow
    # ========================================================================

    def login_or_register(self, country_code: str, phone: str) -> dict:
        """Run steps 1-2: get methods + initiate OTP.

        After this call, wait for SMS and call verify_otp(otp).
        Returns the initiate_otp result.
        """
        methods_result = self.get_login_methods(country_code, phone)
        if methods_result["status"] != 200:
            return methods_result
        return self.initiate_otp(country_code, phone)

    def complete_login(self, otp: str) -> dict:
        """Run steps 3-5: verify OTP → account list → issue token.

        Returns the issue_token result. After success, self.auth.access_token is set.
        """
        verify_result = self.verify_otp(otp)
        if verify_result["status"] != 200:
            return verify_result

        acct_result = self.get_account_list()
        if acct_result["status"] != 200:
            return acct_result

        return self.issue_token()

    # ========================================================================
    # Phase 2: GoPay PIN Setup (HAR-verified 2026-05-15)
    #
    # Real flow:
    #   1. pins/allowed (check PIN validity)
    #   2. cvs/v1/methods (flow="goto_pin_wa_sms") → verification_id
    #   3. cvs/v1/initiate (flow="goto_pin_wa_sms", otp_sms) → otp_token
    #   4. cvs/v1/verify (flow="goto_pin_wa_sms") → JWE verification_token
    #   5. api/v2/users/pins/setup/tokens (pin + Verification-Token: JWE) → done
    # ========================================================================

    def pin_request_otp(self) -> dict:
        """CVS flow for PIN setup: methods → initiate → returns otp_token.

        Uses flow="goto_pin_wa_sms". Requires valid access_token.
        """
        self.auth.transaction_id = str(uuid.uuid4())

        methods_body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "country_code": None,
            "device_verification_token_id": None,
            "email_address": None,
            "flow": "goto_pin_wa_sms",
            "phone_number": None,
        }
        methods_result = self._sso_post("/cvs/v1/methods", methods_body)
        if methods_result["status"] not in (200, 201):
            return methods_result
        data = methods_result["body"].get("data", methods_result["body"])
        self.auth.verification_id = data.get("verification_id", "")
        self.auth.methods = data.get("methods", [])
        log.info("PIN CVS methods: %s, vid=%s", self.auth.methods, self.auth.verification_id)

        initiate_body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "country_code": None,
            "device_verification_token_id": None,
            "email_address": None,
            "flow": "goto_pin_wa_sms",
            "is_multiple_method": None,
            "phone_number": None,
            "verification_id": self.auth.verification_id,
            "verification_method": "otp_sms",
        }
        result = self._sso_cvs_initiate(initiate_body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.otp_token = inner.get("otp_token", "")
            self.auth.otp_length = inner.get("otp_length", 4)
            log.info("PIN OTP sent: otp_token=%s...", self.auth.otp_token[:20])
        return result

    def pin_verify_otp(self, otp: str) -> dict:
        """POST /cvs/v1/verify (flow=goto_pin_wa_sms) → JWE for PIN setup."""
        body = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "otp": otp,
                "otp_token": self.auth.otp_token,
            },
            "flow": "goto_pin_wa_sms",
            "verification_id": self.auth.verification_id,
            "verification_method": "otp_sms",
        }
        result = self._sso_post("/cvs/v1/verify", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.verification_token = inner.get("verification_token", "")
            log.info("PIN OTP verified, token=%s...", self.auth.verification_token[:40])
        return result

    def pin_setup(self, pin: str) -> dict:
        """POST /api/v2/users/pins/setup/tokens → set PIN.

        Latest GoPay 2.8.0 capture: uses Verification-Token (JWE from
        pin_verify_otp), and sends empty challenge_id/client_id.
        """
        body = {
            "challenge_id": "",
            "client_id": "",
            "pin": pin,
        }
        vtoken = self.auth.verification_token
        if not vtoken.startswith("Bearer "):
            vtoken = f"Bearer {vtoken}"
        extra = {
            "Verification-Token": vtoken,
            "is-token-required": "false",
        }
        result = self._gopay_post("/api/v2/users/pins/setup/tokens", body, extra)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
            log.info("PIN setup success")
        return result

    def setup_gopay_pin(self, pin: str, otp: str) -> dict:
        """Full PIN setup: check → CVS OTP verify → set PIN.

        Args:
            pin: 6-digit PIN
            otp: OTP received via SMS
        """
        allowed = self.pin_check_allowed(pin)
        if allowed["status"] not in (200, 201):
            return allowed

        verify = self.pin_verify_otp(otp)
        if verify["status"] not in (200, 201):
            return verify

        return self.pin_setup(pin)

    def get_user_profile(self) -> dict:
        """GET /v1/users/profile → check GoPay profile (is_pin_setup etc)."""
        return self._gopay_request("GET", "/v1/users/profile")

    def pin_post_registration_hook(self, payment_method: str = "GOPAY_WALLET") -> dict:
        """Step 9: POST /v1/customer/payment-options/post-registration-hook → activate GoPay."""
        body = {"payment_method": payment_method, "data": {}}
        return self._gopay_post("/v1/customer/payment-options/post-registration-hook", body)



    # ========================================================================
    # Phase 3: PIN Operations (post-activation)
    # ========================================================================

    def pin_create_challenge(self, flow: str = "SET_PIN") -> dict:
        """POST /api/v1/users/pin/challenges → create challenge for PIN verification.

        Returns challenge_id and client_id needed for pin_verify.
        """
        body = {"flow": flow}
        result = self._gopay_post("/api/v1/users/pin/challenges", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_challenge_id = inner.get("challenge_id", "")
            self.auth.pin_client_id = inner.get("client_id", "")
            log.info("PIN challenge: id=%s, client=%s", self.auth.pin_challenge_id, self.auth.pin_client_id)
        return result

    def pin_verify(self, pin: str, challenge_id: str = "", client_id: str = "") -> dict:
        """POST /api/v1/users/pin/tokens → verify PIN for transaction.

        Returns a pin_token used to authorize the transaction.
        """
        body = {
            "client_id": client_id or self.auth.pin_client_id,
            "pin": pin,
            "challenge_id": challenge_id or self.auth.pin_challenge_id,
        }
        result = self._gopay_post("/api/v1/users/pin/tokens", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
            log.info("PIN verified, pin_token=%s...", self.auth.pin_token[:30] if self.auth.pin_token else "(empty)")
        return result

    # ========================================================================
    # Phase 4: GoPay Envelope (Red Packet)
    # ========================================================================

    def envelope_get_details(self, link_id: str) -> dict:
        """GET /v1/festivals/envelope-requests/{link_id} → red packet details."""
        return self._gopay_request("GET", f"/v1/festivals/envelope-requests/{link_id}")

    def envelope_claim_by_link(self, link_id: str) -> dict:
        """POST /v1/festivals/link → claim red packet that has link_id.

        For envelopes whose deeplink contains link_id (older format).
        Body: {"link_id": "<link_id>"}
        Response 422 GoPay-36006 = expired/claimed.
        """
        return self._gopay_post("/v1/festivals/link", {"link_id": link_id})

    def envelope_claim(self, deeplink_id: str) -> dict:
        """Claim a red packet (envelope) - full flow.

        Captured via VM memory scan (2026-05-16):
          Step 1: GET /v1/festivals/envelope-requests/{deeplink_id}
                  → returns envelope details + generated envelope_request_id
          Step 2: POST /v1/festivals/envelope-requests
                  Body: {"envelope_request_id": "<from_step1>"}
                  → {"data":{"envelope_request_id":"..."},"success":true}

        No PIN required, no consent required.
        """
        import time
        r1 = self._gopay_get(f"/v1/festivals/envelope-requests/{deeplink_id}")
        if r1["status"] != 200:
            return r1
        eid = r1["body"]["data"]["envelope_request_id"]
        time.sleep(1)
        return self._gopay_post("/v1/festivals/envelope-requests", {"envelope_request_id": eid})

    def pin_validate(self, pin: str) -> dict:
        """POST /v1/users/pin/validate → legacy PIN validation."""
        return self._gopay_post("/v1/users/pin/validate", {"pin": pin})

    def pin_reset_v3(self, new_pin: str, otp: str) -> dict:
        """POST /api/v3/users/pins/reset/tokens → reset forgotten PIN."""
        body = {
            "client_id": None,
            "pin": new_pin,
            "challenge_id": self.auth.pin_challenge_id,
            "otp_token": self.auth.pin_otp_auth_token,
            "otp": otp,
        }
        result = self._gopay_post("/api/v3/users/pins/reset/tokens", body)
        if result["status"] in (200, 201):
            inner = result["body"].get("data", result["body"])
            self.auth.pin_token = inner.get("token", "")
        return result

    def pin_update_v3(self, new_pin: str, pin_token: str = "") -> dict:
        """PUT /v3/users/pin/update → change PIN (knows old PIN)."""
        body = {
            "new_pin": new_pin,
            "pin_token": pin_token or self.auth.pin_token,
        }
        return self._gopay_put("/v3/users/pin/update", body)

    def pin_check_allowed(self, pin: str = "", flow: str = "SET_PIN") -> dict:
        """POST /api/v1/users/pins/allowed with the latest capture-backed request shape.

        New-account PIN setup sends the candidate PIN. Older call sites that
        only pass a flow remain supported.
        """
        body = {"pin": pin} if re.fullmatch(r"\d{6}", str(pin or "")) else {"flow": flow}
        return self._gopay_post("/api/v1/users/pins/allowed", body)

    # ========================================================================
    # Phase 4: Wallet Operations
    # ========================================================================

    def get_profile(self) -> dict:
        """GET /v1/users/profile → user profile."""
        return self._gopay_get("/v1/users/profile")

    def get_balance(self) -> dict:
        """GET /v1/payment-options/balances → wallet balance."""
        return self._gopay_get("/v1/payment-options/balances")

    def get_payment_profiles(self) -> dict:
        """GET /v2/payment-options/profiles → payment method profiles."""
        return self._gopay_get("/v2/payment-options/profiles")

    def get_linked_apps(self) -> dict:
        """GET /v1/linkedapps → auto-debit mandates."""
        return self._gopay_get("/v1/linkedapps")

    def unlink_app(self, link_id: str) -> dict:
        """PATCH /v1/links?link_id=<id> → cancel auto-debit mandate."""
        return self._gopay_patch(f"/v1/links?link_id={link_id}", {})

    def get_payment_options(self) -> dict:
        """GET /v1/customer/payment-options/settings/list → payment settings."""
        return self._gopay_get("/v1/customer/payment-options/settings/list")

    def refresh_payment_options(self) -> dict:
        """PUT /v1/customer/payment-options/refresh → refresh payment options."""
        return self._gopay_put("/v1/customer/payment-options/refresh", {})

    def get_ewallet_consent(self) -> dict:
        """GET /v1/customers/consents/e-wallet → consent status."""
        return self._gopay_get("/v1/customers/consents/e-wallet")

    def set_ewallet_consent(self, consent: bool = True) -> dict:
        """PUT /v1/customers/consents/e-wallet → set consent."""
        return self._gopay_put("/v1/customers/consents/e-wallet", {"consent": consent})


# ============================================================================
# CLI
# ============================================================================
if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Gojek/GoPay protocol client")
    sub = parser.add_subparsers(dest="command")

    # --- login ---
    p_login = sub.add_parser("login", help="Start login/register flow")
    p_login.add_argument("--country-code", default="+62")
    p_login.add_argument("--phone", required=True)
    p_login.add_argument("--appinfo", default=r"C:\tools\gojek_capture\fresh_appinfo.txt")
    p_login.add_argument("--headers", default=r"C:\tools\gojek_capture\fresh_headers.txt")

    # --- verify ---
    p_verify = sub.add_parser("verify", help="Verify OTP and complete login")
    p_verify.add_argument("--otp", required=True)

    # --- pin ---
    p_pin = sub.add_parser("pin", help="Setup GoPay PIN")
    p_pin.add_argument("--pin", required=True)
    p_pin.add_argument("--otp", required=True, help="SMS OTP for PIN setup")

    # --- profile ---
    p_profile = sub.add_parser("profile", help="Get user profile")
    p_profile.add_argument("--appinfo", default=r"C:\tools\gojek_capture\fresh_appinfo.txt")
    p_profile.add_argument("--headers", default=r"C:\tools\gojek_capture\fresh_headers.txt")

    # --- balance ---
    p_balance = sub.add_parser("balance", help="Get wallet balance")
    p_balance.add_argument("--appinfo", default=r"C:\tools\gojek_capture\fresh_appinfo.txt")
    p_balance.add_argument("--headers", default=r"C:\tools\gojek_capture\fresh_headers.txt")

    args = parser.parse_args()

    if args.command == "login":
        client = GojekClient.from_device_info(args.appinfo, args.headers)
        result = client.login_or_register(args.country_code, args.phone)
        print(json.dumps(result, indent=2))
        print(f"\nOTP sent via {client.auth.otp_channel}. Length: {client.auth.otp_length}")
        print("Next: run 'verify --otp <code>' to complete login")

    elif args.command == "profile":
        client = GojekClient.from_device_info(args.appinfo, args.headers)
        result = client.get_profile()
        print(json.dumps(result, indent=2))

    elif args.command == "balance":
        client = GojekClient.from_device_info(args.appinfo, args.headers)
        result = client.get_balance()
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()
