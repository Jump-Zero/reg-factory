# -*- coding: utf-8 -*-
"""Claude.ai registration through the first-party HTTP protocol.

This is the protocol-only companion to ``register.py``'s Chromium/CDP flow.
It follows the small request sequence used by ClaudeX: discover login methods,
send a magic link, read the link from the configured mailbox, then exchange the
nonce for a ``sessionKey`` cookie.  The browser flow remains the default; use
``register.py --protocol http`` when a browser profile is not desired.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError

from common.file_lock import append_line
from common.session_export import save_claude_token
from common.temp_email import create_mailbox, fetch_messages
from config import (
    CLAUDE_PROTOCOL_VERSION as CONFIG_CLAUDE_PROTOCOL_VERSION,
    CLAUDE_USE_TEMP_EMAIL,
    COOKIE_OUTPUT_DIR,
    TEMP_EMAIL_PROVIDER,
)


BASE_URL = "https://claude.ai"
CLAUDE_PROTOCOL_VERSION = (
    CONFIG_CLAUDE_PROTOCOL_VERSION.strip() or "1.0.0"
)
CLAUDE_CLIENT_PLATFORM = "web_claude_ai"
CLAUDE_CLIENT_SHA = "882d9a7d43eced6a100e636e1dfdebc55764bd78"
_ANON_NS = uuid.UUID("6f4a1c2e-1b3d-4e5f-8a90-0c1d2e3f4a5b")
_DEVICE_NS = uuid.UUID("9d8c7b6a-5e4f-4321-9a8b-7c6d5e4f3a2b")
_PROFILE_NS = uuid.UUID("3c2b1a09-8f7e-4d6c-b5a4-9382716f5e4d")
_DEFAULT_SEED = "claudex-default"
_MAGIC_LINK_RE = re.compile(
    r"https://claude\.ai/magic-link#[^\s<>\"']+",
    re.IGNORECASE,
)
_BROWSER_PROFILES = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "en-US,en;q=0.9",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "en-US,en;q=0.9",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
        "Gecko/20100101 Firefox/133.0",
        "de-DE,de;q=0.9,en;q=0.8",
    ),
)


class ClaudeProtocolError(RuntimeError):
    """A protocol request or mailbox exchange failed."""


def _stable_uuid(namespace: uuid.UUID, seed: str) -> str:
    return str(uuid.uuid5(namespace, seed))


def build_headers(seed: str | None = None, protocol_version: str | None = None) -> dict[str, str]:
    """Build the Claude Web protocol headers for one stable account identity."""
    stable_seed = str(seed or _DEFAULT_SEED)
    index = uuid.uuid5(_PROFILE_NS, stable_seed).int % len(_BROWSER_PROFILES)
    user_agent, accept_language = _BROWSER_PROFILES[index]
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
        "user-agent": user_agent,
        "accept-language": accept_language,
        "anthropic-client-platform": CLAUDE_CLIENT_PLATFORM,
        "anthropic-client-version": str(protocol_version or CLAUDE_PROTOCOL_VERSION).strip() or CLAUDE_PROTOCOL_VERSION,
        "anthropic-client-sha": CLAUDE_CLIENT_SHA,
        "anthropic-anonymous-id": "claudeai.v1." + _stable_uuid(_ANON_NS, stable_seed),
        "anthropic-device-id": _stable_uuid(_DEVICE_NS, stable_seed),
    }


def _proxy_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    try:
        from common import proxy_switch

        return str(proxy_switch.effective_proxy_url() or "").strip()
    except Exception:
        return ""


class ClaudeProtocolClient:
    """Small requests client implementing ClaudeX's registration endpoints."""

    def __init__(self, seed: str | None = None, proxy: str | None = None, *, session=None,
                 protocol_version: str | None = None):
        self.seed = str(seed or _DEFAULT_SEED)
        self.protocol_version = str(protocol_version or CLAUDE_PROTOCOL_VERSION).strip() or CLAUDE_PROTOCOL_VERSION
        self.session = session or requests.Session()
        self.session.headers.update(build_headers(self.seed, self.protocol_version))
        # A short-lived connection is more reliable through rotating proxies.
        self.session.headers.update({"Connection": "close"})
        proxy_url = _proxy_url(proxy)
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def request_json(self, method: str, path: str, *, payload=None, params=None, retries=5):
        url = path if str(path).startswith("http") else BASE_URL + str(path)
        last_error = None
        for attempt in range(max(1, int(retries))):
            try:
                request_method = getattr(self.session, method.lower(), None)
                if request_method is None:
                    request_method = self.session.request
                    response = request_method(
                        method.upper(), url, json=payload, params=params, timeout=30
                    )
                else:
                    response = request_method(
                        url, json=payload, params=params, timeout=30
                    )
                if response.status_code == 429:
                    last_error = ClaudeProtocolError("HTTP 429")
                    if attempt + 1 < retries:
                        time.sleep(2 * (attempt + 1))
                        continue
                if response.status_code >= 400:
                    detail = str(getattr(response, "text", "") or "")[:240]
                    raise ClaudeProtocolError(f"Claude HTTP {response.status_code}: {detail}")
                if not getattr(response, "text", "").strip():
                    return {}
                try:
                    data = response.json()
                except (TypeError, ValueError) as exc:
                    raise ClaudeProtocolError("Claude returned a non-JSON response") from exc
                if not isinstance(data, dict):
                    raise ClaudeProtocolError("Claude returned an invalid JSON object")
                return data
            except (SSLError, RequestsConnectionError) as exc:
                last_error = exc
                if attempt + 1 >= retries:
                    break
                time.sleep(min(2 ** attempt, 8))
        raise ClaudeProtocolError(str(last_error or "Claude request failed")) from last_error

    def get_login_methods(self, email: str) -> dict[str, Any]:
        return self.request_json(
            "GET", "/api/auth/login_methods", params={"email": email, "source": "claude"}
        )

    def send_magic_link(self, email: str, *, utc_offset: int = -480) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/api/auth/send_magic_link",
            payload={
                "utc_offset": utc_offset,
                "email_address": email,
                "login_intent": None,
                "locale": "en-US",
                "return_to": None,
                "source": "claude",
            },
        )

    def verify_magic_link(self, magic_link: str) -> dict[str, Any]:
        from urllib.parse import unquote, urlsplit

        fragment = unquote(urlsplit(magic_link).fragment)
        nonce, separator, encoded_email = fragment.partition(":")
        if not separator or not nonce or not encoded_email:
            raise ClaudeProtocolError("invalid Claude magic-link fragment")
        response = self.request_json(
            "POST",
            "/api/auth/verify_magic_link",
            payload={
                "credentials": {
                    "method": "nonce",
                    "nonce": nonce,
                    "encoded_email_address": encoded_email,
                },
                "locale": "en-US",
                "source": "claude",
            },
        )
        session_key = _session_key_from_cookiejar(self.session.cookies)
        if not session_key:
            session_key = _find_session_key(response)
            if session_key:
                self.session.cookies.set("sessionKey", session_key, domain=".claude.ai", path="/")
        if not session_key:
            raise ClaudeProtocolError("Claude verification succeeded without a sessionKey cookie")
        return response


