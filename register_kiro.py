# -*- coding: utf-8 -*-
"""Register AWS Builder ID accounts and export credentials for Kiro."""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import secrets
import string
import sys
import threading
import time
import urllib.parse
import uuid

import requests as standard_requests

try:
    from curl_cffi import requests as http_requests
except Exception:  # pragma: no cover - exercised only on minimal installs
    import requests as http_requests

from common import emails as email_pool
from common import proxy_switch
from common.kiro_crypto import FingerprintBuilder, _b64url, encrypt_password
from common.mailbox import get_code_by_token
from common.session_export import build_kiro_rs_credentials, save_kiro_token


OIDC_BASE = "https://oidc.us-east-1.amazonaws.com"
SIGNIN_BASE = "https://us-east-1.signin.aws"
PROFILE_BASE = "https://profile.aws.amazon.com"
VIEW_BASE = "https://view.awsapps.com"
PORTAL_BASE = "https://portal.sso.us-east-1.amazonaws.com"
DIRECTORY_ID = "d-9067642ac7"
START_URL = f"{VIEW_BASE}/start"
SCOPES = ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations",
          "codewhisperer:transformations", "codewhisperer:taskassist"]

_APP_CONFIG_LOCK = threading.Lock()
_APP_CONFIG_CACHE = None


class KiroError(RuntimeError):
    pass


def _random_password() -> str:
    return "Aa1!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))


def _uuid() -> str:
    return str(uuid.uuid4())


