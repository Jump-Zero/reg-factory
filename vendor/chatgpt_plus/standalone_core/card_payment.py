"""Protocol card-payment flow for the account-opening pipeline.

The implementation mirrors the documented sequence while keeping card data,
tokens and customer identifiers in process memory only.  Callers receive a
small result summary; logs contain stage/status information and masked values.
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional dependency fallback
    curl_requests = None

try:
    import requests as plain_requests
except Exception:  # pragma: no cover - optional dependency fallback
    plain_requests = None


APP_BASE = "https://chatgpt.com"
STRIPE_BASE = "https://api.stripe.com"
CHECKOUT_URL = f"{APP_BASE}/backend-api/payments/checkout"
STRIPE_VERSION = "2025-03-31.basil"
STRIPE_BETAS = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_TIMEOUT = 60
STRIPE_HCAPTCHA_SITE_KEY = "463b917e-e264-403f-ad34-34af0ee10294"
STRIPE_HCAPTCHA_URL = (
    "https://b.stripecdn.com/stripethirdparty-srv/assets/"
    "v33.5/HCaptchaInvisible.html"
)
KNOWN_SETUP_PUBLISHABLE_KEYS = {
    "KslHRdbaPg": "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "C6h1nxGoI3": "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
}


class CardPaymentError(RuntimeError):
    """A stage-labelled payment error with safe diagnostic text."""

    def __init__(
        self,
        message: str,
        *,
        proxy_retry_safe: bool = True,
        action_required: str = "",
        checkout_id: str = "",
        processor_entity: str = "",
        checkout_link: str = "",
    ) -> None:
        super().__init__(message)
        # A proxy fallback reruns the complete bind/payment flow.  Once an
        # irreversible remote mutation has been submitted, replaying the flow
        # on another proxy can duplicate a bind or a payment attempt.
        self.proxy_retry_safe = bool(proxy_retry_safe)
        self.action_required = _text(action_required)
        self.checkout_id = _text(checkout_id)
        self.processor_entity = _text(processor_entity)
        self.checkout_link = _text(checkout_link)


@dataclass(slots=True)
class CardPaymentConfig:
    token: str
    account_id: str = ""
    country: str = "US"
    currency: str = "USD"
    promo_campaign: str = "plus-1-month-free"
    billing: dict[str, str] = field(default_factory=dict)
    card: dict[str, str] = field(default_factory=dict)
    session_token: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    device_id: str = ""
    fingerprint_profile: dict[str, str] = field(default_factory=dict)
    timeout: int = DEFAULT_TIMEOUT
    max_setup_confirm_attempts: int = 1
    hcaptcha_token: str = ""
    checkout_id: str = ""
    payment_page_id: str = ""
    processor_entity: str = ""
    publishable_key: str = ""
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    captcha_provider: str = ""
    captcha_key: str = ""
    captcha_api_url: str = ""
    hcaptcha_site_key: str = STRIPE_HCAPTCHA_SITE_KEY
    hcaptcha_website_url: str = STRIPE_HCAPTCHA_URL
    bind_country: str = "US"
    bind_currency: str = "USD"
    strong_bind_direct: bool = True
    stop_after_bind: bool = False
    flow_mode: str = "full"
    payment_method_id: str = ""
    payment_method_publishable_key: str = ""
    card_last4: str = ""
    fast_verify: bool = False
    payment_method_poll_delays: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 4.0)
    subscription_poll_delays: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0)
    sentinel_token: str = ""
    telemetry: str = ""
    client_build_number: str = ""
    client_version: str = ""
    oai_session_id: str = ""
    web_deployment_attestation: str = ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _mask(value: Any, head: int = 8, tail: int = 4) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if len(raw) <= head + tail:
        return "***"
    return f"{raw[:head]}…{raw[-tail:]}"


def _safe_error(value: Any) -> str:
    raw = re.sub(r"\s+", " ", _text(value))
    raw = re.sub(
        r"(?i)(client_secret|confirmation_token|access_token|authorization|"
        r"card\]?\[?(?:number|cvc|cvv)|password)([=:])[^&\s,}]+",
        r"\1\2<REDACTED>",
        raw,
    )
    return raw[:500]


def _log(logger: Callable[[str], None], message: str) -> None:
    try:
        logger(_safe_error(message))
    except Exception:
        pass


def _check_cancel(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled and is_cancelled():
        raise CardPaymentError("payment cancelled")


def _response_json(response: Any) -> dict[str, Any]:
    try:
        value = response.json() or {}
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _response_error(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    payload = _response_json(response)
    if payload:
        error = payload.get("error")
        if isinstance(error, dict):
            code = _text(error.get("code"))
            message = _text(error.get("message"))
            return f"HTTP {status} {code} {message}".strip()
        return f"HTTP {status} {_text(payload.get('message') or payload.get('detail'))}".strip()
    return f"HTTP {status}".strip()


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _find_key(payload: Any, names: tuple[str, ...]) -> str:
    wanted = {item.lower() for item in names}
    for item in _walk(payload):
        for key, value in item.items():
            if key.lower() in wanted and isinstance(value, (str, int, float)):
                text = _text(value)
                if text:
                    return text
    return ""


def _find_list(payload: Any, names: tuple[str, ...]) -> list[Any]:
    wanted = {item.lower() for item in names}
    for item in _walk(payload):
        for key, value in item.items():
            if key.lower() in wanted and isinstance(value, list):
                return value
    return []


def _find_identifier(payload: Any, prefixes: tuple[str, ...]) -> str:
    for item in _walk(payload):
        for value in item.values():
            if not isinstance(value, (str, int, float)):
                continue
            text = _text(value)
            if any(text.startswith(prefix) for prefix in prefixes):
                return text
    return ""


def _find_identifiers(payload: Any, prefixes: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for item in _walk(payload):
        for value in item.values():
            if not isinstance(value, (str, int, float)):
                continue
            text = _text(value)
            if any(text.startswith(prefix) for prefix in prefixes) and text not in found:
                found.append(text)
    return found


def _find_client_secret(payload: Any) -> str:
    value = _find_key(payload, ("client_secret", "clientSecret"))
    if value:
        return value
    for item in _walk(payload):
        for value in item.values():
            text = _text(value)
            if "_secret_" in text and text.startswith(("seti_", "pi_", "cs_")):
                return text
    return ""


def _setup_intent_id(payload: Any, client_secret: str = "") -> str:
    """Prefer the SetupIntent id over the similarly-prefixed client secret."""
    direct = _find_key(payload, ("setup_intent_id", "setupIntentId", "id"))
    if direct.startswith("seti_") and "_secret_" not in direct:
        return direct
    secret_base = _text(client_secret).split("_secret_", 1)[0]
    if secret_base.startswith("seti_"):
        return secret_base
    candidate = _find_identifier(payload, ("seti_",))
    if "_secret_" in candidate:
        candidate = candidate.split("_secret_", 1)[0]
    return candidate if candidate.startswith("seti_") else ""


def _require_intent_succeeded(payload: Any, stage: str) -> str:
    """Reject HTTP-200 Stripe responses that still need action or failed."""
    status = _text(_find_key(payload, ("status",))).lower()
    if status == "succeeded":
        return status
    if status == "requires_action":
        raise CardPaymentError(f"{stage}: requires_action (3DS verification required)")
    if status:
        raise CardPaymentError(f"{stage}: unexpected intent status {status}")
    raise CardPaymentError(f"{stage}: missing intent status")


def _payment_intent_id(payload: Any, client_secret: str = "") -> str:
    direct = _find_key(payload, ("payment_intent_id", "paymentIntentId", "id"))
    if direct.startswith("pi_") and "_secret_" not in direct:
        return direct
    secret_base = _text(client_secret).split("_secret_", 1)[0]
    if secret_base.startswith("pi_"):
        return secret_base
    candidate = _find_identifier(payload, ("pi_",))
    if "_secret_" in candidate:
        candidate = candidate.split("_secret_", 1)[0]
    return candidate if candidate.startswith("pi_") else ""


def _publishable_key_for_setup(client_secret: str, fallback: str = "") -> str:
    secret = _text(client_secret)
    for fragment, key in KNOWN_SETUP_PUBLISHABLE_KEYS.items():
        if fragment in secret:
            return key
    return _text(fallback)


def _checkout_id(payload: Any) -> str:
    return _find_key(
        payload,
        ("checkout_session_id", "checkoutSessionId", "session_id", "sessionId"),
    ) or _find_identifier(payload, ("oaics_", "cs_live_", "cs_test_", "cs_"))


def _supported_checkout_id(value: Any) -> bool:
    return _text(value).startswith(("oaics_", "cs_live_", "cs_test_", "cs_"))


def _processor_entity(payload: Any, country: str) -> str:
    return _find_key(payload, ("processor_entity", "processorEntity")) or (
        "openai_llc" if country.upper() in {"US", "AU"} else "openai_ie"
    )


def _card_fields(card: dict[str, Any]) -> dict[str, str]:
    number = re.sub(r"\D", "", _text(card.get("card_number") or card.get("number")))
    cvc = re.sub(r"\D", "", _text(card.get("cvv") or card.get("cvc")))
    month = re.sub(r"\D", "", _text(card.get("exp_month") or card.get("month"))).zfill(2)
    year = re.sub(r"\D", "", _text(card.get("exp_year") or card.get("year")))
    if len(year) == 2:
        year = f"20{year}"
    if not number or not cvc or len(month) != 2 or len(year) != 4:
        raise CardPaymentError("invalid card fields")
    if len(number) < 12 or len(number) > 19:
        raise CardPaymentError("invalid card number length")
    if len(cvc) not in {3, 4}:
        raise CardPaymentError("invalid card security code length")
    return {"number": number, "cvc": cvc, "exp_month": month, "exp_year": year}


def _billing_fields(config: CardPaymentConfig) -> dict[str, str]:
    source = {str(k): _text(v) for k, v in (config.billing or {}).items()}
    return {
        "name": source.get("name") or "",
        "email": source.get("email"),
        "line1": source.get("line1") or source.get("address") or "",
        "line2": source.get("line2"),
        "city": source.get("city"),
        "state": source.get("state"),
        "postal_code": source.get("postal_code") or source.get("zip") or "",
        "country": source.get("country") or "",
        "phone": source.get("phone"),
    }


def _payment_card_last4(config: CardPaymentConfig) -> str:
    external_last4 = re.sub(r"\D", "", _text(config.card_last4))[-4:]
    if len(external_last4) == 4:
        return external_last4
    if config.card:
        return _card_fields(config.card)["number"][-4:]
    return ""


def _new_session(config: CardPaymentConfig, proxy: str = "") -> Any:
    fingerprint = dict(config.fingerprint_profile or {})
    impersonate = _text(fingerprint.get("tls_impersonate")) or "chrome146"
    if curl_requests is not None:
        session = curl_requests.Session(impersonate=impersonate)
    elif plain_requests is not None:
        session = plain_requests.Session()
    else:
        raise CardPaymentError("missing HTTP dependency: install curl_cffi")
    if hasattr(session, "trust_env"):
        session.trust_env = False
    device_id = _text(config.device_id) or str(uuid.uuid4())
    user_agent = _text(fingerprint.get("ua")) or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )
    major = _text(fingerprint.get("major")) or "146"
    platform = _text(fingerprint.get("platform")) or "Windows"
    sec_ch_ua = _text(fingerprint.get("sec_ch_ua")) or (
        f'"Not;A=Brand";v="8", "Chromium";v="{major}", '
        f'"Google Chrome";v="{major}"'
    )
    accept_language = _text(fingerprint.get("accept_language")) or (
        _text(config.locale) or "en-US"
    )
    oai_language = _text(fingerprint.get("oai_language")) or (
        _text(config.locale) or "en-US"
    )
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": accept_language,
        "Authorization": f"Bearer {_text(config.token)}",
        "Origin": APP_BASE,
        "Referer": f"{APP_BASE}/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": oai_language,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": _text(fingerprint.get("mobile")) or "?0",
        "sec-ch-ua-platform": f'"{platform.strip(chr(34))}"',
        "priority": "u=1, i",
    })
    if _text(config.account_id):
        session.headers["chatgpt-account-id"] = _text(config.account_id)
    if fingerprint:
        session.headers.update({
            "sec-ch-ua-arch": f'"{_text(fingerprint.get("arch")) or "x86"}"',
            "sec-ch-ua-bitness": f'"{_text(fingerprint.get("bitness")) or "64"}"',
            "sec-ch-ua-model": '""',
            "sec-ch-ua-full-version": f'"{_text(fingerprint.get("full")) or major}"',
            "sec-ch-ua-full-version-list": _text(
                fingerprint.get("sec_ch_ua_full_version_list")
            ) or sec_ch_ua,
            "sec-ch-ua-platform-version": (
                f'"{_text(fingerprint.get("platform_version")) or "15.0.0"}"'
            ),
        })
    if _text(proxy):
        session.proxies = {"http": proxy, "https": proxy}
    if hasattr(session, "cookies"):
        try:
            session.cookies.set(
                "oai-did",
                device_id,
                domain=".chatgpt.com",
                path="/",
            )
        except Exception:
            pass
    if _text(config.session_token) and hasattr(session, "cookies"):
        try:
            session.cookies.set(
                "__Secure-next-auth.session-token",
                _text(config.session_token),
                domain=".chatgpt.com",
                path="/",
            )
        except Exception:
            pass
    for cookie in config.cookies or []:
        if not isinstance(cookie, dict) or not _text(cookie.get("name")):
            continue
        try:
            session.cookies.set(
                _text(cookie.get("name")),
                _text(cookie.get("value")),
                domain=_text(cookie.get("domain")) or ".chatgpt.com",
                path=_text(cookie.get("path")) or "/",
            )
        except Exception:
            pass
    return session


def _hydrate_payment_metadata(
    session: Any,
    config: CardPaymentConfig,
    logger: Callable[[str], None],
) -> dict[str, str]:
    """Load the current web build/session attestation used by /payments/* APIs."""
    metadata = {
        "client_build_number": _text(config.client_build_number),
        "client_version": _text(config.client_version),
        "session_id": _text(config.oai_session_id),
        "attestation": _text(config.web_deployment_attestation),
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
                timeout=min(config.timeout, 30),
            )
            if int(getattr(response, "status_code", 0) or 0) < 400:
                html = _text(getattr(response, "text", ""))
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
        except Exception as exc:  # bootstrap metadata is additive
            _log(logger, f"payment bootstrap metadata skipped: {exc}")

    metadata["session_id"] = metadata["session_id"] or str(uuid.uuid4())
    app_headers = {
        "OAI-Client-Build-Number": metadata["client_build_number"],
        "OAI-Client-Version": metadata["client_version"],
        "OAI-Session-Id": metadata["session_id"],
    }
    if hasattr(session, "headers"):
        session.headers.update(
            {key: value for key, value in app_headers.items() if value}
        )
    # The web client only attaches deployment attestation to /payments/* calls.
    # Keep it outside Session defaults so it is never inherited by Stripe or
    # unrelated ChatGPT endpoints.
    try:
        setattr(session, "_oai_payment_attestation", metadata["attestation"])
    except Exception:
        pass
    config.client_build_number = metadata["client_build_number"]
    config.client_version = metadata["client_version"]
    config.oai_session_id = metadata["session_id"]
    config.web_deployment_attestation = metadata["attestation"]
    return metadata


def _stripe_client_bootstrap(
    session: Any,
    config: CardPaymentConfig,
) -> str:
    """Resolve the account's Stripe merchant key from the endpoint used by the UI.

    The settings/payment bundle does not derive this key from a Checkout object.
    It calls ``/payments/stripe_client_bootstrap`` for the current account before
    mounting Elements.  Keeping the same source prevents a PaymentMethod created
    under one Stripe account from being submitted to another account's
    SetupIntent.
    """
    account_id = _text(config.account_id)
    if not account_id:
        raise CardPaymentError("stripe client bootstrap: missing account id")
    response = session.get(
        f"{APP_BASE}/backend-api/payments/stripe_client_bootstrap",
        params={"account_id": account_id},
        headers=_app_headers(
            f"{APP_BASE}/",
            "/backend-api/payments/stripe_client_bootstrap",
            payment_attestation=_payment_attestation(session),
            session=session,
            json_content=False,
        ),
        timeout=config.timeout,
    )
    _capture_oai_update(session, response)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise CardPaymentError(
            f"stripe client bootstrap: {_response_error(response)}"
        )
    payload = _response_json(response)
    publishable_key = _find_key(payload, ("publishable_key", "publishableKey"))
    if not publishable_key.startswith(("pk_live_", "pk_test_")):
        raise CardPaymentError(
            "stripe client bootstrap: missing publishable key"
        )
    return publishable_key


def fetch_stripe_publishable_key(
    config: CardPaymentConfig,
    *,
    proxy: str = "",
    logger: Callable[[str], None] | None = None,
    session_factory: Callable[[CardPaymentConfig, str], Any] | None = None,
) -> str:
    """Fetch the per-account Stripe key needed before mounting Card Elements."""
    log = logger or (lambda _message: None)
    session = session_factory(config, proxy) if session_factory else _new_session(config, proxy)
    try:
        _hydrate_payment_metadata(session, config, log)
        publishable_key = _stripe_client_bootstrap(session, config)
        log(f"Stripe account bootstrap ready: {_mask(publishable_key)}")
        return publishable_key
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def _payment_attestation(session: Any) -> str:
    return _text(getattr(session, "_oai_payment_attestation", ""))


def _response_header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    target = name.lower()
    try:
        for key, value in headers.items():
            if _text(key).lower() == target:
                return _text(value)
    except Exception:
        return ""
    return ""


def _capture_oai_update(session: Any, response: Any) -> None:
    """Track the web client's opaque read-after-write consistency tokens."""

    updates = list(getattr(session, "_oai_pending_updates", []) or [])
    ack = _response_header(response, "x-oai-is-pending-updates-ack")
    if ack and updates:
        try:
            version, bitmap = ack.split(":", 1)
            if version == "3":
                acknowledged = int(bitmap, 16)
                updates = [
                    value
                    for index, value in enumerate(updates)
                    if not ((acknowledged >> index) & 1)
                ]
        except (TypeError, ValueError):
            pass

    update = _response_header(response, "x-oai-is-update")
    if update and update not in updates:
        updates.append(update)
    try:
        setattr(session, "_oai_pending_updates", updates[-32:])
    except Exception:
        pass


def _adopt_auth_session_token(session: Any, response: Any) -> bool:
    """Adopt the refreshed bearer returned by /api/auth/session.

    The browser auth-session client replaces its in-memory access token after a
    workspace update or token refresh.  Keeping the stale bearer here makes the
    immediately following subscription verification fail with token_expired.
    The token remains in the Session only and is never logged or returned.
    """

    if int(getattr(response, "status_code", 0) or 0) != 200:
        return False
    payload = _response_json(response)
    token = _text(payload.get("accessToken") or payload.get("access_token"))
    headers = getattr(session, "headers", None)
    if not token or headers is None:
        return False
    try:
        headers["Authorization"] = f"Bearer {token}"
    except Exception:
        return False
    return True


def _oai_pending_headers(session: Any) -> dict[str, str]:
    updates = [
        _text(value)
        for value in (getattr(session, "_oai_pending_updates", []) or [])
        if _text(value)
    ]
    if not updates:
        return {}
    return {
        "x-oai-is-pending-updates": json.dumps(
            {"v": 3, "updates": updates},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }


def _app_headers(
    referer: str = "",
    route: str = "",
    *,
    payment_attestation: str = "",
    session: Any | None = None,
    json_content: bool = True,
) -> dict[str, Any]:
    headers: dict[str, Any] = {
        "Origin": APP_BASE,
        "Referer": referer or f"{APP_BASE}/",
        # Session defaults contain application/json.  Explicitly remove it for
        # browser-style GET/navigation requests so the wire shape matches HAR.
        "Content-Type": "application/json" if json_content else None,
    }
    if route:
        headers["x-openai-target-path"] = route
        headers["x-openai-target-route"] = route
    if route.startswith("/backend-api/payments/") and _text(payment_attestation):
        headers["OAI-Web-Deployment-Attestation"] = _text(payment_attestation)
    if session is not None:
        headers.update(_oai_pending_headers(session))
    return headers


def _stripe_headers(
    publishable_key: str,
    referer: str,
    *,
    stripe_version: str = "",
) -> dict[str, Any]:
    headers: dict[str, Any] = {
        # Stripe.js sends the publishable key in the request body/query (`key`),
        # not as Bearer authentication.  A Bearer pk_ header selects the normal
        # server API auth path and can produce `secret_key_required` for client
        # operations such as PaymentMethod hydration.
        "Authorization": None,
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        # Remove application-session headers inherited from Session.defaults
        # when crossing from chatgpt.com to api.stripe.com.
        "oai-device-id": None,
        "oai-language": None,
        "chatgpt-account-id": None,
        "OAI-Client-Build-Number": None,
        "OAI-Client-Version": None,
        "OAI-Session-Id": None,
        "OAI-Web-Deployment-Attestation": None,
    }
    if _text(stripe_version):
        headers["Stripe-Version"] = _text(stripe_version)
    return headers


def _post_json(
    session: Any,
    url: str,
    body: dict[str, Any],
    *,
    timeout: int,
    stage: str,
    referer: str = "",
    route: str = "",
    additional_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = _app_headers(
        referer,
        route,
        payment_attestation=_payment_attestation(session),
        session=session,
    )
    if additional_headers:
        headers.update(
            {
                str(key): _text(value)
                for key, value in additional_headers.items()
                if _text(value)
            }
        )
    response = session.post(
        url,
        json=body,
        headers=headers,
        timeout=timeout,
    )
    _capture_oai_update(session, response)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise CardPaymentError(f"{stage}: {_response_error(response)}")
    return _response_json(response)


def _post_form(
    session: Any,
    url: str,
    body: dict[str, Any],
    *,
    key: str,
    referer: str,
    timeout: int,
    stage: str,
    stripe_version: str = "",
) -> tuple[Any, dict[str, Any]]:
    response = session.post(
        url,
        data=urlencode(body, doseq=True),
        headers=_stripe_headers(key, referer, stripe_version=stripe_version),
        timeout=timeout,
    )
    return response, _response_json(response)


def _checkout_create(
    session: Any,
    config: CardPaymentConfig,
    *,
    country: str = "",
    currency: str = "",
    promo_campaign: str | None = None,
) -> dict[str, Any]:
    checkout_country = (_text(country) or config.country).upper()
    checkout_currency = (_text(currency) or config.currency).upper()
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": checkout_country,
            "currency": checkout_currency,
        },
        "checkout_ui_mode": "custom",
    }
    campaign = config.promo_campaign if promo_campaign is None else promo_campaign
    if _text(campaign):
        body["promo_campaign"] = {
            "promo_campaign_id": _text(campaign),
            "is_coupon_from_query_param": False,
        }
    return _post_json(
        session,
        CHECKOUT_URL,
        body,
        timeout=config.timeout,
        stage="checkout create",
        route="/backend-api/payments/checkout",
    )


def _init_payment_page(session: Any, config: CardPaymentConfig, payment_page_id: str, publishable_key: str, processor_entity: str) -> tuple[dict[str, Any], str]:
    stripe_js_id = str(uuid.uuid4())
    page = f"{APP_BASE}/checkout/{processor_entity}/{config.checkout_id or payment_page_id}"
    locale = _text(config.locale) or "zh-CN"
    body = {
        "browser_locale": locale,
        "browser_timezone": _text(config.timezone) or "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": publishable_key,
        "_stripe_version": STRIPE_BETAS,
    }
    response, payload = _post_form(
        session,
        f"{STRIPE_BASE}/v1/payment_pages/{payment_page_id}/init",
        body,
        key=publishable_key,
        referer=page,
        timeout=config.timeout,
        stage="payment page init",
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise CardPaymentError(f"payment page init: {_response_error(response)}")
    return payload, stripe_js_id


def _create_elements_session(
    session: Any,
    config: CardPaymentConfig,
    init_payload: dict[str, Any],
    payment_page_id: str,
    publishable_key: str,
    stripe_js_id: str,
) -> dict[str, Any]:
    options = (
        init_payload.get("elements_options")
        if isinstance(init_payload.get("elements_options"), dict)
        else {}
    )
    amount = options.get("amount")
    currency = _text(options.get("currency") or init_payload.get("currency"))
    payment_config = _text(options.get("payment_method_configuration"))
    method_types = options.get("payment_method_types")
    if not isinstance(method_types, list) or not method_types:
        method_types = ["card", "paypal"]
    params: dict[str, Any] = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount if amount is not None else "2000"),
        "deferred_intent[currency]": currency or _text(config.bind_currency).lower() or "usd",
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": currency or _text(config.bind_currency).lower() or "usd",
        "key": publishable_key,
        "_stripe_version": STRIPE_BETAS,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": (_text(config.locale).split("-", 1)[0] or "zh"),
        "type": "deferred_intent",
        "checkout_session_id": payment_page_id,
    }
    for index, method_type in enumerate(method_types[:4]):
        params[f"deferred_intent[payment_method_types][{index}]"] = _text(
            method_type
        )
    if payment_config:
        params[
            "deferred_intent[payment_method_configuration][id]"
        ] = payment_config
    response = session.get(
        f"{STRIPE_BASE}/v1/elements/sessions",
        params=params,
        headers=_stripe_headers(publishable_key, "https://js.stripe.com/"),
        timeout=config.timeout,
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise CardPaymentError(f"elements session: {_response_error(response)}")
    payload = _response_json(response)
    if not payload:
        raise CardPaymentError("elements session: empty response")
    return payload


def _create_payment_elements_session(
    session: Any,
    config: CardPaymentConfig,
    checkout_payload: dict[str, Any],
    publishable_key: str,
    stripe_js_id: str,
    *,
    amount: str = "0",
) -> dict[str, Any]:
    """Rebuild the zero-amount custom-checkout Elements context from HAR."""
    method_types = [
        _text(item)
        for item in _find_list(checkout_payload, ("payment_method_types",))
        if _text(item)
    ] or ["link", "card"]
    customer_session_secret = _find_key(
        checkout_payload,
        ("customer_session_client_secret", "customerSessionClientSecret"),
    )
    payment_config = _find_key(
        checkout_payload,
        ("payment_method_configuration", "payment_method_configuration_id"),
    )
    custom_methods: list[str] = []
    for item in _find_list(
        checkout_payload, ("custom_payment_methods", "customPaymentMethods")
    ):
        value = _find_key(item, ("id",)) if isinstance(item, dict) else _text(item)
        if value.startswith("cpmt_") and value not in custom_methods:
            custom_methods.append(value)

    currency = (_text(config.currency) or "USD").lower()
    params: dict[str, Any] = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": _text(amount) or "0",
        "deferred_intent[currency]": currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": currency,
        "key": publishable_key,
        "_stripe_version": STRIPE_BETAS,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": _text(config.locale) or "en-US",
        "type": "deferred_intent",
    }
    for index, method_type in enumerate(method_types[:4]):
        params[f"deferred_intent[payment_method_types][{index}]"] = method_type
    if customer_session_secret:
        params["customer_session_client_secret"] = customer_session_secret
    if payment_config:
        params["deferred_intent[payment_method_configuration][id]"] = payment_config
    for index, method_id in enumerate(custom_methods[:4]):
        params[f"custom_payment_methods[{index}]"] = method_id

    response = session.get(
        f"{STRIPE_BASE}/v1/elements/sessions",
        params=params,
        headers=_stripe_headers(publishable_key, "https://js.stripe.com/"),
        timeout=config.timeout,
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise CardPaymentError(f"payment elements session: {_response_error(response)}")
    payload = _response_json(response)
    if not payload:
        raise CardPaymentError("payment elements session: empty response")
    return payload


def _setup_confirm(
    session: Any,
    config: CardPaymentConfig,
    init_payload: dict[str, Any],
    checkout_id: str,
    processor: str,
    publishable_key: str,
    setup_id: str,
    client_secret: str,
    stripe_js_id: str,
    *,
    attempt: int,
) -> tuple[Any, dict[str, Any]]:
    external_payment_method = _text(config.payment_method_id)
    billing = _billing_fields(config)
    elements_session_id = _find_identifier(init_payload, ("elements_session_",))
    wallet_config_id = _find_key(init_payload, ("wallet_config_id",))
    guid = f"{uuid.uuid4()}{uuid.uuid4().hex[:6]}"
    muid = f"{uuid.uuid4()}{uuid.uuid4().hex[:6]}"
    sid = f"{uuid.uuid4()}{uuid.uuid4().hex[:6]}"
    body: dict[str, Any] = {
        "set_as_default_payment_method": "true",
        "expected_payment_method_type": "card",
        "use_stripe_sdk": "true",
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "card-element",
        "client_attribution_metadata[merchant_integration_version]": "2017",
        "client_secret": client_secret,
    }
    if external_payment_method:
        body["payment_method"] = external_payment_method
    else:
        card = _card_fields(config.card)
        body.update({
            "payment_method_data[type]": "card",
            "payment_method_data[billing_details][name]": billing["name"],
            "payment_method_data[allow_redisplay]": "always",
            "payment_method_data[card][number]": card["number"],
            "payment_method_data[card][cvc]": card["cvc"],
            "payment_method_data[card][exp_month]": card["exp_month"],
            "payment_method_data[card][exp_year]": card["exp_year"],
            "payment_method_data[guid]": guid,
            "payment_method_data[muid]": muid,
            "payment_method_data[sid]": sid,
            "payment_method_data[pasted_fields]": "number,exp,cvc",
            "payment_method_data[payment_user_agent]": "stripe.js/3704557c13; stripe-js-v3/3704557c13; card-element",
            "payment_method_data[referrer]": APP_BASE,
            "payment_method_data[time_on_page]": str(random.randint(300_000, 750_000)),
            "payment_method_data[client_attribution_metadata][client_session_id]": stripe_js_id,
            "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
            "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "card-element",
            "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2017",
        })
        if billing["email"]:
            body["payment_method_data[billing_details][email]"] = billing["email"]
        for field in ("line1", "line2", "city", "state", "postal_code", "country"):
            value = billing.get(field, "")
            if value:
                body[f"payment_method_data[billing_details][address][{field}]"] = (
                    value.upper() if field == "country" else value
                )
        if billing["phone"]:
            body["payment_method_data[billing_details][phone]"] = billing["phone"]
        if billing["postal_code"]:
            body["payment_method_data[pasted_fields]"] = "number,exp,cvc,zip"
    if _text(config.hcaptcha_token):
        body["radar_options[hcaptcha_token]"] = _text(config.hcaptcha_token)
    if elements_session_id and not external_payment_method:
        body["payment_method_data[client_attribution_metadata][elements_session_id]"] = elements_session_id
    if wallet_config_id and not external_payment_method:
        body["payment_method_data[client_attribution_metadata][wallet_config_id]"] = wallet_config_id
        body["client_attribution_metadata[wallet_config_id]"] = wallet_config_id
    response, payload = _post_form(
        session,
        f"{STRIPE_BASE}/v1/setup_intents/{setup_id}/confirm",
        body,
        key=publishable_key,
        referer=f"{APP_BASE}/checkout/{processor}/{checkout_id}",
        timeout=config.timeout,
        stage=f"setup confirm attempt {attempt}",
    )
    return response, payload


def _prepare_hcaptcha_token(
    config: CardPaymentConfig,
    *,
    proxy: str,
    logger: Callable[[str], None],
) -> str:
    supplied = _text(config.hcaptcha_token)
    if supplied:
        return supplied
    provider = _text(config.captcha_provider).lower()
    api_key = _text(config.captcha_key)
    api_url = _text(config.captcha_api_url)
    if not provider or not api_key:
        return ""
    if provider != "aixiangshu" and "aixiangshu.com" not in api_url.lower():
        _log(logger, f"Stripe hCaptcha provider not wired for card flow: {provider}")
        return ""
    try:
        from .paypal_plus.signup import _solve_aixiangshu_gateway

        token, _solution = _solve_aixiangshu_gateway(
            api_url=api_url or "https://sub.aixiangshu.com/captcha",
            api_key=api_key,
            task={
                "type": "HCaptchaTask",
                "websiteURL": _text(config.hcaptcha_website_url)
                or STRIPE_HCAPTCHA_URL,
                "websiteKey": _text(config.hcaptcha_site_key)
                or STRIPE_HCAPTCHA_SITE_KEY,
                "isInvisible": True,
                "proxy": _text(proxy),
            },
            timeout=max(60, min(300, int(config.timeout or DEFAULT_TIMEOUT))),
            label="stripe-hcaptcha",
            token_fields=("gRecaptchaResponse", "hcaptchaToken", "token"),
        )
    except Exception as exc:
        raise CardPaymentError(
            f"Stripe hCaptcha solve failed: {type(exc).__name__}"
        ) from exc
    if not token:
        raise CardPaymentError("Stripe hCaptcha solve returned no token")
    _log(logger, "Stripe hCaptcha token ready")
    return token


def run_card_payment(
    config: CardPaymentConfig,
    *,
    proxy: str = "",
    logger: Callable[[str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    session_factory: Callable[[CardPaymentConfig, str], Any] | None = None,
    refresh_checkout: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run bind-card -> checkout-confirm -> subscription verification."""
    log = logger or (lambda _message: None)
    config.token = _text(config.token)
    if not config.token:
        raise CardPaymentError("missing access token")
    external_payment_method = _text(config.payment_method_id)
    if external_payment_method:
        if not re.fullmatch(r"pm_[A-Za-z0-9_-]+", external_payment_method):
            raise CardPaymentError("invalid browser PaymentMethod id")
    else:
        _card_fields(config.card)
    session = session_factory(config, proxy) if session_factory else _new_session(config, proxy)
    checkout: dict[str, Any] = {}
    init_payload: dict[str, Any] = {}
    proxy_retry_safe = True
    try:
        _check_cancel(is_cancelled)
        _hydrate_payment_metadata(session, config, log)
        bind_checkout: dict[str, Any] = {}
        if _text(config.checkout_id):
            checkout = {
                "checkout_session_id": _text(config.checkout_id),
                "processor_entity": _text(config.processor_entity),
                "publishable_key": _text(config.publishable_key),
            }
            log(f"using canonical zero-amount checkout: {_mask(config.checkout_id)}")
        else:
            checkout = _checkout_create(session, config)
        checkout_id = _checkout_id(checkout)
        if not checkout_id:
            raise CardPaymentError("checkout create: missing checkout id")
        processor = _processor_entity(checkout, config.country)
        checkout_amount = _find_key(
            checkout, ("amount", "amount_total", "total_amount")
        ) or "0"
        checkout_publishable_key = _text(config.publishable_key) or _find_key(
            checkout, ("publishable_key", "publishableKey")
        )
        publishable_key = checkout_publishable_key
        config.checkout_id = checkout_id
        account_id = _text(config.account_id) or _find_key(
            checkout, ("account_id", "accountId")
        )
        if not account_id:
            raise CardPaymentError("payment method prepare: missing account id")
        config.account_id = account_id

        # The current web client obtains Stripe.js from this account-scoped
        # bootstrap endpoint.  A Checkout-derived key is not a reliable
        # substitute when accounts are split across Stripe merchant shards.
        account_publishable_key = _stripe_client_bootstrap(session, config)
        browser_publishable_key = _text(config.payment_method_publishable_key)
        if (
            external_payment_method
            and browser_publishable_key
            and browser_publishable_key != account_publishable_key
        ):
            raise CardPaymentError(
                "payment method merchant shard mismatch: browser card key does not "
                "match the account Stripe bootstrap key"
            )
        if (
            _text(config.flow_mode) == "link_pay"
            and checkout_publishable_key.startswith(("pk_live_", "pk_test_"))
            and checkout_publishable_key != account_publishable_key
        ):
            raise CardPaymentError(
                "direct payment merchant shard mismatch: Checkout key does not "
                "match the account Stripe bootstrap key"
            )
        publishable_key = account_publishable_key
        log(f"account Stripe merchant ready: {_mask(publishable_key)}")

        stripe_js_id = str(uuid.uuid4())
        direct_payment = _text(config.flow_mode) == "link_pay"
        if direct_payment:
            payment_method_id = _text(config.payment_method_id)
            if not payment_method_id.startswith("pm_"):
                raise CardPaymentError("direct payment: missing PaymentMethod")
            stripe_context: dict[str, Any] = {}
            customer = _find_identifier(checkout, ("cus_",)) or _find_key(
                checkout, ("customer", "customer_id", "customerId")
            )
            app_payment_methods_status = 0
            stripe_payment_methods_status = 0
            log(
                "FLOW_STEP:payment_prepare:done:direct Checkout payment method "
                f"{_mask(payment_method_id)} ready"
            )
        else:
            bind_step_key = (
                "payment_prepare" if _text(config.flow_mode) == "link_pay" else "bind"
            )
            bind_step_label = (
                "正在验证账号支付方式"
                if bind_step_key == "payment_prepare"
                else "正在执行绑卡"
            )
            log(f"FLOW_STEP:{bind_step_key}:start:{bind_step_label}")
            if config.strong_bind_direct:
                if not checkout_id.startswith("oaics_"):
                    raise CardPaymentError(
                        "strong bind: expected PH oaics checkout context"
                    )
                checkout_context = session.get(
                    f"{APP_BASE}/backend-api/payments/checkout/{processor}/{checkout_id}",
                    headers=_app_headers(
                        f"{APP_BASE}/checkout/{processor}/{checkout_id}",
                        "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}",
                        payment_attestation=_payment_attestation(session),
                        session=session,
                        json_content=False,
                    ),
                    timeout=config.timeout,
                )
                _capture_oai_update(session, checkout_context)
                if int(getattr(checkout_context, "status_code", 0) or 0) >= 400:
                    raise CardPaymentError(
                        f"strong bind checkout context: {_response_error(checkout_context)}"
                    )
                payment_method_prepare_payload = _post_json(
                    session,
                    f"{APP_BASE}/backend-api/payments/payment_method",
                    {"account_id": account_id},
                    timeout=config.timeout,
                    stage="payment method prepare",
                    route="/backend-api/payments/payment_method",
                )
                stripe_context = {
                    "payment_method_prepare": payment_method_prepare_payload
                }
                setup_secret = _find_client_secret(payment_method_prepare_payload)
                setup_id = _setup_intent_id(
                    payment_method_prepare_payload, setup_secret
                )
                setup_publishable_key = _find_key(
                    payment_method_prepare_payload,
                    ("publishable_key", "publishableKey"),
                ) or _publishable_key_for_setup(setup_secret, publishable_key)
                if setup_publishable_key != publishable_key:
                    raise CardPaymentError(
                        "payment method prepare: SetupIntent merchant shard does not "
                        "match the account Stripe bootstrap key"
                    )
                log(f"PH oaics strong-bind SetupIntent ready: {_mask(setup_id)}")
            else:
                payment_page_id = _text(config.payment_page_id)
                if not payment_page_id:
                    bind_checkout = _checkout_create(
                        session,
                        config,
                        country=_text(config.bind_country) or "US",
                        currency=_text(config.bind_currency) or "USD",
                        promo_campaign="",
                    )
                    payment_page_id = _find_identifier(
                        bind_checkout, ("cs_live_", "cs_test_", "cs_")
                    )
                if not payment_page_id.startswith(("cs_live_", "cs_test_", "cs_")):
                    raise CardPaymentError("bind checkout: invalid Stripe payment page id")
                publishable_key = _find_key(
                    bind_checkout, ("publishable_key", "publishableKey")
                ) or publishable_key or _find_identifier(
                    bind_checkout, ("pk_live_", "pk_test_")
                )
                init_payload, stripe_js_id = _init_payment_page(
                    session, config, payment_page_id, publishable_key, processor
                )
                elements_payload = _create_elements_session(
                    session,
                    config,
                    init_payload,
                    payment_page_id,
                    publishable_key,
                    stripe_js_id,
                )
                payment_method_prepare_payload = _post_json(
                    session,
                    f"{APP_BASE}/backend-api/payments/payment_method",
                    {"account_id": account_id},
                    timeout=config.timeout,
                    stage="payment method prepare",
                    route="/backend-api/payments/payment_method",
                )
                stripe_context = {
                    "init": init_payload,
                    "elements": elements_payload,
                    "payment_method_prepare": payment_method_prepare_payload,
                }
                setup_secret = (
                    _find_client_secret(payment_method_prepare_payload)
                    or _find_client_secret(elements_payload)
                    or _find_client_secret(init_payload)
                )
                setup_id = (
                    _setup_intent_id(payment_method_prepare_payload, setup_secret)
                    or _setup_intent_id(elements_payload, setup_secret)
                    or _setup_intent_id(init_payload, setup_secret)
                )
                log(f"Stripe payment-page SetupIntent ready: {_mask(setup_id)}")
            if not setup_id or not setup_secret:
                raise CardPaymentError("strong bind: missing SetupIntent credentials")
            if not publishable_key.startswith(("pk_live_", "pk_test_")):
                raise CardPaymentError("strong bind: missing matching publishable key")

            config.hcaptcha_token = _prepare_hcaptcha_token(
                config,
                proxy=proxy,
                logger=log,
            )

            confirm_response = None
            confirm_payload: dict[str, Any] = {}
            max_attempts = max(1, min(3, int(config.max_setup_confirm_attempts or 1)))
            for attempt in range(1, max_attempts + 1):
                _check_cancel(is_cancelled)
                # SetupIntent confirmation can attach a payment method.  Any
                # transport error from this point has an unknown remote result
                # and must not restart the whole flow on another proxy.
                proxy_retry_safe = False
                confirm_response, confirm_payload = _setup_confirm(
                    session,
                    config,
                    stripe_context,
                    checkout_id,
                    processor,
                    publishable_key,
                    setup_id,
                    setup_secret,
                    stripe_js_id,
                    attempt=attempt,
                )
                status = int(getattr(confirm_response, "status_code", 0) or 0)
                if status == 200:
                    break
                if status == 402 and attempt < max_attempts:
                    log(f"setup confirm returned 402; retry {attempt + 1}/{max_attempts}")
                    continue
                raise CardPaymentError(f"setup confirm: {_response_error(confirm_response)}")
            _require_intent_succeeded(confirm_payload, "setup confirm")
            payment_method_id = _find_key(confirm_payload, ("payment_method", "payment_method_id")) or _find_identifier(confirm_payload, ("pm_",))
            if not payment_method_id:
                raise CardPaymentError("setup confirm: missing PaymentMethod")
            log(f"payment method created: {_mask(payment_method_id)}")

            customer = _find_identifier(
                {"stripe_context": stripe_context, "setup_confirm": confirm_payload},
                ("cus_",),
            ) or _find_identifier(bind_checkout or checkout, ("cus_",))
            if not customer:
                for payload in (
                    confirm_payload,
                    stripe_context,
                    bind_checkout,
                    checkout,
                ):
                    candidate = _find_key(payload, ("customer", "customer_id", "customerId"))
                    if isinstance(candidate, str) and candidate:
                        customer = candidate
                        break

            app_payment_methods_status = 0
            if account_id:
                app_payment_method_ids: list[str] = []
                payment_method_delays = (
                    tuple(config.payment_method_poll_delays) or (0.0,)
                )
                for delay in payment_method_delays:
                    _check_cancel(is_cancelled)
                    if delay > 0:
                        time.sleep(max(0.0, float(delay)))
                    payment_methods = session.get(
                        f"{APP_BASE}/backend-api/payments/payment_methods",
                        params={"account_id": account_id},
                        headers=_app_headers(
                            f"{APP_BASE}/",
                            "/backend-api/payments/payment_methods",
                            payment_attestation=_payment_attestation(session),
                            session=session,
                            json_content=False,
                        ),
                        timeout=config.timeout,
                    )
                    _capture_oai_update(session, payment_methods)
                    app_payment_methods_status = int(
                        getattr(payment_methods, "status_code", 0) or 0
                    )
                    if app_payment_methods_status >= 400:
                        raise CardPaymentError(
                            f"payment methods sync: {_response_error(payment_methods)}"
                        )
                    app_payment_method_ids = _find_identifiers(
                        _response_json(payment_methods), ("pm_",)
                    )
                    if payment_method_id in app_payment_method_ids:
                        break
                if payment_method_id not in app_payment_method_ids:
                    raise CardPaymentError(
                        "payment methods sync: bound card is not visible on account"
                    )

            stripe_payment_methods_status = 0
            if customer:
                stripe_payment_methods = session.get(
                    f"{STRIPE_BASE}/v1/payment_methods",
                    params={"customer": customer, "type": "card", "limit": 30},
                    headers=_stripe_headers(
                        publishable_key,
                        f"{APP_BASE}/checkout/{processor}/{checkout_id}",
                        stripe_version=STRIPE_BETAS,
                    ),
                    timeout=config.timeout,
                )
                stripe_payment_methods_status = int(
                    getattr(stripe_payment_methods, "status_code", 0) or 0
                )
                if stripe_payment_methods_status >= 400:
                    raise CardPaymentError(
                        f"stripe payment methods: {_response_error(stripe_payment_methods)}"
                    )
                stripe_payment_method_ids = _find_identifiers(
                    _response_json(stripe_payment_methods), ("pm_",)
                )
                if payment_method_id not in stripe_payment_method_ids:
                    raise CardPaymentError(
                        "stripe payment methods: bound card is missing from customer"
                    )

            log(
                f"FLOW_STEP:{bind_step_key}:done:支付方式 {_mask(payment_method_id)} 已确认并同步"
            )

            if config.stop_after_bind:
                log("card bind completed; stopped before checkout refresh and payment")
                return {
                    "ok": True,
                    "checkout_id": checkout_id,
                    "processor_entity": processor,
                    "payment_method": _mask(payment_method_id),
                    "card_last4": _payment_card_last4(config),
                    "setup_status": _text(confirm_payload.get("status")) or "succeeded",
                    "app_payment_methods_status": app_payment_methods_status,
                    "stripe_payment_methods_status": stripe_payment_methods_status,
                    "bind_only": True,
                }

            if refresh_checkout is not None:
                _check_cancel(is_cancelled)
                refreshed_checkout = refresh_checkout() or {}
                refreshed_id = _checkout_id(refreshed_checkout)
                refreshed_amount = _text(refreshed_checkout.get("amount"))
                if not _supported_checkout_id(refreshed_id):
                    raise CardPaymentError(
                        "checkout refresh: missing supported checkout id"
                    )
                if refreshed_amount != "0":
                    raise CardPaymentError(
                        f"checkout refresh: expected zero amount, got {refreshed_amount or 'unknown'}"
                    )
                checkout_id = refreshed_id
                checkout_amount = refreshed_amount
                processor = _processor_entity(refreshed_checkout, config.country)
                refreshed_key = _text(
                    refreshed_checkout.get("_publishable_key")
                )
                if (
                    refreshed_key.startswith(("pk_live_", "pk_test_"))
                    and refreshed_key != publishable_key
                ):
                    raise CardPaymentError(
                        "checkout refresh: Stripe merchant shard changed after card bind"
                    )
                refreshed_currency = _text(refreshed_checkout.get("currency"))
                if refreshed_currency:
                    config.currency = refreshed_currency.upper()
                config.checkout_id = checkout_id
                log(f"checkout refreshed after card bind: {_mask(checkout_id)}")

        log("FLOW_STEP:payment:start:正在提交支付确认")

        checkout_context = session.get(
            f"{APP_BASE}/backend-api/payments/checkout/{processor}/{checkout_id}",
            headers=_app_headers(
                f"{APP_BASE}/checkout/{processor}/{checkout_id}",
                "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}",
                payment_attestation=_payment_attestation(session),
                session=session,
                json_content=False,
            ),
            timeout=config.timeout,
        )
        _capture_oai_update(session, checkout_context)
        if int(getattr(checkout_context, "status_code", 0) or 0) >= 400:
            raise CardPaymentError(
                f"canonical checkout restore: {_response_error(checkout_context)}"
            )
        checkout_context_payload = _response_json(checkout_context)
        if not customer:
            customer = _find_identifier(
                checkout_context_payload, ("cus_",)
            ) or _find_key(
                checkout_context_payload,
                ("customer", "customer_id", "customerId"),
            )

        payment_elements_payload = _create_payment_elements_session(
            session,
            config,
            checkout_context_payload,
            publishable_key,
            stripe_js_id,
            amount=checkout_amount,
        )
        stripe_context["payment_elements"] = payment_elements_payload
        log(
            "payment Elements session ready: "
            f"{_mask(_find_identifier(payment_elements_payload, ('elements_session_',)))}"
        )

        billing_fields = _billing_fields(config)
        billing_country = billing_fields.get("country") or config.country.upper()
        billing_address = {
            key: value
            for key, value in {
                "line1": billing_fields.get("line1"),
                "line2": billing_fields.get("line2"),
                "city": billing_fields.get("city"),
                "state": billing_fields.get("state"),
                "postal_code": billing_fields.get("postal_code"),
                "country": billing_country.upper(),
            }.items()
            if value
        }
        taxes_payload = _post_json(
            session,
            f"{APP_BASE}/backend-api/payments/checkout/taxes",
            {
                "checkout_session_id": checkout_id,
                "checkout_email": billing_fields.get("email") or "",
                "billing_country": billing_country.upper(),
                "billing_name": billing_fields.get("name") or "",
                "currency": config.currency.lower(),
                "processor_entity": processor,
                "billing_address": billing_address,
            },
            timeout=config.timeout,
            stage="checkout taxes",
            referer=f"{APP_BASE}/checkout/{processor}/{checkout_id}",
            route="/backend-api/payments/checkout/taxes",
        )
        taxes_checkout = (
            taxes_payload.get("checkout_session")
            if isinstance(taxes_payload.get("checkout_session"), dict)
            else {}
        )
        taxes_status = _text(taxes_checkout.get("status")).lower()
        if taxes_status == "expired":
            raise CardPaymentError(
                "checkout taxes: checkout session expired",
                proxy_retry_safe=False,
            )

        # The browser-created PaymentMethod already carries this account's full
        # billing_details.  The repeated PaymentMethod updates visible in HAR
        # are Address Element hydration events, not a prerequisite for confirm.
        log("browser PaymentMethod billing details ready")

        token_body: dict[str, Any] = {
            "payment_method": payment_method_id,
            "setup_future_usage": "off_session",
            "set_as_default_payment_method": "false",
            "client_context[currency]": config.currency.lower(),
            "client_context[mode]": "subscription",
            "client_context[payment_method_types][0]": "link",
            "client_context[payment_method_types][1]": "card",
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "2021",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
            "key": publishable_key,
        }
        if customer:
            token_body["client_context[customer]"] = customer
        elements_session_id = _find_key(
            stripe_context, ("elements_session_id", "elementsSessionId")
        ) or _find_identifier(stripe_context, ("elements_session_",))
        elements_session_config_id = _find_key(
            stripe_context,
            ("elements_session_config_id", "elementsSessionConfigId", "config_id"),
        )
        if elements_session_id:
            token_body["client_attribution_metadata[elements_session_id]"] = (
                elements_session_id
            )
        if elements_session_config_id:
            token_body[
                "client_attribution_metadata[elements_session_config_id]"
            ] = elements_session_config_id
        response, token_payload = _post_form(
            session,
            f"{STRIPE_BASE}/v1/confirmation_tokens",
            token_body,
            key=publishable_key,
            referer=f"{APP_BASE}/checkout/{processor}/{checkout_id}",
            timeout=config.timeout,
            stage="confirmation token",
            stripe_version=STRIPE_BETAS,
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise CardPaymentError(f"confirmation token: {_response_error(response)}")
        confirmation_token = _find_key(token_payload, ("confirmation_token", "confirmationToken")) or _find_identifier(token_payload, ("ctoken_", "ct_"))
        if not confirmation_token:
            raise CardPaymentError("confirmation token: missing token")

        sentinel_token = _text(config.sentinel_token)
        sentinel_telemetry = _text(config.telemetry)
        if sentinel_token and not sentinel_telemetry:
            # Current checkout JS uses this timing fallback when token()
            # succeeds but SentinelSDK.timing() returns null.
            sentinel_telemetry = "[1,null]"
        missing_confirm_context = []
        if not sentinel_token:
            missing_confirm_context.append("OpenAI-Sentinel-Token")
        if not sentinel_telemetry:
            missing_confirm_context.append("OAI-Telemetry")
        if not _payment_attestation(session):
            missing_confirm_context.append("OAI-Web-Deployment-Attestation")
        if missing_confirm_context:
            raise CardPaymentError(
                "checkout confirm precondition: missing flow-bound "
                + ", ".join(missing_confirm_context),
                proxy_retry_safe=False,
                action_required="checkout_session_confirmation",
                checkout_id=checkout_id,
                processor_entity=processor,
                checkout_link=(
                    f"{APP_BASE}/checkout/{processor}/{checkout_id}"
                    if processor and checkout_id
                    else ""
                ),
            )
        # The application confirm starts the final checkout mutation.  A lost
        # response is ambiguous, so a proxy fallback must not replay it.
        proxy_retry_safe = False
        app_confirm_payload = _post_json(
            session,
            f"{APP_BASE}/backend-api/payments/checkout/confirm",
            {
                "checkout_session_id": checkout_id,
                "confirm_token": confirmation_token,
                "selected_payment_method_type": "card",
            },
            timeout=config.timeout,
            stage="checkout confirm",
            referer=f"{APP_BASE}/checkout/{processor}/{checkout_id}",
            route="/backend-api/payments/checkout/confirm",
            additional_headers={
                "OpenAI-Sentinel-Token": sentinel_token,
                "OAI-Telemetry": sentinel_telemetry,
            },
        )
        checkout_status = _text(app_confirm_payload.get("status")).lower()
        if checkout_status in {"failed", "blocked"}:
            # HTTP 200 only means the confirm endpoint handled the request.
            # `status=blocked` is the account/checkout risk-control business
            # branch and happens before Stripe creates or confirms an Intent.
            # The mutation has already been submitted, so never replay it on
            # another proxy or automatically retry the account.
            detail = (
                " (account risk control, before Stripe intent confirm)"
                if checkout_status == "blocked"
                else ""
            )
            raise CardPaymentError(
                f"checkout confirm: server returned {checkout_status}{detail}",
                proxy_retry_safe=False,
            )
        conditional_offer_flow = (
            app_confirm_payload.get("conditional_offer_preflight") is True
        )
        if conditional_offer_flow and _text(app_confirm_payload.get("type")).lower() == "setup_intent":
            preflight_secret = _find_client_secret(app_confirm_payload)
            preflight_id = _setup_intent_id(app_confirm_payload, preflight_secret)
            if not preflight_id or not preflight_secret:
                raise CardPaymentError(
                    "conditional offer: missing preflight SetupIntent credentials"
                )
            preflight_response = session.get(
                f"{STRIPE_BASE}/v1/setup_intents/{preflight_id}",
                params={
                    "client_secret": preflight_secret,
                    "key": publishable_key,
                },
                headers=_stripe_headers(
                    publishable_key,
                    f"{APP_BASE}/checkout/{processor}/{checkout_id}",
                ),
                timeout=config.timeout,
            )
            if int(getattr(preflight_response, "status_code", 0) or 0) >= 400:
                raise CardPaymentError(
                    f"conditional offer preflight: {_response_error(preflight_response)}"
                )
            preflight_payload = _response_json(preflight_response)
            preflight_status = _text(preflight_payload.get("status")).lower()
            if preflight_status != "succeeded":
                raise CardPaymentError(
                    "conditional offer preflight: "
                    f"{preflight_status or 'missing status'} requires next_action"
                )
            app_confirm_payload = _post_json(
                session,
                f"{APP_BASE}/backend-api/payments/checkout/confirm",
                {"checkout_session_id": checkout_id},
                timeout=config.timeout,
                stage="conditional offer continuation",
                referer=f"{APP_BASE}/checkout/{processor}/{checkout_id}",
                route="/backend-api/payments/checkout/confirm",
                additional_headers={
                    "OpenAI-Sentinel-Token": sentinel_token,
                    "OAI-Telemetry": sentinel_telemetry,
                },
            )
        if conditional_offer_flow and (
            app_confirm_payload.get("conditional_offer_preflight") is not True
            or _text(app_confirm_payload.get("type")).lower() != "payment_intent"
        ):
            raise CardPaymentError(
                "conditional offer continuation: expected PaymentIntent"
            )
        final_secret = _find_client_secret(app_confirm_payload)
        intent_type = _text(app_confirm_payload.get("type")).lower()
        if intent_type not in {"setup_intent", "payment_intent"}:
            if final_secret.startswith("seti_"):
                intent_type = "setup_intent"
            elif final_secret.startswith("pi_"):
                intent_type = "payment_intent"
        if intent_type == "setup_intent":
            final_intent_id = _setup_intent_id(app_confirm_payload, final_secret)
            final_endpoint = f"{STRIPE_BASE}/v1/setup_intents/{final_intent_id}/confirm"
        elif intent_type == "payment_intent":
            final_intent_id = _payment_intent_id(app_confirm_payload, final_secret)
            final_endpoint = f"{STRIPE_BASE}/v1/payment_intents/{final_intent_id}/confirm"
        else:
            final_intent_id = ""
            final_endpoint = ""
        if not final_intent_id or not final_secret:
            raise CardPaymentError(
                "checkout confirm: missing final payment intent credentials"
            )
        return_url = (
            f"{APP_BASE}/checkout/verify?stripe_session_id={checkout_id}"
            f"&processor_entity={processor}&plan_type=plus"
        )
        final_body = {
            "client_secret": final_secret,
            "key": publishable_key,
            "return_url": return_url,
            "_stripe_version": STRIPE_BETAS,
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[merchant_integration_source]": "l1",
        }
        if not conditional_offer_flow:
            final_body["confirmation_token"] = confirmation_token
        response, final_payload = _post_form(
            session,
            final_endpoint,
            final_body,
            key=publishable_key,
            referer=f"{APP_BASE}/checkout/{processor}/{checkout_id}",
            timeout=config.timeout,
            stage="final setup confirm",
        )
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise CardPaymentError(f"final intent confirm: {_response_error(response)}")
        final_intent_status = _require_intent_succeeded(
            final_payload, "final intent confirm"
        )
        _check_cancel(is_cancelled)

        log("FLOW_STEP:payment:done:支付确认已提交")
        log("FLOW_STEP:verify:start:正在校验 Checkout 与订阅状态")

        verify_page = None
        success_data = None
        auth_session = None
        auth_refresh_session = None
        success_page_referer = f"{APP_BASE}/"
        if not config.fast_verify:
            verify_params = {
                "stripe_session_id": checkout_id,
                "processor_entity": processor,
                "plan_type": "plus",
                "redirect_status": final_intent_status,
            }
            if intent_type == "setup_intent":
                verify_params.update(
                    {
                        "setup_intent": final_intent_id,
                        "setup_intent_client_secret": final_secret,
                    }
                )
            else:
                verify_params.update(
                    {
                        "payment_intent": final_intent_id,
                        "payment_intent_client_secret": final_secret,
                    }
                )
            verify_referer = f"{APP_BASE}/checkout/verify?{urlencode(verify_params)}"
            navigation_headers = _app_headers(
                f"{APP_BASE}/checkout/{processor}/{checkout_id}",
                session=session,
                json_content=False,
            )
            navigation_headers.update(
                {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                    "Origin": None,
                    "priority": "u=0, i",
                }
            )
            verify_page = session.get(
                f"{APP_BASE}/checkout/verify",
                params=verify_params,
                headers=navigation_headers,
                timeout=config.timeout,
            )
            _capture_oai_update(session, verify_page)

        verify = session.get(
            f"{APP_BASE}/backend-api/payments/checkout/{processor}/{checkout_id}",
            headers=_app_headers(
                f"{APP_BASE}/checkout/verify",
                "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}",
                payment_attestation=_payment_attestation(session),
                session=session,
                json_content=False,
            ),
            timeout=config.timeout,
        )
        _capture_oai_update(session, verify)
        if int(getattr(verify, "status_code", 0) or 0) >= 400:
            raise CardPaymentError(f"checkout verify: {_response_error(verify)}")

        if not config.fast_verify:
            success_params = {
                "stripe_session_id": checkout_id,
                "plan_type": "plus",
                "processor_entity": processor,
                "_routes": "routes/payments.success",
            }
            success_data = session.get(
                f"{APP_BASE}/payments/success.data",
                params=success_params,
                headers=_app_headers(
                    verify_referer,
                    session=session,
                    json_content=False,
                ),
                timeout=config.timeout,
            )
            _capture_oai_update(session, success_data)
            success_page_referer = (
                f"{APP_BASE}/payments/success?"
                + urlencode(
                    {
                        "stripe_session_id": checkout_id,
                        "plan_type": "plus",
                        "processor_entity": processor,
                    }
                )
            )
            auth_session = session.get(
                f"{APP_BASE}/api/auth/session",
                params={
                    "workspace_update": "true",
                    "reason": "checkout_success",
                    "path": "/payments/success",
                },
                headers=_app_headers(
                    success_page_referer,
                    "/api/auth/session",
                    session=session,
                    json_content=False,
                ),
                timeout=config.timeout,
            )
            _capture_oai_update(session, auth_session)
            if _adopt_auth_session_token(session, auth_session):
                log("workspace auth session updated for subscription verification")
        subscription = None
        subscription_plan = ""
        subscription_auth_refreshed = False
        # The subscription endpoint can lag a successful zero-amount SetupIntent
        # by a fraction of a second.  Poll briefly, but never report success from
        # HTTP 200 alone: the account must actually show the Plus plan.
        subscription_delays = tuple(config.subscription_poll_delays) or (0.0,)
        for subscription_attempt, delay in enumerate(subscription_delays, start=1):
            _check_cancel(is_cancelled)
            if delay > 0:
                time.sleep(max(0.0, float(delay)))
            subscription = session.get(
                f"{APP_BASE}/backend-api/subscriptions",
                params={"account_id": account_id},
                headers=_app_headers(
                    f"{APP_BASE}/",
                    "/backend-api/subscriptions",
                    session=session,
                    json_content=False,
                ),
                timeout=config.timeout,
            )
            _capture_oai_update(session, subscription)
            subscription_http = int(
                getattr(subscription, "status_code", 0) or 0
            )
            if subscription_http == 401 and not subscription_auth_refreshed:
                # This is the same one-shot recovery used by the web fetch
                # wrapper after a token_expired response.  Retry only the GET;
                # never replay any bind or checkout-confirm mutation.
                subscription_auth_refreshed = True
                auth_refresh_session = session.get(
                    f"{APP_BASE}/api/auth/session",
                    params={
                        "refresh": "true",
                        "reason": "token_expired",
                        "method": "GET",
                        "path": "/backend-api/subscriptions",
                    },
                    headers=_app_headers(
                        success_page_referer,
                        "/api/auth/session",
                        session=session,
                        json_content=False,
                    ),
                    timeout=config.timeout,
                )
                _capture_oai_update(session, auth_refresh_session)
                refresh_http = int(
                    getattr(auth_refresh_session, "status_code", 0) or 0
                )
                if refresh_http == 200:
                    _adopt_auth_session_token(session, auth_refresh_session)
                    subscription = session.get(
                        f"{APP_BASE}/backend-api/subscriptions",
                        params={"account_id": account_id},
                        headers=_app_headers(
                            f"{APP_BASE}/",
                            "/backend-api/subscriptions",
                            session=session,
                            json_content=False,
                        ),
                        timeout=config.timeout,
                    )
                    _capture_oai_update(session, subscription)
                    subscription_http = int(
                        getattr(subscription, "status_code", 0) or 0
                    )
            if subscription_http >= 400:
                raise CardPaymentError(
                    f"subscription verify: {_response_error(subscription)}"
                )
            subscription_payload = _response_json(subscription)
            subscription_plan = _text(subscription_payload.get("plan_type"))
            if subscription_plan.lower() == "plus":
                break
        if subscription_plan.lower() != "plus":
            raise CardPaymentError(
                "subscription verify: Plus plan was not activated"
            )
        log("payment succeeded and checkout verified")
        log("FLOW_STEP:verify:done:Checkout 与订阅状态校验完成")
        return {
            "ok": True,
            "checkout_id": checkout_id,
            "processor_entity": processor,
            "payment_method": _mask(payment_method_id),
            "card_last4": _payment_card_last4(config),
            "setup_status": final_intent_status,
            "intent_type": intent_type,
            "verify_status": int(getattr(verify, "status_code", 0) or 0),
            "verify_page_status": int(getattr(verify_page, "status_code", 0) or 0),
            "success_data_status": int(getattr(success_data, "status_code", 0) or 0),
            "auth_session_status": int(getattr(auth_session, "status_code", 0) or 0),
            "auth_refresh_status": int(getattr(auth_refresh_session, "status_code", 0) or 0),
            "app_payment_methods_status": app_payment_methods_status,
            "stripe_payment_methods_status": stripe_payment_methods_status,
            "subscription_status": int(getattr(subscription, "status_code", 0) or 0) if subscription is not None else 0,
            "subscription_plan": subscription_plan,
        }
    except Exception as exc:
        if isinstance(exc, CardPaymentError):
            exc.proxy_retry_safe = bool(
                getattr(exc, "proxy_retry_safe", True) and proxy_retry_safe
            )
            raise
        if not proxy_retry_safe:
            raise CardPaymentError(
                f"post-mutation transport failure: {exc}",
                proxy_retry_safe=False,
            ) from exc
        raise
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