def _find_session_key(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in {"sessionkey", "session_key"} and nested:
                return str(nested).strip()
            found = _find_session_key(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_session_key(nested)
            if found:
                return found
    return ""


def _session_key_from_cookiejar(jar) -> str:
    for cookie in jar:
        if cookie.name == "sessionKey" and cookie.value:
            return str(cookie.value)
    return ""


def _cookies_from_session(client: ClaudeProtocolClient) -> list[dict[str, Any]]:
    cookies = []
    for cookie in client.session.cookies:
        if not cookie.name or cookie.value is None:
            continue
        cookies.append(
            {
                "name": cookie.name,
                "value": str(cookie.value),
                "domain": cookie.domain or ".claude.ai",
                "path": cookie.path or "/",
                "secure": True,
                "httpOnly": bool(getattr(cookie, "_rest", {}).get("HttpOnly")),
                "sameSite": "Lax",
            }
        )
    return cookies


def extract_magic_link(value: Any) -> str | None:
    """Extract a Claude magic-link from plain text or HTML mail content.

    Mail providers may HTML-escape the fragment or percent-encode the colon
    separating the nonce and encoded address.  Keep the returned URL intact
    enough for ``verify_magic_link`` to decode it once, while removing markup
    punctuation that commonly follows a link in a message body.
    """
    from html import unescape
    from urllib.parse import unquote, urlsplit

    text = unescape(str(value or ""))
    match = _MAGIC_LINK_RE.search(text)
    if not match:
        return None
    link = match.group(0).rstrip(".,;!?)\\]}")
    try:
        fragment = unquote(urlsplit(link).fragment)
    except Exception:
        return None
    nonce, separator, encoded_email = fragment.partition(":")
    if not separator or not nonce or not encoded_email:
        return None
    return link


def poll_temp_magic_link(mailbox: dict, *, max_wait=120, poll_interval=5) -> str | None:
    """Poll a common.temp_email mailbox without depending on browser code."""
    consumed = mailbox.setdefault("_consumed_protocol_links", set())
    deadline = time.monotonic() + max(1, int(max_wait))
    while time.monotonic() < deadline:
        try:
            messages = fetch_messages(
                mailbox["id"], mailbox["provider"], email=mailbox.get("email"),
                token=mailbox.get("token", ""), api_key=mailbox.get("api_key"),
                base_url=mailbox.get("base_url"),
            )
            for message in messages:
                candidates = [message]
                candidates.extend((message.get("extracted") or {}).get("links") or [])
                for candidate in candidates:
                    link = extract_magic_link(candidate if isinstance(candidate, str) else json.dumps(candidate))
                    if link and link not in consumed:
                        consumed.add(link)
                        return link
        except Exception:
            pass
        time.sleep(max(0.5, float(poll_interval)))
    return None


def poll_outlook_magic_link(email: str, refresh_token: str, client_id: str, *, max_wait=120) -> str | None:
    from common.mailbox import get_link_by_token

    return get_link_by_token(
        email,
        refresh_token,
        client_id=client_id,
        link_regex=r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:=+/]+",
        sender_contains=("anthropic", "claude", "noreply", "no-reply"),
        subject_contains=("magic", "verify", "sign in", "login", "magic link"),
        must_contain="claude.ai/magic-link",
        max_wait=max_wait,
        poll=5,
    )


def _safe_name(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(email or "account"))[:100] or "account"


def save_protocol_session(client: ClaudeProtocolClient, email: str, password: str = "") -> str:
    session_key = _session_key_from_cookiejar(client.session.cookies)
    if not session_key:
        raise ClaudeProtocolError("protocol response did not provide sessionKey")
    cookies = _cookies_from_session(client)
    output_dir = Path(COOKIE_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    prefix = f"protocol_{_safe_name(email)}_{stamp}"
    (output_dir / f"sk_{prefix}.txt").write_text(session_key, encoding="utf-8")
    (output_dir / f"full_{prefix}.json").write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    append_line(output_dir / "accounts.txt", f"{email}|{password or ''}|{session_key}")
    try:
        save_claude_token(session_key, email)
    except Exception:
        pass
    return session_key


def _mailbox_for_account(provider: str | None, domain: str | None = None) -> dict:
    return create_mailbox(provider=provider or TEMP_EMAIL_PROVIDER, domain=domain or None)


def register_one(
    email: str,
    password: str = "",
    refresh_token: str = "",
    client_id: str = "",
    *,
    provider: str | None = None,
    domain: str | None = None,
    proxy: str | None = None,
    mailbox_wait: int = 120,
    protocol_version: str | None = None,
) -> str:
    """Register one account and return its sessionKey."""
    mailbox = None
    if not email:
        mailbox = _mailbox_for_account(provider, domain)
        email = str(mailbox["email"])
    elif refresh_token:
        try:
            from common.emails import mark_registration_started

            mark_registration_started("claude", email, password)
        except Exception:
            pass
    client = ClaudeProtocolClient(seed=email, proxy=proxy, protocol_version=protocol_version)
    try:
        client.get_login_methods(email)
        client.send_magic_link(email)
        if mailbox is not None:
            magic_link = poll_temp_magic_link(mailbox, max_wait=mailbox_wait)
        elif refresh_token:
            magic_link = poll_outlook_magic_link(
                email, refresh_token, client_id, max_wait=mailbox_wait
            )
        else:
            raise ClaudeProtocolError("protocol registration needs a temp mailbox or Outlook refresh token")
        if not magic_link:
            raise ClaudeProtocolError("timed out waiting for Claude magic link")
        client.verify_magic_link(magic_link)
        session_key = save_protocol_session(client, email, password)
        if mailbox is None:
            try:
                from common.emails import mark_used

                mark_used("claude", email, password)
            except Exception:
                pass
        return session_key
    finally:
        client.close()


def _load_accounts(path: str | None) -> list[tuple[str, str, str, str]]:
    if not path:
        return []
    records = []
    raw = Path(path).read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        parsed = parsed.get("accounts") or parsed.get("data") or []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            address = str(item.get("email") or item.get("email_address") or "").strip()
            if "@" not in address:
                continue
            records.append((
                address,
                str(item.get("password") or "").strip(),
                str(item.get("refresh_token") or item.get("token") or "").strip(),
                str(item.get("client_id") or "").strip(),
            ))
        return records
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 2 and "@" in parts[0]:
            records.append((parts[0].strip(), parts[1].strip(), parts[2].strip() if len(parts) > 2 else "", parts[3].strip() if len(parts) > 3 else ""))
    return records


def run_batch(
    *,
    count: int = 1,
    concurrency: int = 1,
    email: str = "",
    password: str = "",
    token: str = "",
    client_id: str = "",
    emails_file: str | None = None,
    latest_rt: bool = False,
    provider: str | None = None,
    domain: str | None = None,
    proxy: str | None = None,
    mailbox_wait: int = 120,
    protocol_version: str | None = None,
) -> list[str]:
    accounts = _load_accounts(emails_file)
    if email:
        accounts.insert(0, (email, password, token, client_id))
    if latest_rt and not accounts:
        from common import emails as email_pool

        for _ in range(max(1, count)):
            account = email_pool.latest_email("claude", require_token=True, validate_token=True)
            if not account:
                break
            accounts.append(account)
    if not accounts:
        if not (provider or CLAUDE_USE_TEMP_EMAIL):
            print("  [claude-http] no mailbox configured; use --provider, --latest-rt, or --email")
            return []
        accounts = [("", "", "", "") for _ in range(max(1, count))]
    accounts = accounts[: max(1, count)] if count else accounts

    def task(account):
        return register_one(
            account[0], account[1], account[2], account[3],
            provider=provider, domain=domain, proxy=proxy, mailbox_wait=mailbox_wait,
            protocol_version=protocol_version,
        )

    results: list[str] = []
    workers = max(1, min(int(concurrency or 1), len(accounts)))
    if workers == 1:
        for account in accounts:
            try:
                results.append(task(account))
            except Exception as exc:
                print(f"  [claude-http] registration failed: {exc}")
        return results
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, account) for account in accounts]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                print(f"  [claude-http] registration failed: {exc}")
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude.ai HTTP protocol registration")
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--concurrency", "-c", type=int, default=1)
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--client-id", default="")
    parser.add_argument("--emails", default="")
    parser.add_argument("--latest-rt", action="store_true")
    parser.add_argument("--provider", default="")
    parser.add_argument("--domain", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--mailbox-wait", type=int, default=120)
    parser.add_argument("--protocol-version", default=CLAUDE_PROTOCOL_VERSION)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    results = run_batch(
        count=args.count,
        concurrency=args.concurrency,
        email=args.email,
        password=args.password,
        token=args.token,
        client_id=args.client_id,
        emails_file=args.emails or None,
        latest_rt=args.latest_rt,
        provider=args.provider or (TEMP_EMAIL_PROVIDER if CLAUDE_USE_TEMP_EMAIL else None),
        domain=args.domain or None,
        proxy=args.proxy or None,
        mailbox_wait=max(1, args.mailbox_wait),
        protocol_version=args.protocol_version,
    )
    print(f"[claude-http] completed: {len(results)}/{max(1, args.count)}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