def _query(url: str, key: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        query = parsed.query
        if not query and parsed.fragment and "?" in parsed.fragment:
            query = parsed.fragment.split("?", 1)[1]
        return urllib.parse.parse_qs(query).get(key, [""])[0]
    except Exception:
        return ""


def _json(response):
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class KiroClient:
    def __init__(self, proxy: str = "", timeout: int = 60):
        self.timeout = timeout
        self.proxy = proxy
        kwargs = {"timeout": timeout, "verify": False}
        try:
            self.session = http_requests.Session(impersonate="chrome131", **kwargs)
        except TypeError:
            self.session = http_requests.Session(**kwargs)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        awsccc = {"e": 1, "p": 1, "f": 1, "a": 1, "i": str(uuid.uuid4()), "v": "1"}
        self.session.cookies.set("awsccc", base64.b64encode(json.dumps(awsccc, separators=(",", ":")).encode()).decode())
        self.fp = FingerprintBuilder()
        self.workflow_handle = ""
        self.workflow_id = ""
        self.workflow_state = ""
        self.visitor_id = _uuid()
        self.ubid = ""
        self.auth_code = ""
        self.sso_state = ""
        self.wdc_csrf = ""
        self.sso_token_value = ""
        self.user_code = ""
        self.device_code = ""
        self.client_id = ""
        self.client_secret = ""

    @staticmethod
    def _is_tls_transport_error(error):
        message = str(error or "").lower()
        return any(marker in message for marker in (
            "tls connect error",
            "openssl_internal",
            "invalid library",
            "ssl connect error",
        ))

    def _standard_session(self):
        session = standard_requests.Session()
        session.verify = False
        session.headers.update(dict(self.session.headers))
        try:
            session.cookies.update(self.session.cookies.get_dict())
        except Exception:
            pass
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        return session

    def _headers(self, referer="", origin="", content_type="application/json", fetch_site="same-origin"):
        headers = {"Accept": "application/json, text/plain, */*", "Content-Type": content_type,
                   "User-Agent": self.fp.ua, "sec-ch-ua": self.fp.sec_ua,
                   "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
                   "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": fetch_site}
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        return headers

    def _nav_headers(self, referer=""):
        """Headers for top-level page navigation (document requests)."""
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "User-Agent": self.fp.ua, "sec-ch-ua": self.fp.sec_ua,
                   "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
                   "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
                   "sec-fetch-site": "none" if not referer else "cross-site",
                   "Upgrade-Insecure-Requests": "1"}
        if referer:
            headers["Referer"] = referer
        return headers

    def warmup(self):
        """Visit the start page and signin page to establish session cookies before API calls."""
        try:
            self.session.get(
                f"{VIEW_BASE}/start/",
                headers=self._nav_headers(),
                timeout=self.timeout, allow_redirects=True, verify=False,
            )
            time.sleep(random.uniform(0.5, 1.5))
        except Exception:
            pass
        try:
            self.session.get(
                f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login",
                headers=self._nav_headers(referer=f"{VIEW_BASE}/start/"),
                timeout=self.timeout, allow_redirects=True, verify=False,
            )
            time.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    def request(self, method, url, payload=None, headers=None, *, data=None, expected=None, allow_redirects=True):
        kwargs = {"headers": headers or self._headers(), "allow_redirects": allow_redirects, "timeout": self.timeout}
        if data is not None:
            kwargs["data"] = data
        elif payload is not None:
            kwargs["json"] = payload
        try:
            response = self.session.request(method, url, **kwargs)
        except Exception as error:
            if not self._is_tls_transport_error(error):
                raise
            print("  [kiro] curl TLS transport failed; retrying with standard requests")
            fallback = self._standard_session()
            response = fallback.request(method, url, **kwargs)
            try:
                self.session.close()
            except Exception:
                pass
            self.session = fallback
        if expected is not None and response.status_code not in set(expected):
            body = response.text[:400].replace("\n", " ")
            raise KiroError(f"HTTP {response.status_code}: {body}")
        return response

    def post(self, url, payload, headers=None, expected=(200,)):
        return self.request("POST", url, payload, headers, expected=expected)

    def post_form(self, url, data, headers=None, expected=(200,)):
        return self.request("POST", url, None, headers, data=data, expected=expected)

    def get(self, url, headers=None, expected=(200,), allow_redirects=True):
        return self.request("GET", url, None, headers, expected=expected, allow_redirects=allow_redirects)

    def register_client(self):
        data = _json(self.post(f"{OIDC_BASE}/client/register", {
            "clientName": "Amazon Q Developer for command line", "clientType": "public", "scopes": SCOPES,
        }))
        self.client_id = str(data.get("clientId") or "")
        self.client_secret = str(data.get("clientSecret") or "")
        if not self.client_id or not self.client_secret:
            raise KiroError("OIDC 客户端注册失败")

    def register_device(self):
        data = _json(self.post(f"{OIDC_BASE}/device_authorization", {
            "clientId": self.client_id, "clientSecret": self.client_secret, "startUrl": START_URL,
        }))
        self.device_code = str(data.get("deviceCode") or "")
        self.user_code = str(data.get("userCode") or "")
        if not self.device_code or not self.user_code:
            raise KiroError("设备授权初始化失败")
        print(f"  [kiro] device code ready: {self.user_code}")

    def fetch_app_config(self):
        global _APP_CONFIG_CACHE

        try:
            # app.js is large and identical for every account in one batch.
            with _APP_CONFIG_LOCK:
                if _APP_CONFIG_CACHE is None:
                    response = self.get(
                        f"{SIGNIN_BASE}/assets/js/app.js",
                        headers={"Accept": "*/*", "Referer": f"{SIGNIN_BASE}/"},
                    )
                    old_key = self.fp.key
                    self.fp.update_app_js(response.text)
                    if self.fp.key != old_key:
                        print("  [kiro] fingerprint key updated from app.js")
                    else:
                        print("  [kiro] warning: using fallback fingerprint key (app.js regex may be outdated)")
                    _APP_CONFIG_CACHE = (
                        self.fp.key,
                        self.fp.identifier,
                        self.fp.version,
                    )
                else:
                    self.fp.key, self.fp.identifier, self.fp.version = _APP_CONFIG_CACHE
        except Exception as e:
            print(f"  [kiro] warning: failed to fetch app.js: {str(e)[:80]}")

    def portal_login(self, user_code=None):
        code = user_code or self.user_code
        redirect = urllib.parse.quote(f"{VIEW_BASE}/start/#/device?user_code={code}", safe="")
        # portal.sso.us-east-1.amazonaws.com is a different site from view.awsapps.com
        headers = self._headers(origin=VIEW_BASE, referer=f"{VIEW_BASE}/", fetch_site="cross-site")
        try:
            response = self.get(f"{PORTAL_BASE}/login?directory_id=view&redirect_url={redirect}",
                                headers=headers)
        except KiroError as e:
            raise KiroError(f"Portal login 被拦截 (可能IP信誉差): {e}")
        data = _json(response)
        location = str(data.get("redirectUrl") or "")
        self.workflow_handle = _query(location, "workflowStateHandle")
        csrf = str(data.get("csrfToken") or "")
        if csrf:
            self.session.cookies.set("loginCsrfToken", csrf)
        if not self.workflow_handle:
            raise KiroError("Portal 未返回工作流句柄")
        self.fetch_d2c(f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={self.workflow_handle}")

    def fetch_d2c(self, referer):
        parsed = urllib.parse.urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else SIGNIN_BASE
        # vs.aws.amazon.com is a different site from us-east-1.signin.aws
        headers = self._headers(origin=origin, referer=referer, fetch_site="cross-site")
        try:
            response = self.post("https://vs.aws.amazon.com/token", {}, headers=headers)
        except KiroError as e:
            raise KiroError(f"D2C token 获取被拦截 (TES风控): {e}")
        data = _json(response)
        token = str(data.get("token") or "")
        if token:
            self.session.cookies.set("awsd2c-token", token)
            self.session.cookies.set("awsd2c-token-c", token)
            try:
                segment = token.split(".")[1]
                segment += "=" * (-len(segment) % 4)
                claims = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
                self.visitor_id = str(claims.get("vid") or self.visitor_id)
            except Exception:
                pass

    def _fingerprint_input(self, page_type, event_type, time_on_page=0, email=""):
        if page_type == "profile":
            location = f"{PROFILE_BASE}/?workflowID={self.workflow_id}#/signup/{'enter-email' if event_type == 'PageSubmit' else 'start'}"
            referer = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup?workflowStateHandle={self.workflow_handle}"
        else:
            location = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/{page_type}?workflowStateHandle={self.workflow_handle}"
            referer = f"{VIEW_BASE}/"
        return self.fp.encrypted(location, referer, page_type, event_type, time_on_page, email)

    def workflow_execute(self, endpoint, payload, referer):
        rid = _uuid()
        headers = self._headers(referer=referer, origin=SIGNIN_BASE)
        headers.update({"x-amzn-requestid": rid, "x-amz-date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())})
        payload["requestId"] = rid
        return self.post(endpoint, payload, headers=headers, expected=(200, 400))

    def workflow_init(self):
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={self.workflow_handle}"
        for step_id, event in (("", "first_load"), ("start", "PageLoad")):
            response = self.workflow_execute(endpoint, {
                "stepId": step_id, "workflowStateHandle": self.workflow_handle,
                "inputs": [{"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signin", event)}],
            }, referer)
            data = _json(response)
            self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
            if step_id == "" and data.get("stepId") != "start":
                break

    def submit_email(self, email):
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={self.workflow_handle}"
        response = self.workflow_execute(endpoint, {
            "stepId": "get-identity-user", "workflowStateHandle": self.workflow_handle, "actionId": "SUBMIT",
            "inputs": [{"input_type": "UserRequestInput", "username": email},
                       {"input_type": "ApplicationTypeRequestInput", "applicationType": "SSO_INDIVIDUAL_ID"},
                       {"input_type": "UserEventRequestInput", "directoryId": DIRECTORY_ID, "userName": email,
                        "userEvents": [{"input_type": "UserEvent", "eventType": "PAGE_SUBMIT",
                                        "pageName": "IDENTIFICATION", "timeSpentOnPage": 5000}]},
                       {"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signin", "PageSubmit", email=email)}],
            "visitorId": self.visitor_id,
        }, referer)
        data = _json(response)
        self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
        return "signup" if response.status_code == 400 else "login" if response.status_code == 200 else "unknown"

    def signup(self, email):
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={self.workflow_handle}"
        response = self.workflow_execute(endpoint, {
            "stepId": "get-identity-user", "workflowStateHandle": self.workflow_handle, "actionId": "SIGNUP",
            "inputs": [{"input_type": "UserRequestInput", "username": email},
                       {"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signup", "PageSubmit")}],
            "visitorId": self.visitor_id,
        }, referer)
        data = _json(response)
        redirect = str((data.get("redirect") or {}).get("url") or "")
        self.workflow_handle = _query(redirect, "workflowStateHandle") or self.workflow_handle

    def signup_init(self, email):
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup/api/execute"
        referer = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup?workflowStateHandle={self.workflow_handle}"
        last_response = ""
        for step_id, event in (("", "first_load"), ("start", "PageLoad")):
            response = self.workflow_execute(endpoint, {
                "stepId": step_id, "workflowStateHandle": self.workflow_handle,
                "inputs": [{"input_type": "UserRequestInput", "username": email},
                           {"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signup", event)}],
                "visitorId": self.visitor_id,
            }, referer)
            last_response = response.text[:600].replace("\n", " ")
            data = _json(response)
            self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
            redirect = str((data.get("redirect") or {}).get("url") or "")
            self.workflow_id = _query(redirect, "workflowID") or self.workflow_id
        if not self.workflow_id:
            raise KiroError(f"Signup 未返回 workflowID: {last_response}")

    def profile_init(self):
        self.ubid = f"186-{random.randrange(10**7):07d}-{random.randrange(10**6):06d}"
        self.session.cookies.set("aws-user-profile-ubid", self.ubid)
        self.session.cookies.set("i18next", "en-US")
        url = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        self.get(url, headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": self.fp.ua})
        self.fetch_d2c(url)

    def profile_start(self):
        ref = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        response = self.post(f"{PROFILE_BASE}/api/start", {
            "workflowID": self.workflow_id,
            "browserData": {"attributes": {"fingerprint": self._fingerprint_input("profile", "PageLoad"),
                                              "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                              "timeSpentOnPage": "38", "eventType": "PageLoad", "ubid": self.ubid,
                                              "visitorId": self.visitor_id}, "cookies": {}},
        }, headers=self._headers(origin=PROFILE_BASE, referer=ref))
        self.workflow_state = str(_json(response).get("workflowState") or "")
        if not self.workflow_state:
            raise KiroError("Profile 启动失败")

    def send_otp(self, email):
        sent_at = time.time()
        ref = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        response = self.post(f"{PROFILE_BASE}/api/send-otp", {
            "workflowState": self.workflow_state, "email": email,
            "browserData": {"attributes": {"fingerprint": self._fingerprint_input("profile", "PageSubmit", 6500, email),
                                              "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                              "timeSpentOnPage": "6500", "pageName": "EMAIL_COLLECTION", "eventType": "PageSubmit",
                                              "ubid": self.ubid, "visitorId": self.visitor_id}, "cookies": {}},
        }, headers=self._headers(origin=PROFILE_BASE, referer=ref), expected=(200,))
        return sent_at

    def create_identity(self, email, full_name, otp):
        ref = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        response = self.post(f"{PROFILE_BASE}/api/create-identity", {
            "workflowState": self.workflow_state, "userData": {"email": email, "fullName": full_name}, "otpCode": otp,
            "browserData": {"attributes": {"fingerprint": self._fingerprint_input("profile", "EmailVerification"),
                                              "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                                              "timeSpentOnPage": "45000", "pageName": "EMAIL_VERIFICATION", "eventType": "EmailVerification",
                                              "ubid": self.ubid, "visitorId": self.visitor_id}, "cookies": {}},
        }, headers=self._headers(origin=PROFILE_BASE, referer=ref))
        data = _json(response)
        registration_code = str(data.get("registrationCode") or "")
        sign_state = str(data.get("signInState") or "")
        if not registration_code:
            raise KiroError("验证码校验失败")
        return registration_code, sign_state

    def set_password(self, email, password, registration_code, sign_state):
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup/api/execute"
        ref = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup?registrationCode={registration_code}&state={sign_state}"
        response = self.workflow_execute(endpoint, {
            "stepId": "", "state": sign_state,
            "inputs": [{"input_type": "UserRegistrationRequestInput", "registrationCode": registration_code, "state": sign_state},
                       {"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signup", "PageSubmit")}],
        }, ref)
        data = _json(response)
        context = ((data.get("workflowResponseData") or {}).get("encryptionContextResponse") or {})
        jwk = ((context.get("publicKey") or {}) if isinstance(context, dict) else {})
        if not jwk.get("n") or not jwk.get("e"):
            raise KiroError("未获取到密码加密公钥")
        encrypted = encrypt_password(password, jwk, str(context.get("issuer") or "signin"),
                                     str(context.get("audience") or "AWSPasswordService"), str(context.get("region") or "us-east-1"))
        self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
        response = self.workflow_execute(endpoint, {
            "stepId": "get-new-password-for-password-creation", "workflowStateHandle": self.workflow_handle,
            "actionId": "SUBMIT", "inputs": [{"input_type": "PasswordRequestInput", "password": encrypted, "successfullyEncrypted": "SUCCESSFUL"},
                                                {"input_type": "UserRequestInput", "username": email},
                                                {"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signup", "PageSubmit")}],
            "visitorId": self.visitor_id,
        }, ref)
        redirect = str((_json(response).get("redirect") or {}).get("url") or "")
        if not redirect:
            raise KiroError("密码设置失败")
        return _query(redirect, "workflowStateHandle"), _query(redirect, "state"), _query(redirect, "workflowResultHandle")

    def complete_signup(self, handle, state, result_handle, email):
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        ref = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={handle}&state={state}&workflowResultHandle={result_handle}"
        response = self.workflow_execute(endpoint, {
            "stepId": "", "workflowStateHandle": handle, "workflowResultHandle": result_handle, "state": state,
            "inputs": [{"input_type": "UserRequestInput", "username": email},
                       {"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signin", "PageLoad")}],
            "visitorId": self.visitor_id,
        }, ref)
        data = _json(response)
        if data.get("stepId") != "end-of-workflow-success":
            raise KiroError("注册工作流未完成")
        redirect = str((data.get("redirect") or {}).get("url") or "")
        self.auth_code = _query(redirect, "workflowResultHandle")
        self.sso_state = _query(redirect, "state")
        self.wdc_csrf = _query(redirect, "wdc_csrf_token")

    def sso_token(self):
        redirect = urllib.parse.quote(f"{VIEW_BASE}/start/#/", safe="")
        data = _json(self.get(f"{PORTAL_BASE}/login?directory_id=view&redirect_url={redirect}",
                              headers=self._headers(origin=VIEW_BASE, referer=f"{VIEW_BASE}/", fetch_site="cross-site")))
        csrf = str(data.get("csrfToken") or "")
        if csrf:
            self.session.cookies.set("loginCsrfToken", csrf)
        handle = _query(str(data.get("redirectUrl") or ""), "workflowStateHandle")
        if not handle:
            raise KiroError("SSO 工作流初始化失败")
        self.workflow_handle = handle
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        ref = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={handle}"
        response = self.workflow_execute(endpoint, {"stepId": "", "workflowStateHandle": handle,
                                                     "inputs": [{"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signin", "PageLoad")}]}, ref)
        result = _json(response)
        if result.get("stepId") == "start":
            handle = str(result.get("workflowStateHandle") or handle)
            self.workflow_handle = handle
            response = self.workflow_execute(endpoint, {"stepId": "start", "workflowStateHandle": handle,
                                                         "inputs": [{"input_type": "FingerPrintRequestInput", "fingerPrint": self._fingerprint_input("signin", "PageLoad")}]}, ref)
            result = _json(response)
        redirect_url = str((result.get("redirect") or {}).get("url") or "")
        auth_code = _query(redirect_url, "workflowResultHandle") or self.auth_code
        state = _query(redirect_url, "state") or self.sso_state
        wdc_csrf = _query(redirect_url, "wdc_csrf_token") or self.wdc_csrf
        if result.get("stepId") == "end-of-workflow-success" and auth_code:
            start_params = urllib.parse.urlencode({
                key: value for key, value in {
                    "state": state,
                    "workflowResultHandle": auth_code,
                    "wdc_csrf_token": wdc_csrf,
                }.items() if value
            })
            self.get(f"{VIEW_BASE}/start/?{start_params}",
                     headers=self._headers(origin=VIEW_BASE, referer=f"{SIGNIN_BASE}/"))
        sso_headers = self._headers(origin=VIEW_BASE, referer=f"{VIEW_BASE}/", content_type="application/x-www-form-urlencoded")
        # profile.aws.amazon.com and view.awsapps.com can both set this name;
        # the Portal response value is the one required by auth/sso-token.
        csrf_value = csrf or ""
        if not csrf_value:
            for cookie in self.session.cookies:
                if cookie.name == "loginCsrfToken":
                    csrf_value = cookie.value
                    break
        if csrf_value:
            sso_headers["x-amz-sso-csrf-token"] = csrf_value
        token = ""
        for _ in range(5):
            response = self.post_form(
                f"{PORTAL_BASE}/auth/sso-token",
                urllib.parse.urlencode({"authCode": auth_code, "state": state, "orgId": "view"}),
                headers=sso_headers, expected=(200, 401),
            )
            token = str(_json(response).get("token") or "")
            if token or response.status_code != 401:
                break
            time.sleep(3)
        if not token:
            raise KiroError("SSO token 获取失败")
        self.sso_token_value = token
        return token

    def device_token(self):
        token = self.sso_token_value
        accepted = _json(self.post(f"{OIDC_BASE}/device_authorization/accept_user_code",
                                   {"userCode": self.user_code, "userSessionId": token}))
        device_context = accepted.get("deviceContext")
        self.post(f"{OIDC_BASE}/device_authorization/associate_token", {"deviceContext": device_context, "userSessionId": token})
        for _ in range(30):
            response = self.post(f"{OIDC_BASE}/token", {"clientId": self.client_id, "clientSecret": self.client_secret,
                                                         "deviceCode": self.device_code, "grantType": "urn:ietf:params:oauth:grant-type:device_code"}, expected=(200, 400))
            if response.status_code == 200:
                data = _json(response)
                if data.get("refreshToken"):
                    return data
            time.sleep(2)
        raise KiroError("Builder ID token 轮询超时")

    def kiro_authorize(self, sso_token):
        """Best-effort IDE authorization; Builder ID credentials remain usable if this optional step changes."""
        try:
            data = _json(self.post(f"{OIDC_BASE}/client/register", {"clientName": "Kiro IDE", "clientType": "public",
                                                                      "scopes": SCOPES, "redirectUris": ["http://127.0.0.1/oauth/callback"],
                                                                      "grantTypes": ["authorization_code", "refresh_token"],
                                                                      "issuerUrl": START_URL}))
            client_id, client_secret = str(data.get("clientId") or ""), str(data.get("clientSecret") or "")
            if not client_id:
                return {}
            verifier = _b64url(os.urandom(32))
            import hashlib
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            state = _uuid()
            query = urllib.parse.urlencode({"response_type": "code", "client_id": client_id,
                                            "redirect_uri": "http://127.0.0.1:49152/oauth/callback", "scopes": ",".join(SCOPES),
                                            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
            response = self.get(f"{OIDC_BASE}/authorize?{query}", headers={"User-Agent": self.fp.ua}, expected=(302,), allow_redirects=False)
            orchestrator = _query(response.headers.get("Location", ""), "orchestrator_id")
            if not orchestrator:
                return {}
            result = _json(self.post(f"{OIDC_BASE}/authentication_result", {"orchestrator_id": orchestrator},
                                     headers={**self._headers(origin=VIEW_BASE, referer=f"{VIEW_BASE}/"),
                                              "x-amz-sso-bearer-token": sso_token,
                                              "x-amz-sso_bearer_token": sso_token}))
            resume = str(result.get("location") or "")
            response = self.get(resume, headers={"User-Agent": self.fp.ua}, expected=(302,), allow_redirects=False)
            context = _query(response.headers.get("Location", ""), "authorizationResumptionContext")
            if not context:
                return {}
            result = _json(self.post(f"{OIDC_BASE}/device_authorization/associate_token",
                                     {"authorizationResumptionContext": context, "userSessionId": sso_token}))
            response = self.get(str(result.get("location") or ""), headers={"User-Agent": self.fp.ua}, expected=(302,), allow_redirects=False)
            code = _query(response.headers.get("Location", ""), "code")
            if not code:
                return {}
            token = _json(self.post(f"{OIDC_BASE}/token", {"clientId": client_id, "clientSecret": client_secret,
                                                            "grantType": "authorization_code", "code": code,
                                                            "redirectUri": "http://127.0.0.1:49152/oauth/callback",
                                                            "codeVerifier": verifier}))
            return token if token.get("accessToken") else {}
        except Exception as exc:
            print(f"  [kiro] IDE 授权跳过: {str(exc)[:120]}")
            return {}


def _get_kiro_verification_code(
    email, mailbox_token, mailbox_client_id, args, *, mailbox=None, sent_at=None
):
    """Read the Kiro OTP from Graph or a configured HTTP mailbox provider."""
    if mailbox:
        from common.temp_email import poll_verification_code_blocking

        return poll_verification_code_blocking(
            mailbox.get("id") or mailbox.get("email") or email,
            mailbox.get("provider") or "custom",
            email=email,
            token=mailbox.get("token") or "",
            api_key=mailbox.get("api_key") or None,
            base_url=mailbox.get("base_url") or None,
            max_wait=min(180, args.timeout),
            poll_interval=5,
            sender_hint=("amazon", "aws", "signin"),
            subject_hint=("verification", "confirm", "code", "验证码"),
        )
    if not mailbox_token or not mailbox_client_id:
        raise KiroError("Kiro 注册需要 Outlook refresh token 和 client id 读取验证码")
    return get_code_by_token(
        email,
        mailbox_token,
        mailbox_client_id,
        sender_contains=("amazon", "aws", "signin"),
        subject_contains=("verification", "confirm", "code", "验证码"),
        max_wait=min(180, args.timeout),
        poll=5,
        received_after=sent_at,
    )


def register_one(email, mailbox_password, mailbox_token, mailbox_client_id, args, mailbox=None):
    target_password = args.account_password or _random_password()
    proxy = proxy_switch.effective_proxy_url()
    client = KiroClient(proxy=proxy, timeout=args.timeout)
    try:
        client.warmup()
        client.fetch_app_config()
        time.sleep(random.uniform(0.3, 0.8))
        client.register_client()
        time.sleep(random.uniform(0.2, 0.5))
        client.register_device()
        time.sleep(random.uniform(0.5, 1.5))
        client.portal_login()
        time.sleep(random.uniform(0.3, 0.8))
        client.workflow_init()
        time.sleep(random.uniform(0.3, 0.6))
        status = client.submit_email(email)
        if status == "login":
            raise KiroError("邮箱已注册，跳过")
        if status != "signup":
            raise KiroError("邮箱状态无法进入注册")
        client.signup(email); client.signup_init(email); client.profile_init(); client.profile_start()
        time.sleep(random.uniform(0.5, 1.0))
        sent_at = client.send_otp(email)
        print("  [kiro] waiting for verification code")
        otp = _get_kiro_verification_code(
            email,
            mailbox_token,
            mailbox_client_id,
            args,
            mailbox=mailbox,
            sent_at=sent_at,
        )
        if not otp:
            raise KiroError("等待验证码超时")
        registration_code, sign_state = client.create_identity(email, args.full_name, otp)
        handle, state, result_handle = client.set_password(email, target_password, registration_code, sign_state)
        client.complete_signup(handle, state, result_handle, email)
        sso_token = client.sso_token()
        aws_token = client.device_token()
        kiro_token = client.kiro_authorize(sso_token)
        raw_record = {"email": email, "provider": "BuilderId", "region": "us-east-1",
                  "clientId": client.client_id, "clientSecret": client.client_secret,
                  "refreshToken": aws_token.get("refreshToken"), "accessToken": aws_token.get("accessToken"),
                  "expiresIn": aws_token.get("expiresIn")}
        if kiro_token.get("profileArn"):
            raw_record["profileArn"] = kiro_token["profileArn"]
        if not save_kiro_token(raw_record, email):
            raise KiroError("Kiro 凭据落盘失败")
        record = build_kiro_rs_credentials(raw_record, email)
        return {"status": "success", "email": email, "record": record}
    except Exception as exc:
        return {"status": "failed", "email": email, "error": str(exc)[:300]}


def main():
    parser = argparse.ArgumentParser(description="Kiro Builder ID registration")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--concurrency", "-c", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="", help="Outlook mailbox password")
    parser.add_argument("--refresh-token", default="")
    parser.add_argument("--client-id", default="")
    parser.add_argument(
        "--email-provider",
        choices=("pool", "temp", "custom"),
        default="pool",
        help="邮箱来源：pool=Outlook/自有 Graph 邮箱，temp=TEMP_EMAIL_PROVIDER，custom=自建 REST 邮箱 API",
    )
    parser.add_argument(
        "--temp-provider",
        choices=("", "yyds", "remail", "gptmail", "moemail", "cfmail", "icloud", "custom"),
        default="",
        help="--email-provider temp 时覆盖 TEMP_EMAIL_PROVIDER（如 yyds、gptmail、remail、custom）",
    )
    parser.add_argument("--account-password", default="", help="Kiro account password; empty generates one")
    parser.add_argument("--full-name", default="Test User")
    parser.add_argument("--node", default="auto", help="Compatibility option; proxy is selected from WebUI settings")
    parser.add_argument("--keep-on-fail", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    proxy_switch.apply_platform_environment("kiro")
    if args.email:
        if args.email_provider != "pool":
            print("[kiro] --email 仅支持 pool；临时/自建邮箱请省略 --email，由 API 自动分配")
            return 1
        accounts = [(args.email.strip(), args.password.strip(), args.refresh_token.strip(), args.client_id.strip())]
    elif args.email_provider in {"temp", "custom"}:
        from common.temp_email import create_mailbox

        accounts = []
        provider = "custom" if args.email_provider == "custom" else (args.temp_provider.strip() or None)
        for _ in range(max(1, args.count)):
            try:
                mailbox = create_mailbox(provider=provider)
                accounts.append((mailbox["email"], "", "", "", mailbox))
                print(f"[kiro] mailbox allocated provider={mailbox.get('provider')}: {mailbox['email']}")
            except Exception as exc:
                print(f"[kiro] mailbox allocation failed: {str(exc)[:220]}")
                break
    else:
        accounts = []
        for _ in range(max(1, args.count)):
            # Kiro reads the verification code through Microsoft Graph, so do
            # not reserve a mailbox whose refresh token is already unusable.
            account = email_pool.latest_email("kiro", require_token=True, validate_token=True)
            if not account:
                break
            accounts.append(account)
    if not accounts:
        print("[kiro] no mailbox available")
        return 1
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from common.concurrency import build_worker_plan
    from common.task_context import activate_worker

    worker_plan = build_worker_plan("kiro", len(accounts), args.concurrency)
    worker_plan.log()

    def run_lane(slot):
        output = []
        for index in range(slot + 1, len(accounts) + 1, worker_plan.effective_concurrency):
            account = accounts[index - 1]
            with activate_worker(worker_plan.worker(index)) as worker:
                print(
                    f"[kiro] {index}/{len(accounts)} {account[0]} "
                    f"worker={worker.worker_id} slot={worker.slot} "
                    f"proxy={proxy_switch.current_node()}"
                )
                mailbox = account[4] if len(account) > 4 else None
                output.append((index, account, register_one(*account[:4], args, mailbox=mailbox)))
        return output

    success = 0
    with ThreadPoolExecutor(max_workers=worker_plan.effective_concurrency) as pool:
        futures = [
            pool.submit(run_lane, slot)
            for slot in range(worker_plan.effective_concurrency)
        ]
        for future in as_completed(futures):
            for _index, account, result in future.result():
                if result["status"] == "success":
                    success += 1
                    if len(account) == 4:
                        email_pool.mark_used("kiro", account[0], account[1])
                    print(f"[kiro] success: {success}/{len(accounts)}")
                else:
                    if len(account) == 4:
                        email_pool.mark_error(
                            "kiro", account[0], account[1], result.get("error", "failed")
                        )
                    print(f"[kiro] failed: {result.get('error', 'unknown')}")
    return 0 if success == len(accounts) else 1


if __name__ == "__main__":
    sys.exit(main())
