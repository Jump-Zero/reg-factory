"""
GoPay Pure-Protocol Payment — 不需要浏览器。

完整 Midtrans GoPay 支付流程：
  Phase A: Linking（绑定 GoPay）
    1. POST /snap/v3/accounts/{snap}/linking      → reference
    2. POST /v1/linking/validate-reference         → 验证
    3. POST /v1/linking/user-consent               → 同意
    4. POST /v1/linking/resend-otp                 → 强制 SMS OTP
    5. POST /v1/linking/validate-otp               → 验证 OTP → challenge_id
    6. POST /api/v1/users/pin/tokens/nb            → PIN → pin_token (MGUPA)
    7. POST /v1/linking/validate-pin               → 提交 pin_token

  Phase B: Charge（扣款）
    8. GET  /snap/v3/accounts/{snap}/gopay         → 轮询直到 linked
    9. POST /snap/v2/transactions/{snap}/charge    → 扣款 → challenge reference

  Phase C: Challenge（支付确认）
    10. GET  /v1/payment/validate                  → 验证支付
    11. POST /v1/payment/confirm                   → 确认
    12. POST /api/v1/users/pin/tokens/nb           → PIN (GWC)
    13. POST /v1/payment/process                   → 最终处理

  Phase D: 验证
    14. GET  /snap/v1/transactions/{snap}/status   → 交易状态

来源：HAR 抓包 chatgpt.com.free.plus.gopay.har (2026-05-01)
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Optional, Callable

import tls_client

from .payment_fingerprint import normalize_payment_fingerprint, payment_fingerprint_headers

log = logging.getLogger(__name__)

MIDTRANS_BASE = "https://app.midtrans.com"
GWA_BASE = "https://gwa.gopayapi.com"
CUSTOMER_BASE = "https://customer.gopayapi.com"

PIN_CLIENT_LINKING = "51b5f09a-3813-11ee-be56-0242ac120002-MGUPA"
PIN_CLIENT_PAYMENT = "47180a8e-f56e-11ed-a05b-0242ac120003-GWC"

LINK_RETRY_LIMIT = 2
LINK_RETRY_SLEEP_S = 12.0
SNAP_SIGNING_KEY = os.environ.get("OPAI_MIDTRANS_SNAP_SIGNING_KEY", "1feab063-bf3f-4025-90bf-3be6fa4f4cc2")


def _snap_signature_hex(message: str) -> str:
    digest = hmac.new(SNAP_SIGNING_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
    # Midtrans' current Snap bundle emits the sha256 hmac hex with adjacent
    # bytes swapped. Mirror that browser output so the API accepts protocol calls.
    return "".join(digest[i + 2:i + 4] + digest[i:i + 2] for i in range(0, len(digest), 4))


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


class GoPayPaymentError(Exception):
    pass


class GoPayFraudDenyError(GoPayPaymentError):
    pass


class GoPayPayment:
    """纯协议 GoPay 支付。"""

    def __init__(self, proxy: str = "", payment_fingerprint: Optional[dict] = None):
        self._session = tls_client.Session(client_identifier="chrome_120")
        proxy = _normalize_proxy_url(proxy)
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
        self.payment_fingerprint = normalize_payment_fingerprint(payment_fingerprint)
        self._headers = payment_fingerprint_headers(self.payment_fingerprint)
        self._fingerprint_expectations = {
            key: self._headers.get(key, "")
            for key in (
                "User-Agent",
                "Accept-Language",
                "Sec-CH-UA",
                "Sec-CH-UA-Mobile",
                "Sec-CH-UA-Platform",
                "X-Timezone",
                "Viewport-Width",
            )
        }

    @property
    def profile_id(self) -> str:
        return str(self.payment_fingerprint.get("profile_id") or "")

    def _request_headers(self, extra: Optional[dict] = None) -> dict:
        headers = {**self._headers}
        if extra:
            headers.update(extra)
        self._assert_fingerprint_headers(headers)
        return headers

    def _snap_signature_headers(self, path: str, body: Optional[dict] = None) -> dict:
        timestamp = str(int(time.time()))
        if isinstance(body, dict):
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        else:
            payload = body or ""
        message = f"{path}:{timestamp}:{payload}"
        signature = _snap_signature_hex(message)
        return {
            "X-Snap-Signature": signature,
            "X-Timestamp": timestamp,
            "X-Source": "snap",
            "X-Source-App-Type": "redirection",
            "X-Source-Version": "2.3.0",
        }

    def _assert_fingerprint_headers(self, headers: dict) -> None:
        for key, expected in self._fingerprint_expectations.items():
            if expected and headers.get(key) != expected:
                raise GoPayPaymentError(
                    f"payment fingerprint drift: {key} expected={expected!r} got={headers.get(key)!r}"
                )

    @staticmethod
    def _extract_challenge_id(body: dict) -> str:
        """从响应里递归找 challenge_id，兼容多种嵌套格式。"""
        if not isinstance(body, dict):
            return ""
        for key in ("challenge_id",):
            if body.get(key):
                return str(body[key])
        for key in ("data", "challenge", "action", "value"):
            nested = body.get(key)
            if isinstance(nested, dict):
                found = GoPayPayment._extract_challenge_id(nested)
                if found:
                    return found
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        found = GoPayPayment._extract_challenge_id(item)
                        if found:
                            return found
        return ""

    def _midtrans_get(self, path: str, timeout: int = 15, auth_snap: str = "") -> dict:
        url = f"{MIDTRANS_BASE}{path}"
        extra = self._snap_signature_headers(path)
        if auth_snap:
            extra["Authorization"] = "Basic " + base64.b64encode(f"{auth_snap}:".encode()).decode()
        r = self._session.get(url, headers=self._request_headers(extra), timeout_seconds=timeout)
        log.debug("[MT GET] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _midtrans_post(self, path: str, body: dict, timeout: int = 15, auth_snap: str = "") -> dict:
        url = f"{MIDTRANS_BASE}{path}"
        extra = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._snap_signature_headers(path, body),
        }
        if auth_snap:
            extra["Authorization"] = "Basic " + base64.b64encode(f"{auth_snap}:".encode()).decode()
        r = self._session.post(
            url,
            headers=self._request_headers(extra),
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
            timeout_seconds=timeout,
        )
        log.debug("[MT POST] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _midtrans_delete(self, path: str, timeout: int = 15) -> dict:
        url = f"{MIDTRANS_BASE}{path}"
        r = self._session.delete(url, headers=self._request_headers(self._snap_signature_headers(path)), timeout_seconds=timeout)
        log.debug("[MT DELETE] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _gwa_post(self, path: str, body: dict, timeout: int = 15) -> dict:
        url = f"{GWA_BASE}{path}"
        headers = self._request_headers({
            "Origin": "https://merchants-gws-app.gopayapi.com",
            "Referer": "https://merchants-gws-app.gopayapi.com/",
        })
        r = self._session.post(url, headers=headers, data=json.dumps(body), timeout_seconds=timeout)
        log.debug("[GWA POST] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _gwa_get(self, path: str, timeout: int = 15) -> dict:
        url = f"{GWA_BASE}{path}"
        headers = self._request_headers({
            "Origin": "https://merchants-gws-app.gopayapi.com",
            "Referer": "https://merchants-gws-app.gopayapi.com/",
        })
        r = self._session.get(url, headers=headers, timeout_seconds=timeout)
        log.debug("[GWA GET] %s → %d", path, r.status_code)
        try:
            return {"status": r.status_code, "body": r.json()}
        except Exception:
            return {"status": r.status_code, "body": {"raw": r.text[:500]}}

    def _pin_verify(self, challenge_id: str, pin: str, client_id: str) -> str:
        """POST /api/v1/users/pin/tokens/nb → 返回 pin_token (JWT)。"""
        url = f"{CUSTOMER_BASE}/api/v1/users/pin/tokens/nb"
        body = {"challenge_id": challenge_id, "client_id": client_id, "pin": pin}
        headers = self._request_headers({
            "Origin": "https://pin-web-client.gopayapi.com",
            "Referer": "https://pin-web-client.gopayapi.com/",
        })
        r = self._session.post(url, headers=headers, data=json.dumps(body), timeout_seconds=15)
        log.debug("[PIN] challenge=%s client=%s → %d", challenge_id[:12], client_id[-6:], r.status_code)
        if r.status_code != 200:
            raise GoPayPaymentError(f"PIN verify failed: {r.status_code} {r.text[:200]}")
        try:
            data = r.json()
            token = data.get("data", {}).get("token", "")
            if not token:
                token = data.get("token", "")
            return token
        except Exception:
            raise GoPayPaymentError(f"PIN verify parse error: {r.text[:200]}")

    def pay(
        self,
        midtrans_url: str,
        phone: str,
        country_code: str,
        pin: str,
        wait_otp: Callable[[str, int], Optional[str]] = None,
        progress: Callable[[str], None] | None = None,
        midtrans_client_key: str = "",
    ) -> dict:
        """
        执行完整的 GoPay 支付流程。

        Args:
            midtrans_url: Midtrans snap redirect URL
            phone: 手机号（不含国际码，如 85142447768）
            country_code: 国际码（如 62）
            pin: 6 位 GoPay PIN
            wait_otp: 等待 OTP 的回调函数 (phone, timeout) → code or None

        Returns:
            {"success": bool, "detail": str, "transaction_status": str}
        """
        def note(message: str) -> None:
            log.info("[pay] %s", message)
            if progress:
                try:
                    progress(message)
                except Exception:
                    log.debug("payment progress callback failed", exc_info=True)

        # 提取 snap token
        m = re.search(r"/snap/v[34]/redirection/([0-9a-f-]{36})", midtrans_url)
        if not m:
            return {"success": False, "detail": "invalid midtrans URL"}
        snap = m.group(1)
        log.info("[pay] snap=%s phone=%s%s profile_id=%s", snap[:12], country_code, phone, self.profile_id)

        # === Phase A: Linking ===

        # Step 1: linking
        note("Step 1: linking")
        link_body = {
            "type": "gopay",
            "country_code": country_code,
            "phone_number": phone,
        }
        link_r = {}
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            link_r = self._midtrans_post(
                f"/snap/v3/accounts/{snap}/linking",
                link_body,
                auth_snap=midtrans_client_key or snap,
            )
            if link_r["status"] in (200, 201):
                break
            if link_r["status"] == 406:
                body_text = json.dumps(link_r.get("body", {}), ensure_ascii=False)[:300]
                if attempt <= LINK_RETRY_LIMIT:
                    log.info(
                        "[pay] linking 406/pending linked state (%s), sleep %.0fs retry %d/%d",
                        body_text, LINK_RETRY_SLEEP_S, attempt, LINK_RETRY_LIMIT,
                    )
                    time.sleep(LINK_RETRY_SLEEP_S)
                    continue
                return {
                    "success": False,
                    "detail": (
                        "Midtrans 链接已有未完成的 GoPay 绑定状态，不能重复绑定；"
                        "请重新用 AT 生成一条新的 Midtrans 链接后再支付"
                    ),
                }
            if link_r["status"] == 429:
                if attempt <= LINK_RETRY_LIMIT:
                    log.info(
                        "[pay] linking 429 rate limited, sleep %.0fs retry %d/%d",
                        LINK_RETRY_SLEEP_S, attempt, LINK_RETRY_LIMIT,
                    )
                    time.sleep(LINK_RETRY_SLEEP_S)
                    continue
                return {"success": False, "detail": "linking 429 rate limited，请换新 Midtrans 链接或稍后重试"}
            break
        if link_r["status"] not in (200, 201):
            return {"success": False, "detail": f"linking failed: {link_r['status']} {str(link_r.get('body', ''))[:200]}"}

        # 从 response 提取 reference
        body = link_r["body"]
        act_url = body.get("activation_link_url", "")
        ref_m = re.search(r"reference=([0-9a-f-]{36})", act_url)
        if not ref_m:
            return {"success": False, "detail": f"no reference in linking response: {str(body)[:200]}"}
        reference = ref_m.group(1)
        note(f"linking reference={reference}")

        time.sleep(1)

        # Step 2: validate-reference
        note("Step 2: validate-reference")
        vr = self._gwa_post("/v1/linking/validate-reference", {"reference_id": reference})
        if vr["status"] != 200:
            return {"success": False, "detail": f"validate-reference failed: {vr['status']}"}

        time.sleep(1)

        # Step 3: user-consent
        note("Step 3: user-consent")
        uc = self._gwa_post("/v1/linking/user-consent", {"reference_id": reference})
        if uc["status"] != 200:
            return {"success": False, "detail": f"user-consent failed: {uc['status']}"}

        time.sleep(1)

        # Step 4: resend-otp (强制 SMS)
        note("Step 4: resend-otp (force SMS)")
        resend = self._gwa_post("/v1/linking/resend-otp", {
            "reference_id": reference,
            "otp_channel": "SMS",
        })
        log.info("[pay] resend-otp: %d", resend["status"])

        # 等待 OTP
        if not wait_otp:
            return {"success": False, "detail": "no OTP callback provided"}
        full_phone = f"+{country_code}{phone}"
        note(f"Waiting for OTP on {full_phone}")
        otp_code = wait_otp(full_phone, 120)
        if not otp_code:
            return {"success": False, "detail": "OTP timeout"}
        note("OTP received, validating")

        time.sleep(1)

        # Step 5: validate-otp
        note("Step 5: validate-otp")
        vo = self._gwa_post("/v1/linking/validate-otp", {
            "reference_id": reference,
            "otp": otp_code,
        })
        if vo["status"] != 200:
            return {"success": False, "detail": f"validate-otp failed: {vo['status']} {str(vo['body'])[:200]}"}

        # 提取 challenge_id
        vo_body = vo.get("body", {})
        log.info("[pay] validate-otp response: %s", json.dumps(vo_body, ensure_ascii=False)[:500])

        # 尝试多种路径提取 challenge_id
        challenge_id = ""
        if isinstance(vo_body, dict):
            challenge_id = (vo_body.get("challenge_id", "")
                          or vo_body.get("data", {}).get("challenge_id", ""))
            # 可能在 redirect_url / pin_url 里
            for key in ("redirect_url", "pin_url", "url", "callback_url"):
                url_val = vo_body.get(key, "") or vo_body.get("data", {}).get(key, "")
                if url_val:
                    m = re.search(r"challengeId=([0-9a-f-]{36})", url_val)
                    if m:
                        challenge_id = m.group(1)
                        break
        # 如果还没有，尝试从整个 response 文本里搜
        if not challenge_id:
            body_str = json.dumps(vo_body, ensure_ascii=False)
            m = re.search(r"[Cc]hallenge[_]?[Ii]d[\"':=\s]+([0-9a-f-]{36})", body_str)
            if m:
                challenge_id = m.group(1)
        if not challenge_id:
            log.error("[pay] No challenge_id found in validate-otp response")
            return {"success": False, "detail": f"no challenge_id: {json.dumps(vo_body, ensure_ascii=False)[:300]}"}

        note(f"linking challenge_id={challenge_id[:16]}")
        time.sleep(1)

        # Step 6: PIN verify (linking)
        note("Step 6: PIN verify (MGUPA)")
        pin_token = self._pin_verify(challenge_id, pin, PIN_CLIENT_LINKING)
        log.info("[pay] pin_token=%s...", pin_token[:30])

        time.sleep(1)

        # Step 7: validate-pin
        note("Step 7: validate-pin")
        vp = self._gwa_post("/v1/linking/validate-pin", {
            "reference_id": reference,
            "token": pin_token,
        })
        if vp["status"] != 200:
            return {"success": False, "detail": f"validate-pin failed: {vp['status']}"}
        note("Linking complete")

        # === Phase B: Charge ===

        # Step 8: poll gopay status
        note("Step 8: poll gopay linked status")
        for _ in range(10):
            time.sleep(2)
            gs = self._midtrans_get(f"/snap/v3/accounts/{snap}/gopay")
            if gs["status"] == 200:
                acct_status = gs["body"].get("account_status", "")
                if acct_status == "ENABLED" or "linked" in str(gs["body"]).lower():
                    note(f"GoPay linked: {acct_status}")
                    break
        else:
            return {"success": False, "detail": "gopay not linked after polling"}

        time.sleep(1)

        # Step 9: charge
        note("Step 9: charge")
        charge = self._midtrans_post(f"/snap/v2/transactions/{snap}/charge", {
            "payment_type": "gopay",
            "tokenization": "true",
            "promo_details": None,
        })
        charge_body = charge["body"]
        charge_json = json.dumps(charge_body, ensure_ascii=False)
        log.info("[pay] charge response: %s", charge_json[:1000])

        # fraud check（HTTP 可能是 200 但 body 里 status_code=202 + fraud_status=deny）
        body_status = str(charge_body.get("status_code", ""))
        fraud = charge_body.get("fraud_status", "")
        txn_status = charge_body.get("transaction_status", "")
        if fraud == "deny" or txn_status == "deny":
            raise GoPayFraudDenyError(f"FRAUD DENIED: {charge_json[:300]}")
        if charge["status"] not in (200, 201) and body_status not in ("200", "201"):
            return {"success": False, "detail": f"charge failed: HTTP {charge['status']} body_status={body_status}"}

        # charge 直接 settlement（无需 challenge）
        if txn_status in ("settlement", "capture"):
            note("charge already settled, no challenge needed")
            return {"success": True, "detail": "payment completed (direct settlement)", "transaction_status": txn_status}

        challenge_ref = ""
        actions = charge_body.get("actions") or []
        for act in actions:
            u = act.get("url") or ""
            ref_m2 = re.search(r"reference=([A-Za-z0-9]+)", u)
            if ref_m2:
                challenge_ref = ref_m2.group(1)
                break
        if not challenge_ref:
            for key in ("gopay_verification_link_url", "redirect_url", "url", "deeplink_url"):
                u = charge_body.get(key) or ""
                ref_m2 = re.search(r"reference=([A-Za-z0-9]+)", u)
                if ref_m2:
                    challenge_ref = ref_m2.group(1)
                    break
        if not challenge_ref:
            log.warning("[pay] no challenge ref, charge_body keys: %s", list(charge_body.keys()))
            return {"success": False, "detail": f"no challenge ref in charge response: {charge_json[:400]}"}
        note(f"charge challenge_ref={challenge_ref}")

        # === Phase C: Challenge ===

        # HAR 里在 validate 之前先访问了 challenge 页面（可能设 cookie/session）
        verification_url = charge_body.get("gopay_verification_link_url") or ""
        if verification_url:
            log.info("[pay] GET challenge page: %s", verification_url[:120])
            try:
                vr = self._session.get(verification_url, headers=self._request_headers({
                    "Referer": "https://app.midtrans.com/",
                }), timeout_seconds=15)
                log.info("[pay] challenge page: %d (%d bytes)", vr.status_code, len(vr.text))
            except Exception as e:
                log.warning("[pay] challenge page fetch failed: %s", e)

        time.sleep(1)

        # Step 10: payment validate
        note("Step 10: payment validate")
        pv = self._gwa_get(f"/v1/payment/validate?reference_id={challenge_ref}")
        log.info("[pay] validate response: %d %s", pv["status"], json.dumps(pv.get("body", {}), ensure_ascii=False)[:800])
        if pv["status"] != 200:
            return {"success": False, "detail": f"payment validate failed: {pv['status']}"}

        # 提取支付阶段的 challenge_id（可能嵌套在多层结构里）
        pv_body = pv.get("body", {})
        pay_challenge_id = self._extract_challenge_id(pv_body)

        time.sleep(1)

        # Step 11: payment confirm
        note("Step 11: payment confirm")
        pc = self._gwa_post(f"/v1/payment/confirm?reference_id={challenge_ref}", {
            "payment_instructions": [],
        })
        log.info("[pay] confirm response: %d %s", pc["status"], json.dumps(pc.get("body", {}), ensure_ascii=False)[:800])
        if pc["status"] != 200:
            return {"success": False, "detail": f"payment confirm failed: {pc['status']}"}

        # 从 confirm response 提取 challenge_id（如果 validate 没给）
        if not pay_challenge_id:
            pc_body = pc.get("body", {})
            pay_challenge_id = self._extract_challenge_id(pc_body)
        if not pay_challenge_id:
            return {"success": False, "detail": "no challenge_id for payment PIN"}
        note(f"payment challenge_id={pay_challenge_id[:16]}")

        time.sleep(1)

        # Step 12: PIN verify (payment)
        note("Step 12: PIN verify (GWC)")
        pay_pin_token = self._pin_verify(pay_challenge_id, pin, PIN_CLIENT_PAYMENT)

        time.sleep(1)

        # Step 13: payment process
        note("Step 13: payment process")
        pp = self._gwa_post(f"/v1/payment/process?reference_id={challenge_ref}", {
            "challenge": {
                "type": "GOPAY_PIN_CHALLENGE",
                "value": {"pin_token": pay_pin_token},
            },
        })
        if pp["status"] != 200:
            return {"success": False, "detail": f"payment process failed: {pp['status']} {str(pp['body'])[:200]}"}
        note("Payment process OK")

        # === Phase D: 验证 ===
        time.sleep(2)

        # Step 14: check status
        note("Step 14: check transaction status")
        ts = self._midtrans_get(f"/snap/v1/transactions/{snap}/status")
        txn_status = ts.get("body", {}).get("transaction_status", "unknown")
        note(f"Transaction status: {txn_status}")

        if txn_status in ("settlement", "capture"):
            return {"success": True, "detail": "payment completed", "transaction_status": txn_status}
        else:
            return {"success": False, "detail": f"transaction_status={txn_status}", "transaction_status": txn_status}
