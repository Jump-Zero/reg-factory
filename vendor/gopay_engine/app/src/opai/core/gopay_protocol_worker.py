"""
GoPay Pure-Protocol Worker — registration + payment parallel pipeline.

Self-contained deployment version — all imports are local (no C:\\tools dependency).

Each worker thread loops independently:
  1. Register GoPay account (rent phone → signup → refresh → PIN)
  2. Push account to inbox, wait for balance > 0
  3. Claim inbox job → pure-protocol Midtrans payment
  4. Done or failed → loop back to step 1
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import string
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import tls_client

from .sms_helpers import (
    sms_api, sms_get_number, sms_wait_code, sms_request_another,
    sms_cancel, sms_done, api_call_with_retry, get_error_code,
    get_sms_api_key, load_selected_env_file, sms_api_base_url, is_waf_block, is_rate_limited,
)
from .gojek_client import (
    GojekClient,
    CLIENT_ID as _GOJEK_CLIENT_ID,
    CLIENT_SECRET as _GOJEK_CLIENT_SECRET,
    looks_like_network_timeout,
    mask_proxy_url,
    probe_proxy_egress,
)

from .gopay_payment_protocol import GoPayPayment, GoPayFraudDenyError
from .payment_fingerprint import ensure_account_payment_fingerprint

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_selected_env_file(("OPAI_SMSBOWER_", "OPAI_HEROSMS_", "OPAI_GOPAY_PROXY_", "OPAI_GOPAY_REGISTER_PROXY"))

INBOX_URL = os.environ.get("OPAI_PAYMENT_INBOX_BASE_URL", "")
INBOX_USER = os.environ.get("OPAI_PAYMENT_INBOX_BASIC_USER", "")
INBOX_PASS = os.environ.get("OPAI_PAYMENT_INBOX_BASIC_PASS", "")
POLL_INTERVAL = float(os.environ.get("OPAI_GOPAY_POLL_INTERVAL", "10"))
MIN_REMAINING_SEC = int(os.environ.get("OPAI_GOPAY_MIN_REMAINING_SEC", "300"))
DEFAULT_PIN = os.environ.get("OPAI_GOPAY_DEFAULT_PIN", "147258")
MIN_BALANCE_RP = int(os.environ.get("OPAI_GOPAY_MIN_BALANCE_RP", "1"))
POST_PIN_BALANCE_WAIT_SEC = int(os.environ.get("OPAI_GOPAY_POST_PIN_BALANCE_WAIT_SEC", "180"))
POST_PIN_BALANCE_POLL_SEC = int(os.environ.get("OPAI_GOPAY_POST_PIN_BALANCE_POLL_SEC", "10"))
ENVELOPE_STORE_FILE = os.environ.get("OPAI_GOPAY_ENVELOPE_STORE", "config/envelope_links.json")

GOPAY_ACCOUNT_TTL = int(os.environ.get("OPAI_GOPAY_ACCOUNT_TTL_SEC", "1200"))

_NOVPROXY_TPL = os.environ.get("OPAI_GOPAY_PROXY_TEMPLATE", "")


def _normalize_proxy_url(raw: str) -> str:
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


def _make_proxy() -> str:
    override = os.environ.get("OPAI_GOPAY_REGISTER_PROXY", "").strip()
    if override:
        return _normalize_proxy_url(override)
    if not _NOVPROXY_TPL:
        return ""
    sid = "gp" + "".join(random.choices(string.ascii_letters + string.digits, k=6))
    return _normalize_proxy_url(_NOVPROXY_TPL.format(sid=sid))


def _preflight_proxy(proxy: str, note: Optional[Callable[[str], None]] = None) -> Optional[str]:
    proxy = _normalize_proxy_url(proxy)
    if not proxy:
        if note:
            note("代理预检: 未配置代理，将直连")
        return None
    if note:
        note(f"代理预检中: {mask_proxy_url(proxy)}")
    result = probe_proxy_egress(proxy)
    if result.get("ok"):
        if note:
            note(f"代理预检通过: 出口 IP {result.get('ip') or '-'}")
        return None
    detail = result.get("error") or result.get("raw") or f"HTTP {result.get('status')}"
    return f"代理预检失败: {mask_proxy_url(proxy)} {detail}"


# ---------------------------------------------------------------------------
# Inbox account sync
# ---------------------------------------------------------------------------

_INBOX_AUTH = None


def _inbox_auth_header() -> str:
    global _INBOX_AUTH
    if _INBOX_AUTH is None:
        _INBOX_AUTH = "Basic " + base64.b64encode(f"{INBOX_USER}:{INBOX_PASS}".encode()).decode()
    return _INBOX_AUTH


def _inbox_push_account(phone: str, data: dict):
    try:
        url = f"{INBOX_URL}/api/gopay-accounts"
        req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", _inbox_auth_header())
        urllib.request.urlopen(req, timeout=10)
        log.info("[inbox] %s pushed", phone)
    except Exception as e:
        log.warning("[inbox] %s push failed: %s", phone, e)


def _inbox_delete_account(phone: str):
    try:
        url = f"{INBOX_URL}/api/gopay-accounts/{urllib.parse.quote(phone, safe='')}"
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Authorization", _inbox_auth_header())
        urllib.request.urlopen(req, timeout=10)
        log.info("[inbox] %s deleted", phone)
    except Exception as e:
        log.debug("[inbox] %s delete failed: %s", phone, e)


def _inbox_ttl_cleanup():
    def _loop():
        while True:
            time.sleep(60)
            try:
                url = f"{INBOX_URL}/api/gopay-accounts"
                req = urllib.request.Request(url)
                req.add_header("Authorization", _inbox_auth_header())
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read().decode())
                now = time.time()
                for a in data.get("accounts", []):
                    added = a.get("added_at", "")
                    if not added:
                        continue
                    try:
                        ts = datetime.fromisoformat(added.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if now - ts > GOPAY_ACCOUNT_TTL:
                        phone = a.get("phone", "")
                        if phone:
                            log.info("[inbox-ttl] %s expired (%.0fs old), removing", phone, now - ts)
                            _inbox_delete_account(phone)
            except Exception as e:
                log.debug("[inbox-ttl] cleanup error: %s", e)

    t = threading.Thread(target=_loop, daemon=True, name="inbox-ttl")
    t.start()


# ---------------------------------------------------------------------------
# Deferred phone cancel
# ---------------------------------------------------------------------------

_CANCEL_MIN_AGE = int(os.environ.get("OPAI_GOPAY_CANCEL_MIN_AGE_SEC", "130"))


def _deferred_cancel_phone(api_key: str, activation_id: str, phone: str, rented_at: float):
    def _loop():
        _inbox_delete_account(phone)
        wait = max(0, _CANCEL_MIN_AGE - (time.time() - rented_at))
        if wait > 0:
            time.sleep(wait + 5)
        deadline = rented_at + 1200
        while time.time() < deadline:
            try:
                resp = sms_api(api_key, "setStatus", {"id": activation_id, "status": "8"})
                if "CANCEL" in (resp or "").upper() or "ACCESS" in (resp or "").upper():
                    log.info("[cancel] %s OK: %s", phone, resp)
                    return
                log.debug("[cancel] %s response: %s", phone, resp)
            except Exception as e:
                log.debug("[cancel] %s error: %s", phone, e)
            time.sleep(180)
        log.info("[cancel] %s gave up (SMS provider 20min auto-reclaim)", phone)

    t = threading.Thread(target=_loop, daemon=True, name=f"cancel-{phone}")
    t.start()


def _blocking_cancel_phone(api_key: str, activation_id: str, phone: str, rented_at: float) -> None:
    wait = max(0, _CANCEL_MIN_AGE - (time.time() - rented_at))
    if wait > 0:
        log.info("[cancel] %s waiting %.0fs before release", phone, wait)
        time.sleep(wait + 5)
    try:
        sms_cancel(api_key, activation_id)
    except Exception as exc:
        log.debug("[cancel] %s blocking cancel failed: %s", phone, exc)


# ---------------------------------------------------------------------------
# Account persistence
# ---------------------------------------------------------------------------

ACCOUNTS_FILE = os.environ.get(
    "OPAI_GOPAY_ACCOUNTS_FILE",
    str(Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "gopay_worker_accounts.json"),
)
_accounts_lock = threading.Lock()


def _save_account(phone: str, local: str, pin: str, aid: str, client: GojekClient):
    balance = _check_balance(client)
    if balance < 0:
        balance = 0
    customer_id = client.user_uuid or client.auth.account_id
    entry = {
        "phone": phone,
        "local": local,
        "pin": pin,
        "activation_id": aid,
        "customer_id": customer_id,
        "account_id": client.auth.account_id or customer_id,
        "device_token": client.device_token,
        "device_uniqueid": client.uniqueid,
        "device_session_id": client.session_id,
        "access_token": client.auth.access_token,
        "refresh_token": client.auth.refresh_token,
        "proxy": client.proxy,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "balance": balance,
    }
    with _accounts_lock:
        accounts = []
        if os.path.exists(ACCOUNTS_FILE):
            try:
                accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
            except Exception:
                pass
        replaced = False
        for i, account in enumerate(accounts):
            if account.get("phone") == phone or account.get("local") == local:
                if account.get("payment_fingerprint"):
                    entry["payment_fingerprint"] = account["payment_fingerprint"]
                else:
                    ensure_account_payment_fingerprint(entry)
                accounts[i] = {**account, **entry}
                replaced = True
                break
        if not replaced:
            ensure_account_payment_fingerprint(entry)
            accounts.append(entry)
        open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))
    log.info("[save] %s saved locally", phone)
    _inbox_push_account(phone, {**entry, "added_at": entry["registered_at"]})


def _update_account_balance(phone: str, balance: int, client: GojekClient):
    with _accounts_lock:
        accounts = []
        if os.path.exists(ACCOUNTS_FILE):
            try:
                accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
            except Exception:
                return
        for a in accounts:
            if a["phone"] == phone:
                ensure_account_payment_fingerprint(a)
                a["balance"] = balance
                a["access_token"] = client.auth.access_token
                a["refresh_token"] = client.auth.refresh_token
                break
        open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))
    log.info("[save] %s balance=%d updated locally", phone, balance)


def _check_balance(client: GojekClient) -> int:
    try:
        r = client.get_balance()
        if r["status"] == 200:
            data = r["body"].get("data", [])
            if isinstance(data, list) and data:
                return data[0].get("balance", {}).get("value", 0)
        return -1
    except Exception:
        return -1


def _wait_post_pin_balance(client: GojekClient, note: Callable[[str], None]) -> int:
    deadline = time.time() + max(0, POST_PIN_BALANCE_WAIT_SEC)
    interval = max(3, POST_PIN_BALANCE_POLL_SEC)
    last_balance = _check_balance(client)
    if last_balance > 0 or POST_PIN_BALANCE_WAIT_SEC <= 0:
        return last_balance

    note(f"余额暂为 {max(last_balance, 0)} Rp，继续等待系统异步到账，最多 {POST_PIN_BALANCE_WAIT_SEC}s")
    attempt = 1
    while time.time() < deadline:
        time.sleep(min(interval, max(1, deadline - time.time())))
        last_balance = _check_balance(client)
        if last_balance > 0:
            note(f"余额到账: {last_balance} Rp")
            return last_balance
        if last_balance >= 0:
            note(f"余额轮询 {attempt}: 仍为 {last_balance} Rp")
        else:
            note(f"余额轮询 {attempt}: 查询失败，继续")
        attempt += 1
    return last_balance


def _run_warmup_step(label: str, func: Callable[[], dict], note: Callable[[str], None]) -> dict:
    # Expose in-flight state; otherwise a slow next request makes the UI look
    # as if it stopped after the last completed Support SDK step.
    note(f"{label} 请求中")
    try:
        result = api_call_with_retry(func)
        if not isinstance(result, dict):
            result = {"status": 0, "body": {"error": f"invalid result type: {type(result).__name__}"}}
        status = result.get("status")
        if status in (200, 201, 204):
            note(f"{label} 请求完成 ({status})，继续后续流程")
        else:
            body = str(result.get("body", ""))[:180]
            note(f"{label} 返回 {status}，继续后续流程" + (f": {body}" if body else ""))
        return result
    except Exception as exc:
        log.debug("%s failed during post-PIN warmup", label, exc_info=True)
        note(f"{label} 异常，继续后续流程: {exc}")
        return {"status": 0, "body": {"error": str(exc)}}


def _run_real_device_pre_pin_warmup(client: GojekClient, note: Callable[[str], None]) -> None:
    """Replay the latest app warmup sequence after token refresh and before PIN setup."""
    note("开始执行真机 token 后、PIN 前 App 初始化链路")
    _run_warmup_step("Support SDK initiate 初始化", client.support_customer_initiate, note)
    _run_warmup_step(
        "Support SDK initiate 短包补发",
        lambda: client.support_customer_initiate(prefer_shortest=True),
        note,
    )
    _run_warmup_step("Support SDK actions 初始化", client.support_customer_actions, note)
    _run_warmup_step("公开实验配置初始化", client.litmus_public_experiments, note)
    _run_warmup_step("Gojek customer profile 初始化", client.gojek_customer_profile, note)
    _run_warmup_step("登录态实验配置初始化", client.litmus_experiments, note)
    _run_warmup_step("Chat profile 初始化", client.chat_profile, note)
    _run_warmup_step("Courier Token 初始化", client.courier_token, note)
    _run_warmup_step("GoFin Token 初始化", client.gofin_token, note)
    _run_warmup_step("支付方式 profiles 初始化", client.gopay_get_profiles, note)
    _run_warmup_step("GoPay 首页 BFF 初始化", client.gopay_home_v3, note)
    _run_warmup_step("节日红包资源初始化", client.festivals_assets, note)
    _run_warmup_step("App 条款/隐私 consent 同步", client.accept_signup_consents, note)
    _run_warmup_step("支付方式 balances 初始化", client.gopay_get_balances, note)
    _run_warmup_step("用户资料刷新", client.get_user_profile, note)
    _run_warmup_step("红点角标初始化", client.red_badges, note)
    _run_warmup_step("Cross-sell 配置初始化", client.cross_sells, note)
    _run_warmup_step("KYC 状态初始化", client.kyc_status, note)
    _run_warmup_step("PayLater profile 初始化", client.paylater_profile, note)
    _run_warmup_step("钱包卡片余额组件初始化", client.wallet_card_balance, note)
    _run_warmup_step("钱包卡片 widget 初始化", client.wallet_card_widget, note)
    _run_warmup_step("Push Token 绑定", client.update_push_token, note)
    _run_warmup_step("安全评分 gopay_home 刷新", lambda: client.security_meter("gopay_home"), note)
    _run_warmup_step("安全评分 account_safety_home 刷新", lambda: client.security_meter("account_safety_home"), note)
    _run_warmup_step("安全评分 security_meter 刷新", lambda: client.security_meter("security_meter"), note)


def _run_real_device_post_pin_warmup(client: GojekClient, note: Callable[[str], None]) -> None:
    """Replay the normal post-PIN app initialization seen in the real-device capture."""
    note("开始执行真机 PIN 后钱包初始化链路")
    _run_warmup_step("安全评分 gopay_home 刷新", lambda: client.security_meter("gopay_home"), note)
    _run_warmup_step("安全评分 account_safety_home 刷新", lambda: client.security_meter("account_safety_home"), note)
    _run_warmup_step("安全评分 security_meter 刷新", lambda: client.security_meter("security_meter"), note)
    _run_warmup_step(
        "安全提示 cyber_security_zero_policy 展示回传",
        lambda: client.security_meter(
            "security_meter",
            view_count=1,
            click_count=0,
            security_aware_identifier="cyber_security_zero_policy",
        ),
        note,
    )
    _run_warmup_step("用户资料刷新", client.get_user_profile, note)
    _run_warmup_step("Gojek customer profile 补刷新", client.gojek_customer_profile, note)
    _run_warmup_step("Courier Token 补刷新", client.courier_token, note)
    _run_warmup_step("公开实验配置补刷新", client.litmus_public_experiments, note)
    _run_warmup_step("登录态实验配置补刷新", client.litmus_experiments, note)
    _run_warmup_step("节日红包资源补刷新", client.festivals_assets, note)
    _run_warmup_step("支付方式 balances 补刷新", client.gopay_get_balances, note)
    _run_warmup_step("红点角标补刷新", client.red_badges, note)
    _run_warmup_step("支付方式 profiles 补刷新", client.gopay_get_profiles, note)
    _run_warmup_step("KYC 状态补刷新", client.kyc_status, note)
    _run_warmup_step("Support SDK session 上报", client.support_customer_session, note)
    _run_warmup_step("Support SDK activity 上报", client.support_customer_activity, note)


def _run_post_pin_hook(client: GojekClient, phone: str, note: Callable[[str], None], attempt: int) -> int:
    try:
        time.sleep(2 if attempt == 1 else 10)
        hook = api_call_with_retry(client.pin_post_registration_hook)
        status = int(hook.get("status", 0) or 0)
        if status in (200, 201):
            note(f"GoPay 钱包激活 hook 第 {attempt} 次完成: {status}")
        else:
            note(f"GoPay 钱包激活 hook 第 {attempt} 次返回 {status}")
        return status
    except Exception as exc:
        log.warning("[%s] post-registration hook attempt %d failed: %s", phone, attempt, exc)
        note(f"GoPay 钱包激活 hook 第 {attempt} 次异常: {exc}")
        return 0


def _run_post_pin_activation(client: GojekClient, phone: str, envelope_did: str, note: Callable[[str], None]) -> dict:
    """Activate wallet after PIN setup and replay app warmup without blocking on balance."""
    first_hook_status = _run_post_pin_hook(client, phone, note, 1)
    _run_real_device_post_pin_warmup(client, note)
    second_hook_status = 0
    if first_hook_status not in (200, 201):
        note("hook 首次未通过，刷新 token 后补打第二次 hook")
        try:
            refresh = api_call_with_retry(client.refresh_token)
            note(f"hook 补偿前 token refresh 返回 {refresh.get('status')}")
        except Exception as exc:
            note(f"hook 补偿前 token refresh 异常，继续补打 hook: {exc}")
        second_hook_status = _run_post_pin_hook(client, phone, note, 2)
        if second_hook_status in (200, 201):
            note("hook 第二次通过，补跑余额和安全状态刷新")
            _run_warmup_step("支付方式 balances 补刷新", client.gopay_get_balances, note)
            _run_warmup_step("钱包卡片余额组件补刷新", client.wallet_card_balance, note)
            _run_warmup_step("安全评分 security_meter 补刷新", lambda: client.security_meter("security_meter"), note)
        else:
            note("hook 第二次仍未通过，继续等待余额但该号可能不会触发系统赠送")
    note("真机钱包初始化完成，开始刷新系统余额")
    return {"first_hook_status": first_hook_status, "second_hook_status": second_hook_status}


def _run_post_pin_reward(client: GojekClient, phone: str, envelope_did: str, note: Callable[[str], None]) -> int:
    """Activate wallet after PIN setup, replay app warmup, then read balance."""
    _run_post_pin_activation(client, phone, envelope_did, note)
    time.sleep(1)
    balance = _wait_post_pin_balance(client, note)
    if balance >= 0:
        note(f"余额已刷新: {balance} Rp")
    else:
        note("余额刷新失败，worker 会继续轮询余额")
    return balance


def _claim_configured_envelope(client: GojekClient, note: Callable[[str], None]) -> Optional[dict]:
    try:
        from opai.core.envelope_manager import EnvelopeManager

        mgr = EnvelopeManager(Path(ENVELOPE_STORE_FILE))
        active = mgr.get_active()
        if not active:
            note("节日红包未配置 active 链接，跳过")
            return None
        note(f"开始领取节日红包，active 链接 {len(active)} 条")
        result = mgr.claim_one(client)
        if result and result.get("status") in (200, 201) and result.get("body", {}).get("success"):
            note("节日红包领取完成")
        elif result:
            note(f"节日红包领取失败: {result.get('status')} {str(result.get('body', ''))[:300]}")
        else:
            note("节日红包没有可领取的 active 链接")
        return result
    except Exception as exc:
        log.warning("configured envelope claim failed: %s", exc, exc_info=True)
        note(f"节日红包领取异常，继续流程: {exc}")
        return None


def _normalize_phone(phone: str) -> str:
    """Normalize Indonesian local/intl phone input to +62xxxxxxxx."""
    digits = "".join(ch for ch in phone.strip() if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("62"):
        pass
    elif digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    else:
        return ""
    local = digits[2:] if digits.startswith("62") else digits
    if not local.startswith(("81", "82", "83", "85", "87", "88", "89")):
        return ""
    if len(digits) < 10 or len(digits) > 15:
        return ""
    return f"+{digits}"


def _normalize_phone_for_country(phone: str, country_code: str) -> str:
    """Normalize a phone with an explicit country code for live probing."""
    digits = "".join(ch for ch in phone.strip() if ch.isdigit())
    country_digits = "".join(ch for ch in country_code.strip() if ch.isdigit())
    if not digits or not country_digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith(country_digits):
        return f"+{digits}"
    if digits.startswith("0"):
        return f"+{country_digits}{digits[1:]}"
    return f"+{country_digits}{digits}"


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _load_account_payment_fingerprint(phone: str) -> Optional[dict]:
    """Load and persist the saved payment fingerprint for an account."""
    if not os.path.exists(ACCOUNTS_FILE):
        return None
    target = _phone_digits(phone)
    with _accounts_lock:
        try:
            accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
        except Exception:
            return None
        if not isinstance(accounts, list):
            return None
        for idx, account in enumerate(accounts):
            if not isinstance(account, dict):
                continue
            item_phone = _phone_digits(account.get("phone", ""))
            item_local = _phone_digits(account.get("local", ""))
            if target and (
                target == item_phone
                or (item_local and target == item_local)
                or (item_phone and item_phone.endswith(target))
                or (item_local and target.endswith(item_local))
            ):
                profile = ensure_account_payment_fingerprint(account)
                accounts[idx] = account
                open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))
                return profile
    return None


def migrate_account_payment_fingerprints() -> dict:
    """Ensure every saved account has a stable payment fingerprint."""
    path = Path(ACCOUNTS_FILE)
    if not path.exists():
        return {"path": str(path), "total": 0, "updated": 0, "accounts": []}

    with _accounts_lock:
        try:
            accounts = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"read accounts failed: {exc}") from exc
        if not isinstance(accounts, list):
            raise RuntimeError("accounts file must contain a JSON list")

        updated = 0
        public_accounts = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            before = account.get("payment_fingerprint")
            profile = ensure_account_payment_fingerprint(account)
            if before != profile:
                updated += 1
            public_accounts.append({
                "phone": account.get("phone", ""),
                "local": account.get("local", ""),
                "profile_id": profile.get("profile_id", ""),
            })

        if updated:
            path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "path": str(path),
        "total": len(public_accounts),
        "updated": updated,
        "accounts": public_accounts,
    }


def _prompt_code(phone: str, purpose: str, timeout: int = 0) -> Optional[str]:
    label_map = {
        "signup": "注册 OTP",
        "pin": "PIN OTP",
        "login": "登录 OTP",
    }
    label = label_map.get(purpose, "OTP")
    suffix = f"（建议 {timeout}s 内输入）" if timeout else ""
    try:
        code = input(f"[manual] {label} 已发送到 {phone}，请输入验证码{suffix}: ").strip()
    except EOFError:
        return None
    return code or None


def _mask_code(code: str) -> str:
    if not code:
        return ""
    if len(code) <= 2:
        return "*" * len(code)
    return code[0] + ("*" * (len(code) - 2)) + code[-1]


def _signup_created_without_token(result: dict) -> bool:
    if int(result.get("status", 0) or 0) != 206:
        return False
    body = result.get("body") if isinstance(result, dict) else {}
    if not isinstance(body, dict):
        return False
    data = body.get("data", body)
    if not isinstance(data, dict):
        return False
    customer = data.get("customer", {})
    if not isinstance(customer, dict):
        return False
    return (
        body.get("success") is True
        and customer.get("active") is True
        and customer.get("phone_verified") is True
        and not data.get("access_token")
        and not data.get("refresh_token")
    )


def _register_one_from_phone(
    phone: str,
    aid: str,
    pin: str,
    proxy: str,
    envelope_did: str,
    wait_code: Callable[[str, int], Optional[str]],
    request_another_code: Optional[Callable[[], None]] = None,
    on_failure: Optional[Callable[[], None]] = None,
    country_code: str = "+62",
    signed_up_country: str = os.environ.get("OPAI_GOPAY_SIGNED_UP_COUNTRY", "62"),
    allow_unsupported_country: bool = False,
    return_existing: bool = False,
    return_failure: bool = False,
    status_cb: Optional[Callable[[str], None]] = None,
    relogin_after_register: bool = False,
    claim_envelope_after_register: bool = False,
    signup_otp_timeout: int = 180,
    pin_first_otp_timeout: int = 60,
    pin_retry_otp_timeout: int = 180,
    pin_otp_attempts: int = 3,
) -> Optional[dict]:
    """Full registration flow with pluggable phone/OTP providers."""
    def note(message: str) -> None:
        if status_cb:
            try:
                status_cb(message)
            except Exception:
                pass

    def fail(message: str) -> Optional[dict]:
        note(message)
        if return_failure:
            return {"failed": True, "phone": phone, "local": local if "local" in locals() else "", "error": message}
        return None

    country_code = country_code if country_code.startswith("+") else f"+{country_code}"
    phone = (
        _normalize_phone(phone)
        if country_code == "+62" and not allow_unsupported_country
        else _normalize_phone_for_country(phone, country_code)
    )
    if not phone:
        log.error("No phone number provided")
        return fail("手机号为空或格式不支持")

    country_digits = country_code.lstrip("+")
    local = phone.lstrip("+")
    if local.startswith(country_digits):
        local = local[len(country_digits):]

    proxy_error = _preflight_proxy(proxy, note)
    if proxy_error:
        log.warning("[%s] %s", phone, proxy_error)
        return fail(proxy_error)

    log.info("[%s] Proxy: %s", phone, proxy.split("@")[-1] if "@" in proxy else "direct")
    client = GojekClient.from_phone(phone, proxy=proxy)
    success = False

    try:
        _run_warmup_step("Support SDK initiate 启动", client.support_customer_initiate, note)
        _run_warmup_step("Support SDK actions 启动", client.support_customer_actions, note)

        # === Phase 1: Login check ===
        note("Support SDK actions 已完成，开始检测手机号是否已有 GoPay 账号")
        time.sleep(2)
        methods = client.get_login_methods(country_code, local)
        note(f"手机号账号检测返回 {methods.get('status')}，继续判断注册/登录分支")

        if methods["status"] in (200, 201):
            log.info("[%s] Already registered, skipping", phone)
            note("号码已注册，不能作为新号注册")
            if return_existing:
                return {
                    "phone": phone,
                    "aid": aid,
                    "pin": pin,
                    "client": client,
                    "local": local,
                    "already_registered": True,
                    "login_methods": methods.get("body", {}),
                }
            return None

        err_code = get_error_code(methods)
        if methods["status"] == 403 or is_waf_block(methods):
            log.warning("[%s] WAF 403, need new proxy IP", phone)
            return fail("登录探测被风控/WAF 拒绝，需要更换代理或稍后重试")
        if methods["status"] == 429 or is_rate_limited(methods):
            log.warning("[%s] login methods rate limited, stop before signup", phone)
            return fail("登录探测被限频，未确认是新号，已停止注册以避免重复发 OTP")
        body_text = str(methods.get("body", "")).lower()
        if methods["status"] not in (401, 404) and "not_found" not in body_text and "not found" not in body_text:
            log.warning("[%s] login methods inconclusive: %s %s", phone, methods["status"], methods.get("body"))
            return fail(f"登录探测未确认是新号: {methods['status']} {str(methods.get('body', ''))[:300]}")

        # === Signup ===
        log.info("[%s] New number -> signup", phone)
        otp_result = client.signup_request_otp(phone, country_code=country_code)
        if otp_result["status"] not in (200, 201):
            msg = f"Signup OTP 申请失败: {otp_result['status']} {str(otp_result.get('body', ''))[:300]}"
            log.error("[%s] %s", phone, msg)
            body_text = str(otp_result.get("body", "")).lower()
            if otp_result["status"] == 429 or "ratelimit" in body_text or "rate_limit" in body_text:
                msg = f"注册 OTP 申请被限频，号码进入冷却: {str(otp_result.get('body', ''))[:300]}"
            return fail(msg)

        otp = wait_code("signup", max(1, signup_otp_timeout))
        if not otp:
            log.error("[%s] Signup OTP timeout", phone)
            return fail("注册 OTP 输入超时")
        log.info("[%s] Signup OTP: %s", phone, _mask_code(otp))
        note("注册 OTP 已提交，开始验证")

        time.sleep(2)
        verify = api_call_with_retry(client.signup_verify_otp, otp, phone)
        if verify["status"] not in (200, 201):
            log.error("[%s] Signup verify failed: %d", phone, verify["status"])
            return fail(f"注册 OTP 验证失败: {verify['status']} {str(verify.get('body', ''))[:300]}")
        note("注册 OTP 验证通过")

        time.sleep(2)
        names = [
            "Budi Santoso", "Adi Pratama", "Siti Rahayu", "Dewi Lestari",
            "Rizky Ramadhan", "Putri Wulandari", "Agus Setiawan", "Rina Kusuma",
            "Hendra Wijaya", "Novi Anggraini", "Dian Permata", "Wahyu Hidayat",
            "Fitri Handayani", "Joko Susilo", "Ratna Sari", "Bambang Prasetyo",
            "Mega Puspita", "Eko Nugroho", "Sari Indah", "Yusuf Maulana",
            "Lina Marlina", "Arief Rahman", "Wati Suryani", "Dedi Kurniawan",
            "Ayu Lestari", "Rudi Hartono", "Nisa Fitriani", "Bayu Anggara",
            "Sri Mulyani", "Fajar Setiadi", "Indra Gunawan", "Tika Rahmawati",
        ]
        signup = api_call_with_retry(client.signup_create_account,
                                     name=random.choice(names), phone=phone, email="", country=signed_up_country)
        signup_missing_token = False
        if signup["status"] not in (200, 201):
            err = get_error_code(signup)
            if _signup_created_without_token(signup):
                signup_missing_token = True
                log.warning("[%s] Signup returned 206: account created but token is empty", phone)
                note("创建账号返回 206：账号已创建但未下发 token，自动登录补 token")
            elif "phone_already_taken" not in err:
                log.error("[%s] Signup failed: %s", phone, signup["body"])
                return fail(f"创建账号失败: {signup['status']} {str(signup.get('body', ''))[:300]}")
        log.info("[%s] Signup success (uid=%s)", phone, client.user_uuid)

        # === Phase 2: Refresh ===
        if client.auth.refresh_token:
            note("创建账号接口完成，开始刷新 token")
            time.sleep(5)
            refresh = api_call_with_retry(client.refresh_token)
            if refresh["status"] in (200, 201):
                log.info("[%s] Token refreshed", phone)
                note("Token refresh 成功")
            elif client.auth.access_token:
                log.warning("[%s] Token refresh failed: %d; continuing with signup token", phone, refresh["status"])
                note(f"Token refresh 返回 {refresh['status']}，继续尝试创建账号接口返回的 token")
            else:
                log.error("[%s] Token refresh failed: %d", phone, refresh["status"])
                return fail(f"Token refresh 失败: {refresh['status']} {str(refresh.get('body', ''))[:300]}")
        elif client.auth.access_token:
            note("创建账号接口完成，未返回 refresh_token，继续尝试现有 token")
        elif signup_missing_token:
            note("注册账号已创建但未下发 token，开始登录补 token")

            def login_otp_callback() -> Optional[str]:
                if request_another_code:
                    request_another_code()
                note("登录 OTP 已发送，等待验证码")
                code = wait_code("login", 180)
                if code:
                    note("登录 OTP 已提交，开始验证")
                return code

            time.sleep(2)
            try:
                login_result = client.login(country_code, local, pin, login_otp_callback, note)
            except Exception as exc:
                log.exception("[%s] Login after 206 signup exception: %s", phone, exc)
                return fail(f"206 后登录补 token 异常: {exc}")
            if login_result["status"] not in (200, 201):
                if login_result["status"] == 429 or is_rate_limited(login_result):
                    return fail(f"206 后登录补 token 被限频: {str(login_result.get('body', ''))[:300]}")
                return fail(f"206 后登录补 token 失败: {login_result['status']} {str(login_result.get('body', ''))[:300]}")
            if not client.auth.access_token:
                return fail("206 后登录成功但仍未拿到 access_token")
            note("登录补 token 成功")
        else:
            return fail("创建账号接口未返回 access_token/refresh_token")

        # === Phase 3: GoPay Init ===
        time.sleep(2)
        api_call_with_retry(client.gopay_init)
        time.sleep(2)
        _run_real_device_pre_pin_warmup(client, note)
        time.sleep(2)
        profile = api_call_with_retry(client.get_user_profile)
        is_pin_set = profile["body"].get("data", {}).get("is_pin_setup", False) if profile["status"] == 200 else False

        if is_pin_set:
            log.info("[%s] PIN already set", phone)
        else:
            # === Phase 4: PIN Setup ===
            log.info("[%s] Setting PIN...", phone)
            if request_another_code:
                request_another_code()
            time.sleep(2)

            pin_allowed = api_call_with_retry(client.pin_check_allowed, pin)
            if pin_allowed["status"] not in (200, 201):
                return fail(f"PIN 可用性检查失败: {pin_allowed['status']} {str(pin_allowed.get('body', ''))[:300]}")

            pin_otp_r = api_call_with_retry(client.pin_request_otp)
            if pin_otp_r["status"] == 401 and client.auth.refresh_token:
                note("PIN OTP 申请 401，会话已失效，刷新 token 后重试")
                refresh = api_call_with_retry(client.refresh_token)
                if refresh["status"] in (200, 201):
                    time.sleep(2)
                    pin_otp_r = api_call_with_retry(client.pin_request_otp)
            if pin_otp_r["status"] not in (200, 201):
                log.error("[%s] PIN OTP request failed: %d", phone, pin_otp_r["status"])
                return fail(f"PIN OTP 申请失败: {pin_otp_r['status']} {str(pin_otp_r.get('body', ''))[:300]}")

            pin_verify = None
            max_pin_attempts = max(1, pin_otp_attempts)
            for pin_attempt in range(1, max_pin_attempts + 1):
                pin_timeout = pin_first_otp_timeout if pin_attempt == 1 else pin_retry_otp_timeout
                pin_code = wait_code("pin", max(1, pin_timeout))
                if not pin_code:
                    log.warning("[%s] PIN OTP timeout, resending... attempt=%d", phone, pin_attempt)
                    note(f"PIN OTP 输入超时，准备重新发送 ({pin_attempt}/{max_pin_attempts})")
                    if pin_attempt >= max_pin_attempts:
                        break
                    resend_body = {
                        "client_id": _GOJEK_CLIENT_ID,
                        "client_secret": _GOJEK_CLIENT_SECRET,
                        "flow": "goto_pin_wa_sms",
                        "verification_id": client.auth.verification_id,
                        "verification_method": "otp_sms",
                    }
                    time.sleep(2)
                    resend = client._sso_cvs_initiate(resend_body)
                    if resend["status"] in (200, 201):
                        inner = resend["body"].get("data", resend["body"])
                        client.auth.otp_token = inner.get("otp_token", "")
                        if request_another_code:
                            request_another_code()
                    continue

                log.info("[%s] PIN OTP: %s", phone, _mask_code(pin_code))
                note("PIN OTP 已提交，开始验证")

                time.sleep(2)
                pin_verify = api_call_with_retry(client.pin_verify_otp, pin_code)
                if pin_verify["status"] in (200, 201):
                    break

                log.error("[%s] PIN verify failed: %d", phone, pin_verify["status"])
                body_text = str(pin_verify.get("body", ""))[:300]
                if "otp_invalid" not in body_text or pin_attempt >= max_pin_attempts:
                    return fail(f"PIN OTP 验证失败: {pin_verify['status']} {body_text}")
                note(f"PIN OTP 不正确，重新发送新的 PIN OTP ({pin_attempt + 1}/{max_pin_attempts})")
                resend = api_call_with_retry(client.pin_request_otp)
                if resend["status"] not in (200, 201):
                    return fail(f"PIN OTP 重新发送失败: {resend['status']} {str(resend.get('body', ''))[:300]}")

            if not pin_verify or pin_verify["status"] not in (200, 201):
                return fail("PIN OTP 未验证通过")

            time.sleep(2)
            pin_result = api_call_with_retry(client.pin_setup, pin)
            if pin_result["status"] not in (200, 201):
                log.error("[%s] PIN setup failed: %d", phone, pin_result["status"])
                return fail(f"PIN 设置失败: {pin_result['status']} {str(pin_result.get('body', ''))[:300]}")
            log.info("[%s] PIN set OK", phone)
            note("PIN 设置完成")

        _run_post_pin_activation(client, phone, envelope_did, note)
        if relogin_after_register:
            note("PIN 后初始化完成，开始退出登录并重新登录更新 token")
            logout = client.logout()
            logout_status = int(logout.get("status", 0) or 0)
            if logout_status in (200, 201, 204):
                note(f"退出登录完成: {logout_status}")
            elif logout_status == 401:
                note("退出登录返回 401，会话可能已失效，继续重新登录更新 token")
            elif logout_status in (400, 500, 502, 503, 504):
                body_text = str(logout.get("body", ""))[:220]
                note(f"退出登录接口返回 {logout_status}，按服务端临时异常处理，继续重新登录更新 token: {body_text}")
            else:
                return fail(f"退出登录失败: {logout_status} {str(logout.get('body', ''))[:300]}")

            relogin = _login_one_manual_existing(
                phone=phone,
                pin=pin,
                proxy=proxy,
                wait_code=wait_code,
                country_code=country_code,
                status_cb=note,
                return_failure=True,
            )
            if not relogin or relogin.get("failed"):
                return fail(f"重新登录更新 token 失败: {relogin.get('error', '未知错误') if relogin else '无返回'}")
            client = relogin.get("client") or client
            note("重新登录完成，使用新 token 继续刷新余额")

        # Persist the usable account immediately after registration/PIN setup.
        # Balance rewards can arrive asynchronously; they must not block the
        # account from appearing in the GoPay account pool.
        _save_account(phone, local, pin, aid, client)
        note("GoPay 账号已写入账号池，余额继续后台刷新")

        if claim_envelope_after_register:
            _claim_configured_envelope(client, note)
            balance_after_envelope = _wait_post_pin_balance(client, note)
            if balance_after_envelope >= 0:
                note(f"节日红包后余额已刷新: {balance_after_envelope} Rp")
        else:
            time.sleep(1)
            balance = _wait_post_pin_balance(client, note)
            if balance >= 0:
                note(f"余额已刷新: {balance} Rp")
            else:
                note("余额刷新失败，worker 会继续轮询余额")

        if relogin_after_register:
            success = True
            return {
                "phone": phone,
                "aid": aid,
                "pin": pin,
                "client": client,
                "local": local,
                "relogged_in": True,
            }

        # Refresh the persisted balance/token state after the polling phase.
        _save_account(phone, local, pin, aid, client)

        success = True
        return {"phone": phone, "aid": aid, "pin": pin, "client": client, "local": local}

    except Exception as e:
        log.exception("[%s] Registration exception: %s", phone, e)
        if looks_like_network_timeout(e):
            return fail("注册异常: 代理连接 GoTo/GoPay 接口超时，已停止本次任务；请先确认代理出口稳定，等几分钟再试，避免触发限流")
        return fail(f"注册异常: {e}")
    finally:
        if not success and on_failure:
            on_failure()


# ---------------------------------------------------------------------------
# Register one GoPay account
# ---------------------------------------------------------------------------

def _register_one(
    api_key: str,
    pin: str,
    proxy: str,
    envelope_did: str,
    cancel_blocking: bool = False,
    status_cb: Optional[Callable[[str], None]] = None,
    signup_otp_timeout: int = 180,
    pin_first_otp_timeout: int = 60,
    pin_retry_otp_timeout: int = 180,
    pin_otp_attempts: int = 3,
) -> Optional[dict]:
    """Full registration flow: rent phone -> signup -> refresh -> PIN."""
    phone, aid = sms_get_number(api_key)
    if not phone:
        log.error("No phone number available")
        return None

    rented_at = time.time()

    cancel = (
        lambda: _blocking_cancel_phone(api_key, aid, _normalize_phone(phone), rented_at)
        if cancel_blocking
        else _deferred_cancel_phone(api_key, aid, _normalize_phone(phone), rented_at)
    )

    previous_otp = {"signup": ""}

    def wait_sms_code(purpose: str, timeout: int) -> Optional[str]:
        # Reusing one activation id is intentional: registration, PIN and
        # payment OTPs can share the same rented number.  After setStatus=3,
        # providers may replay the registration OTP; filter it before PIN/2FA.
        ignored = previous_otp["signup"] if purpose in {"pin", "login"} else ""
        code = sms_wait_code(api_key, aid, timeout=timeout, ignore_code=ignored)
        if purpose == "signup" and code:
            previous_otp["signup"] = code
        return code

    return _register_one_from_phone(
        phone=phone,
        aid=aid,
        pin=pin,
        proxy=proxy,
        envelope_did=envelope_did,
        wait_code=wait_sms_code,
        request_another_code=lambda: sms_request_another(api_key, aid),
        on_failure=cancel,
        status_cb=status_cb,
        signup_otp_timeout=signup_otp_timeout,
        pin_first_otp_timeout=pin_first_otp_timeout,
        pin_retry_otp_timeout=pin_retry_otp_timeout,
        pin_otp_attempts=pin_otp_attempts,
    )


def _register_one_manual(
    phone: str,
    pin: str,
    proxy: str,
    envelope_did: str,
    relogin_after_register: bool = False,
    claim_envelope_after_register: bool = False,
) -> Optional[dict]:
    """Full registration flow using a manually supplied phone and terminal OTP input."""
    normalized = _normalize_phone(phone)
    return _register_one_from_phone(
        phone=normalized,
        aid="manual",
        pin=pin,
        proxy=proxy,
        envelope_did=envelope_did,
        wait_code=lambda purpose, timeout: _prompt_code(normalized, purpose, timeout),
        return_existing=True,
        relogin_after_register=relogin_after_register,
        claim_envelope_after_register=claim_envelope_after_register,
    )


def _register_one_manual_live_country(
    phone: str,
    pin: str,
    proxy: str,
    envelope_did: str,
    country_code: str,
    signed_up_country: str,
    relogin_after_register: bool = False,
    claim_envelope_after_register: bool = False,
) -> Optional[dict]:
    """Manual registration flow that really calls GoPay with an explicit country code."""
    normalized = _normalize_phone_for_country(phone, country_code)
    return _register_one_from_phone(
        phone=normalized,
        aid="manual",
        pin=pin,
        proxy=proxy,
        envelope_did=envelope_did,
        wait_code=lambda purpose, timeout: _prompt_code(normalized, purpose, timeout),
        country_code=country_code,
        signed_up_country=signed_up_country,
        allow_unsupported_country=True,
        return_existing=True,
        relogin_after_register=relogin_after_register,
        claim_envelope_after_register=claim_envelope_after_register,
    )


def _login_one_manual_existing(
    phone: str,
    pin: str,
    proxy: str,
    wait_code: Callable[[str, int], Optional[str]],
    country_code: str = "+62",
    status_cb: Optional[Callable[[str], None]] = None,
    return_failure: bool = False,
) -> Optional[dict]:
    """Login an existing GoPay account with PIN + manually supplied OTP."""
    def note(message: str) -> None:
        if status_cb:
            try:
                status_cb(message)
            except Exception:
                pass

    def fail(message: str) -> Optional[dict]:
        note(message)
        if return_failure:
            return {"failed": True, "phone": normalized, "local": local if "local" in locals() else "", "error": message}
        return None

    country_code = country_code if country_code.startswith("+") else f"+{country_code}"
    normalized = _normalize_phone_for_country(phone, country_code)
    if not normalized:
        return fail("手机号为空或格式不支持")
    country_digits = country_code.lstrip("+")
    local = normalized.lstrip("+")
    if local.startswith(country_digits):
        local = local[len(country_digits):]

    proxy_error = _preflight_proxy(proxy, note)
    if proxy_error:
        log.warning("[%s] %s", normalized, proxy_error)
        return fail(proxy_error)

    client = GojekClient.from_phone(normalized, proxy=proxy)
    note("开始已有账号登录：PIN + OTP")

    def otp_callback() -> Optional[str]:
        note("登录 OTP 已发送，等待输入")
        code = wait_code("login", 180)
        if code:
            note("登录 OTP 已提交，开始验证")
        return code

    try:
        result = client.login(country_code, local, pin, otp_callback, note)
    except Exception as exc:
        log.exception("[%s] Login exception: %s", normalized, exc)
        if looks_like_network_timeout(exc):
            return fail("已有账号登录异常: 代理连接 GoTo/GoPay 接口超时，已停止本次任务；请先确认代理出口稳定，等几分钟再试")
        return fail(f"已有账号登录异常: {exc}")
    if result["status"] not in (200, 201):
        if result["status"] == 429 or is_rate_limited(result):
            return fail(f"已有账号登录被限频: {str(result.get('body', ''))[:300]}")
        return fail(f"已有账号登录失败: {result['status']} {str(result.get('body', ''))[:300]}")

    note("已有账号登录成功，保存账号")
    _save_account(normalized, local, pin, "manual-login", client)
    return {
        "phone": normalized,
        "aid": "manual-login",
        "pin": pin,
        "client": client,
        "local": local,
        "logged_in_existing": True,
    }


# ---------------------------------------------------------------------------
# Job handling
# ---------------------------------------------------------------------------

def _job_remaining_sec(job: dict) -> float:
    expires = job.get("expires_at", "")
    if not expires:
        return 3600
    try:
        exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return (exp - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 3600


def _get_envelope_did() -> str:
    # External envelope links are intentionally disabled. We keep this function
    # for CLI/backward-compatible call sites, but it always returns empty so the
    # post-PIN flow only relies on GoPay's own system activation reward.
    return ""


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

def _pay_job(job: dict, account: dict, inbox_client, api_key: str, pin: str, proxy: str = "") -> tuple[bool, str]:
    job_id = job["id"]
    midtrans_url = job.get("provider_url") or job.get("paypal_url") or ""
    phone = account["local"]
    log.info("[job:%s] Paying with %s (protocol)", job_id[:8], account["phone"])

    try:
        payment_profile = ensure_account_payment_fingerprint(account)
        log.info("[job:%s] payment profile_id=%s", job_id[:8], payment_profile.get("profile_id", ""))
        payment = GoPayPayment(proxy=proxy, payment_fingerprint=payment_profile)

        def wait_otp(ph: str, timeout: int = 120) -> Optional[str]:
            try:
                sms_api(api_key, "setStatus", {"id": account["aid"], "status": "3"})
            except Exception:
                pass
            time.sleep(2)
            return sms_wait_code(api_key, account["aid"], timeout=timeout)

        result = payment.pay(
            midtrans_url=midtrans_url,
            phone=phone,
            country_code="62",
            pin=pin,
            wait_otp=wait_otp,
        )

        detail = result.get("detail", "")
        if result.get("success"):
            log.info("[job:%s] Payment SUCCESS!", job_id[:8])
            try:
                inbox_client._req("PUT", f"/api/jobs/{job_id}/paid")
            except Exception as e:
                log.error("[job:%s] Mark paid failed: %s", job_id[:8], e)
            return True, detail
        else:
            log.warning("[job:%s] Payment failed: %s", job_id[:8], detail)
            try:
                inbox_client._req("PUT", f"/api/jobs/{job_id}/cancel")
            except Exception:
                pass
            return False, detail

    except GoPayFraudDenyError as e:
        log.warning("[job:%s] FRAUD DENIED: %s", job_id[:8], e)
        try:
            inbox_client._req("PUT", f"/api/jobs/{job_id}/cancel")
        except Exception:
            pass
        return False, "fraud_deny -- phone burned"

    except Exception as e:
        log.exception("[job:%s] Payment exception: %s", job_id[:8], e)
        try:
            inbox_client._req("PUT", f"/api/jobs/{job_id}/cancel")
        except Exception:
            pass
        return False, str(e)


def _claim_job(inbox, min_remaining: float = MIN_REMAINING_SEC) -> Optional[dict]:
    try:
        job = inbox._req("POST", "/api/jobs/claim_next", data={
            "prefer_paypal_url": False, "prefer_oldest": True, "provider": "gopay",
        })
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            log.warning("Inbox poll error: %s", e)
        return None
    except Exception as e:
        log.warning("Inbox poll error: %s", e)
        return None

    if job is None:
        return None

    url = job.get("provider_url") or job.get("paypal_url") or ""
    if "midtrans" not in url:
        return None

    remaining = _job_remaining_sec(job)
    if remaining < min_remaining:
        log.info("Job %s: %.0fs left < %ds, cancelling", job["id"][:8], remaining, min_remaining)
        try:
            inbox._req("PUT", f"/api/jobs/{job['id']}/cancel")
        except Exception:
            pass
        return None

    return job


# ---------------------------------------------------------------------------
# Phone reactivation
# ---------------------------------------------------------------------------

_PHONE_LIFETIME = 1080


def _sms_reactivate(api_key: str, activation_id: str) -> Optional[str]:
    try:
        s = tls_client.Session(client_identifier="chrome_120")
        r = s.post(f"{sms_api_base_url()}/stubs/handler_api.php", params={
            "api_key": api_key, "action": "reactivate", "id": activation_id,
        }, timeout_seconds=15)
        log.info("[reactivate] aid=%s -> %d: %s", activation_id, r.status_code, r.text[:200])
        if r.status_code == 200:
            data = r.json()
            new_aid = str(data.get("activationId", ""))
            if new_aid:
                return new_aid
        return None
    except Exception as e:
        log.warning("[reactivate] aid=%s failed: %s", activation_id, e)
        return None


def _resume_account(phone: str, proxy: str = "") -> Optional[dict]:
    if not os.path.exists(ACCOUNTS_FILE):
        log.error("[resume] %s not found", ACCOUNTS_FILE)
        return None
    accounts = json.loads(open(ACCOUNTS_FILE, encoding="utf-8").read())
    digits = phone.strip().lstrip("+")
    entry = None
    entry_idx = -1
    for i, a in enumerate(accounts):
        a_digits = a["phone"].strip().lstrip("+")
        a_local = a.get("local", "")
        if a_digits == digits or (a_local and a_local == digits) or (a_local and digits.endswith(a_local)):
            entry = a
            entry_idx = i
            break
    if not entry:
        log.error("[resume] phone %s not found in %s", phone, ACCOUNTS_FILE)
        return None

    if not proxy:
        proxy = _make_proxy()
    client = GojekClient.from_phone(entry["phone"], proxy=proxy)
    client.auth.access_token = entry["access_token"]
    client.auth.refresh_token = entry["refresh_token"]
    client.user_uuid = entry.get("customer_id", "")
    if entry.get("device_uniqueid"):
        client.uniqueid = entry.get("device_uniqueid", "")
    if entry.get("device_session_id"):
        client.session_id = entry.get("device_session_id", "")
    if entry.get("device_token"):
        client.device_token = entry.get("device_token", "")
    payment_profile = ensure_account_payment_fingerprint(entry)
    with _accounts_lock:
        if 0 <= entry_idx < len(accounts):
            accounts[entry_idx] = entry
        open(ACCOUNTS_FILE, "w", encoding="utf-8").write(json.dumps(accounts, indent=2, ensure_ascii=False))

    log.info("[resume] Refreshing token for %s...", entry["phone"])
    try:
        r = client.refresh_token()
        if r["status"] in (200, 201):
            log.info("[resume] Token refreshed OK for %s", entry["phone"])
        else:
            log.warning("[resume] Token refresh returned %d, trying with existing token", r["status"])
    except Exception as e:
        log.warning("[resume] Token refresh failed: %s, trying with existing token", e)

    return {
        "phone": entry["phone"],
        "client": client,
        "aid": entry.get("activation_id", ""),
        "pin": entry.get("pin", DEFAULT_PIN),
        "local": entry.get("local", ""),
        "payment_fingerprint": payment_profile,
        "resumed": True,
    }


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _worker_loop(
    inbox, api_key: str, pin: str, stop: threading.Event,
    worker_id: int,
    resume_phone: str = "",
):
    tag = f"[w{worker_id}]"
    envelope_did = _get_envelope_did()

    while not stop.is_set():
        # === Register or resume ===
        if resume_phone:
            log.info("%s Resuming account %s...", tag, resume_phone)
            proxy = _make_proxy()
            account = _resume_account(resume_phone, proxy)
            resume_phone = ""
        else:
            new_did = _get_envelope_did()
            if new_did:
                envelope_did = new_did
            log.info("%s Registering new GoPay account...", tag)
            proxy = _make_proxy()
            account = _register_one(api_key, pin, proxy, envelope_did)

        if not account:
            log.warning("%s Registration/resume failed, retry in 10s", tag)
            stop.wait(10)
            continue

        phone = account["phone"]
        client = account["client"]
        aid = account["aid"]
        is_resumed = account.get("resumed", False)
        register_time = 0 if is_resumed else time.time()
        log.info("%s Account ready: %s%s", tag, phone, " (resumed)" if is_resumed else "")

        # === Wait for balance >= MIN_BALANCE_RP ===
        balance_ok = False
        max_wait = 3600
        wait_start = time.time()
        phone_activated_at = register_time
        reactivate_count = 0
        max_reactivates = 3
        while not stop.is_set():
            if time.time() - wait_start > max_wait:
                log.warning("%s Waited %ds for balance, giving up", tag, max_wait)
                break

            phone_age = time.time() - phone_activated_at
            if phone_age > _PHONE_LIFETIME - 120:
                if reactivate_count < max_reactivates:
                    log.info("%s Phone expiring during balance wait, reactivating (%d/%d)...",
                             tag, reactivate_count + 1, max_reactivates)
                    new_aid = _sms_reactivate(api_key, aid)
                    if new_aid:
                        aid = new_aid
                        account["aid"] = new_aid
                        phone_activated_at = time.time()
                        reactivate_count += 1
                    else:
                        log.warning("%s Reactivate failed during balance wait, phone may be lost", tag)
                        reactivate_count += 1

            bal = _check_balance(client)
            if bal >= MIN_BALANCE_RP:
                log.info("%s Balance=%d Rp (>=%d), ready!", tag, bal, MIN_BALANCE_RP)
                _update_account_balance(phone, bal, client)
                _inbox_delete_account(phone)
                balance_ok = True
                break
            elif bal >= 0:
                waited = int(time.time() - wait_start)
                log.info("%s Balance=%d Rp (need >=%d), waiting 15s... (%ds elapsed)", tag, bal, MIN_BALANCE_RP, waited)
                stop.wait(15)
            else:
                log.warning("%s Balance check failed, trying token refresh", tag)
                try:
                    client.refresh_token()
                except Exception:
                    pass
                stop.wait(30)

        if not balance_ok:
            log.info("%s No balance after waiting, registering new account", tag)
            continue

        # === Payment loop ===
        while not stop.is_set():
            phone_age = time.time() - phone_activated_at
            if phone_age > _PHONE_LIFETIME - 120:
                if reactivate_count >= max_reactivates:
                    log.info("%s Max reactivates (%d) reached, retiring phone", tag, max_reactivates)
                    break
                log.info("%s Phone expiring, reactivating (%d/%d)...", tag, reactivate_count + 1, max_reactivates)
                new_aid = _sms_reactivate(api_key, aid)
                if new_aid:
                    aid = new_aid
                    account["aid"] = new_aid
                    phone_activated_at = time.time()
                    reactivate_count += 1
                    log.info("%s Reactivated, new aid=%s", tag, new_aid)
                else:
                    log.warning("%s Reactivate failed, retiring phone", tag)
                    break

            job = _claim_job(inbox)
            if not job:
                stop.wait(POLL_INTERVAL)
                continue

            remaining = _job_remaining_sec(job)
            phone_left = _PHONE_LIFETIME - (time.time() - phone_activated_at)
            log.info("%s Job %s -> %s (job %.0fs, phone %.0fs)",
                     tag, job["id"][:8], phone, remaining, phone_left)

            success, detail = _pay_job(job, account, inbox, api_key, pin, proxy=proxy)
            if success:
                log.info("%s Job %s paid!", tag, job["id"][:8])
                break

            if "fraud_deny" in detail.lower() or "fraud denied" in detail.lower() or "burned" in detail.lower():
                log.warning("%s FRAUD DENIED, retiring phone", tag)
                break

            if "already linked" in detail.lower():
                log.warning("%s Already linked, retiring phone", tag)
                break

            log.warning("%s Job %s failed (%s), next job", tag, job["id"][:8], detail[:60])

        # === Release phone ===
        try:
            sms_done(api_key, aid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_worker(
    max_workers: int = 3,
    pin: str = DEFAULT_PIN,
    poll_interval: float = POLL_INTERVAL,
    resume_phones: Optional[list] = None,
    api_key: str = "",
):
    from .payment_inbox import PaymentInboxClient

    api_key = get_sms_api_key(api_key)
    if not api_key:
        log.error("No SMS API key. Set OPAI_SMSBOWER_API_KEY, OPAI_SMSBOWER_API_KEY_FILE, or OPAI_GOPAY_SMS_ENV_FILE")
        return

    inbox = PaymentInboxClient(base_url=INBOX_URL, basic_auth=(INBOX_USER, INBOX_PASS))
    stop = threading.Event()

    resume_phones = resume_phones or []
    actual_workers = max(max_workers, len(resume_phones))
    log.info("Worker started: workers=%d poll=%.0fs resume=%s ttl=%ds",
             actual_workers, poll_interval, resume_phones or "(none)", GOPAY_ACCOUNT_TTL)
    _inbox_ttl_cleanup()

    threads = []
    for i in range(actual_workers):
        rp = resume_phones[i] if i < len(resume_phones) else ""
        t = threading.Thread(
            target=_worker_loop,
            args=(inbox, api_key, pin, stop, i),
            kwargs={"resume_phone": rp},
            daemon=True, name=f"w{i}",
        )
        t.start()
        threads.append(t)
        time.sleep(2)

    try:
        while True:
            alive = sum(1 for t in threads if t.is_alive())
            if alive == 0:
                log.error("All workers dead, exiting")
                break
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Shutting down")
        stop.set()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GoPay Protocol Worker")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--pin", default=DEFAULT_PIN)
    parser.add_argument("--poll", type=float, default=POLL_INTERVAL)
    parser.add_argument("--api-key", default="", help="SMSBower API key (or set OPAI_SMSBOWER_API_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="Register one account only, no inbox")
    parser.add_argument("--resume", nargs="+", metavar="PHONE", help="Resume from existing accounts")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    if args.dry_run:
        log.info("=== DRY RUN: register one account ===")
        api_key = get_sms_api_key(args.api_key)
        if not api_key:
            log.error("No API key")
            return
        proxy = _make_proxy()
        envelope_did = _get_envelope_did()
        result = _register_one(api_key, args.pin, proxy, envelope_did)
        if result:
            log.info("SUCCESS: %s pin=%s", result["phone"], args.pin)
            sms_done(api_key, result["aid"])
        else:
            log.error("FAILED")
        return

    run_worker(max_workers=args.workers, pin=args.pin, poll_interval=args.poll,
               resume_phones=args.resume, api_key=args.api_key)


if __name__ == "__main__":
    main()
