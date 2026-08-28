"""
GoPay 2.8.0 / GoTo-Gojek enhanced signing helper.

Verified against the latest Burp capture:
  - /goto-auth/login/methods X-E1 first segment: MATCH

Canonical form:
  {x-apptype};{x-phonemodel}:{bearer};{x-uniqueid}:;{md5(body)}:{host+path};
  {method}:{ts};{x-deviceos}:{x-appversion};{x-m1}:{x-appid};
  {nonce}:{x-phonemake};{x-platform}

Usage:
    from opai.core.gopay_signer_v2 import sign_v2

    result = sign_v2(
        token="Bearer eyJ...",
        timestamp_ms="1778088988941",
        url="customer.gopayapi.com/v1/linkedapps",
        method="GET",
    )
    # result["X-E1"], result["X-E2"]
"""
import hashlib
import hmac as hmac_mod
import os
import time

_DEFAULT_KEY = b"4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"

# GoPay 2.8.0 / GoTo enhanced signing static signature id for
# accounts.goto-products.com and api.gojekapi.com captures.
_V2_ID = "ED9A2B38749FBDE9ACA61D6A685B7"


def sign_v2(
    token: str = "",
    timestamp_ms: str = None,
    url: str = "",
    method: str = "GET",
    body: str = "",
    d1: str = "",
    model: str = "Xiaomi, MI 9",
    xm1: str = "",
    os_info: str = "Android,13",
    appid: str = "com.gojek.gopay",
    version: str = "2.8.0",
    adjts: str = "D",
    uniqueid: str = "",
    hmac_key: bytes = None,
    nonce_hex: str = None,
    phone_make: str = "Google",
    os_name: str = "Android",
) -> dict:
    """Sign a GoPay API request using the V2 algorithm.

    Returns dict with X-E1, X-E2, and debug fields (_hmac, _message).
    """
    if token.startswith("Bearer "):
        token = token[7:]

    if timestamp_ms is None:
        timestamp_ms = str(int(time.time() * 1000))

    body_hash = hashlib.md5(body.encode("utf-8")).hexdigest()

    if nonce_hex is None:
        nonce_hex = os.urandom(80).hex()

    if hmac_key is None:
        hmac_key = _DEFAULT_KEY

    message = (
        f"GOPAY;"
        f"{model}:{token};"
        f"{uniqueid}:;"
        f"{body_hash}:{url};"
        f"{method.upper()}:{timestamp_ms};"
        f"{os_info}:{version};"
        f"{xm1}:{appid};"
        f"{nonce_hex}:{phone_make};"
        f"{os_name}"
    )

    hmac_hex = hmac_mod.new(
        hmac_key, message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    x_e1 = f"{hmac_hex}:{nonce_hex}:{adjts}:{timestamp_ms}"

    return {
        "X-E1": x_e1,
        "X-E2": _V2_ID,
        "_hmac": hmac_hex,
        "_message": message,
        "_body_hash": body_hash,
    }
