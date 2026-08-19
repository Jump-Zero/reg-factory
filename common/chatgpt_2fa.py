# -*- coding: utf-8 -*-
"""Enable ChatGPT authenticator 2FA from an already signed-in browser page."""

import asyncio
import json
import time
import uuid
from urllib.parse import urlencode

from common.oauth_codex import _totp_code
from common.session_export import fetch_chatgpt_session


class ChatGPTTwoFactorError(RuntimeError):
    pass


_BROWSER_FETCH = """
async request => {
    const options = {
        method: request.method || 'GET',
        credentials: 'include',
        cache: 'no-store',
        headers: request.headers || {},
    };
    if (request.body !== undefined && request.body !== null) {
        options.body = request.body;
    }
    try {
        const response = await fetch(request.url, options);
        const text = await response.text();
        let data = null;
        try { data = text ? JSON.parse(text) : {}; } catch (_) {}
        return {
            ok: response.ok,
            status: response.status,
            data,
            text: data === null ? text.slice(0, 400) : '',
        };
    } catch (error) {
        return {ok: false, status: 0, data: null, text: String(error).slice(0, 400)};
    }
}
"""


async def _browser_fetch_json(page, url, *, method="GET", headers=None, body=None):
    return await page.evaluate(
        _BROWSER_FETCH,
        {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
        },
    )


def _error_message(response, action):
    status = int((response or {}).get("status") or 0)
    data = (response or {}).get("data")
    message = ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "")
        elif error:
            message = str(error)
        message = message or str(data.get("message") or data.get("code") or "")
    message = message or str((response or {}).get("text") or "")
    suffix = f": {message[:160]}" if message else ""
    return ChatGPTTwoFactorError(f"{action} failed (HTTP {status}){suffix}")


def _response_data(response, action):
    if not isinstance(response, dict) or not response.get("ok"):
        raise _error_message(response, action)
    data = response.get("data")
    if not isinstance(data, dict):
        raise ChatGPTTwoFactorError(f"{action} returned a non-JSON response")
    return data


async def _device_id(context):
    try:
        for cookie in await context.cookies():
            if cookie.get("name") == "oai-did" and cookie.get("value"):
                return str(cookie["value"])
    except Exception:
        pass
    return str(uuid.uuid4())


async def _goto_committed(page, url, timeout=60000):
    try:
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    except Exception:
        # Slow proxy routes often time out after the destination document has
        # committed. The next API call is the authoritative readiness check.
        if not getattr(page, "url", ""):
            raise
    await asyncio.sleep(2)


async def _fresh_session(page, attempts=4):
    for attempt in range(attempts):
        session = await fetch_chatgpt_session(page)
        if session:
            return session
        if attempt + 1 < attempts:
            await asyncio.sleep(2)
    raise ChatGPTTwoFactorError("2FA reauthentication did not refresh the ChatGPT session")


async def enable_chatgpt_totp(page, context, email, fetch_email_code):
    """Enable TOTP and return ``(secret, refreshed_session)``.

    ``fetch_email_code`` is the registration flow's async mailbox callback. It
    is reused so Outlook Graph, browser fallback, iCloud, resend exclusion, and
    mailbox credentials remain owned by the caller.
    """
    if "chatgpt.com" not in str(getattr(page, "url", "")).lower():
        await _goto_committed(page, "https://chatgpt.com/")

    device_id = await _device_id(context)
    csrf_data = _response_data(
        await _browser_fetch_json(page, f"/api/auth/csrf?ts={int(time.time() * 1000)}"),
        "2FA CSRF request",
    )
    csrf_token = str(csrf_data.get("csrfToken") or "").strip()
    if not csrf_token:
        raise ChatGPTTwoFactorError("2FA CSRF response did not include csrfToken")

    query = urlencode({
        "connection": "password",
        "login_hint": str(email or "").strip(),
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": device_id,
    })
    form = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf_token,
        "json": "true",
    })
    requested_at = time.time()
    signin_data = _response_data(
        await _browser_fetch_json(
            page,
            f"/api/auth/signin/openai?{query}",
            method="POST",
            headers={"content-type": "application/x-www-form-urlencoded"},
            body=form,
        ),
        "2FA reauthentication",
    )
    auth_url = str(signin_data.get("url") or "").strip()
    if not auth_url:
        raise ChatGPTTwoFactorError("2FA reauthentication did not return an authorize URL")

    print("  [2fa] reauth requested; waiting for a fresh email code...")
    await _goto_committed(page, auth_url)
    code = await fetch_email_code(
        received_after=requested_at,
        allow_browser_fallback=True,
    )
    if not code:
        raise ChatGPTTwoFactorError("2FA reauthentication email code was not received")

    validate_data = _response_data(
        await _browser_fetch_json(
            page,
            "/api/accounts/email-otp/validate",
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps({"code": str(code)}, separators=(",", ":")),
        ),
        "2FA email verification",
    )
    continue_url = str(validate_data.get("continue_url") or "").strip()
    if not continue_url:
        raise ChatGPTTwoFactorError("2FA email verification did not return continue_url")

    await _goto_committed(page, continue_url)
    session = await _fresh_session(page)
    access_token = str(session.get("accessToken") or "").strip()
    language = await page.evaluate("() => navigator.language || 'en-US'")
    api_headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "oai-device-id": device_id,
        "oai-language": str(language or "en-US"),
    }
    enrollment = _response_data(
        await _browser_fetch_json(
            page,
            "/backend-api/accounts/mfa/enroll",
            method="POST",
            headers=api_headers,
            body=json.dumps({"factor_type": "totp"}, separators=(",", ":")),
        ),
        "TOTP enrollment",
    )
    secret = str(enrollment.get("secret") or "").strip()
    session_id = str(enrollment.get("session_id") or "").strip()
    if not secret or not session_id:
        raise ChatGPTTwoFactorError("TOTP enrollment response was missing required fields")

    activation = _response_data(
        await _browser_fetch_json(
            page,
            "/backend-api/accounts/mfa/user/activate_enrollment",
            method="POST",
            headers=api_headers,
            body=json.dumps(
                {
                    "code": _totp_code(secret),
                    "factor_type": "totp",
                    "session_id": session_id,
                },
                separators=(",", ":"),
            ),
        ),
        "TOTP activation",
    )
    if activation.get("success") is not True:
        raise ChatGPTTwoFactorError("TOTP activation returned success=false")

    print("  [2fa] authenticator enabled; secret saved with the account")
    return secret, session
