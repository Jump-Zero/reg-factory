"""Parse mailbox, browser-session and Codex token records for batch imports."""

from __future__ import annotations

import hashlib
import json
import re


DEFAULT_GRAPH_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CLIENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
COOKIE_RE = re.compile(
    r"(?:^|[;\s])(?P<name>__Secure-next-auth\.session-token(?:\.\d+)?|session-token)=(?P<value>[^;\s]+)",
    re.IGNORECASE,
)


def _domain(email: str) -> str:
    return str(email or "").rsplit("@", 1)[-1].lower().strip()


def is_icloud_email(email: str) -> bool:
    domain = _domain(email)
    return domain in {"icloud.com", "me.com", "mac.com"} or domain.endswith(".icloud.com")


def is_outlook_email(email: str) -> bool:
    domain = _domain(email)
    return (
        domain.startswith(("outlook.", "hotmail.", "live."))
        or domain in {"outlook.com", "hotmail.com", "live.com", "msn.com", "passport.com"}
        or domain.endswith((".outlook.com", ".hotmail.com", ".live.com"))
    )


def _split_fields(line: str) -> list[str]:
    value = str(line or "").strip()
    # Batch exports commonly use long hyphen/plus separators.  Treat runs as
    # delimiters while preserving single plus signs inside URLs or passwords.
    if re.search(r"(?:-{4,}|\+{4,})", value):
        return [part.strip() for part in re.split(r"(?:-{4,}|\+{4,})", value) if part.strip()]
    if "----" in value:
        return [part.strip() for part in value.split("----")]
    if "\t" in value or any(delimiter in value for delimiter in ("|", ";", ",")):
        normalized = re.sub(r"[\t|;,]+", "\x00", value)
        return [part.strip() for part in normalized.split("\x00")]
    # A colon is accepted only for the simple email:password variant.  Do not
    # split arbitrary tokens, proxy URLs or passwords containing colons.
    if value.count(":") == 1 and re.match(r"^[^\s@]+@[^\s@]+:", value):
        return [part.strip() for part in value.split(":", 1)]
    fields = [part.strip() for part in value.split()]
    return fields


def _token_kind(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    if COOKIE_RE.search(value):
        return "session_token"
    # A three-segment JWT is normally the short-lived ChatGPT access token;
    # five segments are commonly encrypted NextAuth session values.
    segments = value.split(".")
    if len(segments) == 3 and all(segments):
        return "access_token"
    return "session_token"


def _cookies_from_value(value) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        cookies = []
        for match in COOKIE_RE.finditer(value):
            cookies.append({"name": match.group("name"), "value": match.group("value")})
        return cookies
    if not isinstance(value, list):
        return []
    cookies = []
    for item in value:
        if not isinstance(item, dict) or not item.get("name") or not item.get("value"):
            continue
        cookie = dict(item)
        cookie["name"] = str(cookie["name"])
        cookie["value"] = str(cookie["value"])
        cookies.append(cookie)
    return cookies


def _nested(value: dict, *keys):
    for key in keys:
        current = value.get(key)
        if isinstance(current, dict):
            return current
    return {}


def _from_mapping(value: dict) -> dict:
    credentials = _nested(value, "credentials", "oauth", "token")
    user = _nested(value, "user", "profile")
    account = _nested(value, "account", "subscription")
    email = str(
        value.get("email")
        or value.get("username")
        or value.get("login")
        or credentials.get("email")
        or user.get("email")
        or ""
    ).strip()
    password = str(value.get("password") or value.get("pass") or value.get("pwd") or "").strip()
    refresh_token = str(
        value.get("refresh_token")
        or value.get("refreshToken")
        or value.get("oauth_refresh_token")
        or value.get("rt")
        or credentials.get("refresh_token")
        or credentials.get("refreshToken")
        or ""
    ).strip()
    client_id = str(
        value.get("client_id")
        or value.get("clientId")
        or value.get("app_id")
        or value.get("appId")
        or credentials.get("client_id")
        or credentials.get("clientId")
        or ""
    ).strip()
    access_token = str(
        value.get("access_token")
        or value.get("accessToken")
        or value.get("at")
        or credentials.get("access_token")
        or credentials.get("accessToken")
        or ""
    ).strip()
    session_token = str(
        value.get("session_token")
        or value.get("sessionToken")
        or value.get("chatgpt_session_token")
        or ""
    ).strip()
    cookies = _cookies_from_value(value.get("cookies") or value.get("cookie"))
    if not session_token:
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            if name.lower() in {"__secure-next-auth.session-token", "session-token"}:
                session_token = str(cookie.get("value") or "")
                break
    plan_type = str(
        value.get("plan_type")
        or value.get("planType")
        or account.get("plan_type")
        or account.get("planType")
        or ""
    ).strip().lower()
    phone_status = str(
        value.get("codex_phone_status") or value.get("phone_status") or ""
    ).strip().lower()
    mail_api_url = str(
        value.get("mail_api_url")
        or value.get("icloud_api_url")
        or value.get("mailbox_api_url")
        or value.get("mail_api_base")
        or value.get("mailbox_url")
        or ""
    ).strip()
    mail_api_key = str(
        value.get("mail_api_key")
        or value.get("icloud_api_key")
        or value.get("mailbox_api_key")
        or ""
    ).strip()
    two_factor = str(
        value.get("two_factor")
        or value.get("two_fa")
        or value.get("2fa")
        or value.get("otp_secret")
        or value.get("twoFactor")
        or ""
    ).strip()

    if cookies or session_token:
        source_type = "session_token"
    elif access_token and refresh_token and not (password or refresh_token.startswith("M.")):
        source_type = "oauth_token"
    elif access_token and not (password or refresh_token):
        source_type = "access_token"
    else:
        source_type = "mailbox"
    record = {
        "source_type": source_type,
        "email": email,
        "password": password,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "access_token": access_token,
        "session_token": session_token,
        "cookies": cookies,
        "plan_type": plan_type,
        "codex_phone_status": phone_status,
        "provider": str(value.get("provider") or "").strip().lower(),
        "mail_api_url": mail_api_url,
        "mail_api_key": mail_api_key,
        "two_factor": two_factor,
    }
    if source_type == "oauth_token":
        record["oauth_credentials"] = {
            key: value
            for key, value in {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": value.get("id_token") or value.get("idToken") or credentials.get("id_token") or credentials.get("idToken") or "",
                "expires_at": value.get("expires_at") or value.get("expiresAt") or credentials.get("expires_at") or credentials.get("expiresAt") or "",
                "email": email,
                "chatgpt_account_id": value.get("chatgpt_account_id") or value.get("account_id") or "",
                "chatgpt_user_id": value.get("chatgpt_user_id") or value.get("user_id") or "",
                "plan_type": plan_type,
                "client_id": client_id,
            }.items()
            if value not in (None, "", [])
        }
    return record


def _from_token(raw: str) -> dict:
    value = str(raw or "").strip()
    value = re.sub(r"^(?:bearer|access_token|session_token|token)\s*[:=]\s*", "", value, flags=re.IGNORECASE)
    matches = list(COOKIE_RE.finditer(value))
    if matches:
        return _from_mapping({
            "cookies": [
                {"name": match.group("name"), "value": match.group("value")}
                for match in matches
            ]
        })
    if len(value) < 20:
        raise ValueError("unrecognized account or token record")
    kind = _token_kind(value)
    return _from_mapping({kind: value})


def _validate(record: dict) -> dict:
    source_type = record.get("source_type") or "mailbox"
    email = record.get("email") or ""
    if email and not EMAIL_RE.fullmatch(email):
        raise ValueError("invalid account email")
    if source_type in {"session_token", "access_token"}:
        if not record.get("session_token") and not record.get("access_token") and not record.get("cookies"):
            raise ValueError("token record is empty")
        return record
    if source_type == "oauth_token":
        if not record.get("access_token") or not record.get("refresh_token"):
            raise ValueError("OAuth token record requires access_token and refresh_token")
        return record
    if not email:
        raise ValueError("account record requires email")
    if record.get("mail_api_url"):
        if not re.match(r"^https?://[^\s]+$", str(record["mail_api_url"]), re.IGNORECASE):
            raise ValueError("mail API URL must start with http:// or https://")
        record["provider"] = "icloud"
    if record.get("refresh_token") and not record.get("client_id"):
        record["client_id"] = DEFAULT_GRAPH_CLIENT_ID
    if not record.get("password") and not record.get("refresh_token") and not is_icloud_email(email):
        raise ValueError("account record requires a password, Graph refresh token, or iCloud mailbox")
    if record.get("provider") not in {"", "outlook", "graph", "microsoft", "icloud"}:
        raise ValueError("unsupported mailbox provider")
    if not record.get("provider"):
        record["provider"] = "icloud" if is_icloud_email(email) else "outlook" if is_outlook_email(email) else "outlook"
    return record


def parse_account_line(line: str) -> dict:
    """Parse one mailbox, session-cookie, OAuth JSON, or raw token record."""
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        raise ValueError("empty account record")
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON account record") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON account record must be an object")
        return _validate(_from_mapping(parsed))
    if raw.startswith("["):
        raise ValueError("cookie JSON arrays must be supplied as one JSON document")
    fields = _split_fields(raw)
    if fields and EMAIL_RE.fullmatch(fields[0]):
        email = fields[0]
        if len(fields) == 1:
            return _validate(_from_mapping({"email": email}))
        if fields[1].lower().startswith(("http://", "https://")):
            mapping = {
                "email": email,
                "provider": "icloud",
                "mail_api_url": fields[1],
            }
            if len(fields) > 2:
                mapping["two_factor"] = fields[2]
            if len(fields) > 3:
                mapping["mail_api_key"] = fields[3]
            return _validate(_from_mapping(mapping))
        password = fields[1]
        third = fields[2] if len(fields) > 2 else ""
        fourth = fields[3] if len(fields) > 3 else ""
        if len(fields) == 3 and CLIENT_ID_RE.fullmatch(third) and password.startswith("M."):
            mapping = {"email": email, "refresh_token": password, "client_id": third}
        elif len(fields) == 3 and CLIENT_ID_RE.fullmatch(third):
            mapping = {"email": email, "password": password, "client_id": third}
        elif len(fields) == 3 and third.startswith("M."):
            mapping = {"email": email, "password": password, "refresh_token": third}
        elif len(fields) == 3 and CLIENT_ID_RE.fullmatch(fourth):
            mapping = {"email": email, "password": password, "refresh_token": third, "client_id": fourth}
        elif CLIENT_ID_RE.fullmatch(third) and not CLIENT_ID_RE.fullmatch(fourth):
            mapping = {"email": email, "password": password, "client_id": third, "refresh_token": fourth}
        elif CLIENT_ID_RE.fullmatch(fourth):
            mapping = {"email": email, "password": password, "refresh_token": third, "client_id": fourth}
        else:
            # Preserve arbitrary third/fourth fields as RT/client-id for custom
            # Outlook variants; validation supplies the default Graph client id.
            mapping = {"email": email, "password": password, "refresh_token": third, "client_id": fourth}
        return _validate(_from_mapping(mapping))
    return _validate(_from_token(raw))


def _whole_json_records(text: str):
    try:
        value = json.loads(str(text or "").lstrip("\ufeff"))
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(value, dict):
        return [_validate(_from_mapping(value))]
    if isinstance(value, list):
        if value and all(isinstance(item, dict) and item.get("name") and item.get("value") for item in value):
            return [_validate(_from_mapping({"cookies": value}))]
        return [_validate(_from_mapping(item)) for item in value if isinstance(item, dict)]
    return None


def _identity(record: dict) -> str:
    if record.get("email"):
        return f"email:{record['email'].lower()}"
    payload = json.dumps(
        {key: record.get(key) for key in ("source_type", "access_token", "session_token", "cookies")},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return "token:" + hashlib.sha256(payload).hexdigest()


def parse_account_text(text: str) -> tuple[list[dict], list[dict]]:
    records = []
    errors = []
    seen = set()
    try:
        whole = _whole_json_records(text)
    except ValueError as exc:
        return [], [{"line": 1, "error": str(exc)}]
    if whole is not None:
        candidates = [(index, item) for index, item in enumerate(whole, start=1)]
    else:
        candidates = []
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                index += 1
                continue
            # iCloud batch records may be supplied as a three-line block:
            # email, mailbox API URL, and the per-mailbox 2FA/API token.
            if (
                EMAIL_RE.fullmatch(line.strip())
                and index + 1 < len(lines)
                and re.match(r"^https?://[^\s]+$", lines[index + 1].strip(), re.IGNORECASE)
            ):
                record_line = index + 1
                third_index = index + 2
                while third_index < len(lines) and not lines[third_index].strip():
                    third_index += 1
                mapping = {
                    "email": line.strip(),
                    "provider": "icloud",
                    "mail_api_url": lines[index + 1].strip(),
                }
                if (
                    third_index < len(lines)
                    and not lines[third_index].lstrip().startswith("#")
                    and not EMAIL_RE.fullmatch(lines[third_index].strip())
                ):
                    mapping["two_factor"] = lines[third_index].strip()
                    index = third_index + 1
                else:
                    index = index + 2
                try:
                    candidates.append((record_line, _validate(_from_mapping(mapping))))
                except ValueError as exc:
                    errors.append({"line": record_line, "error": str(exc)})
                continue
            try:
                candidates.append((index + 1, parse_account_line(line)))
            except ValueError as exc:
                errors.append({"line": index + 1, "error": str(exc)})
            index += 1
    for line_number, record in candidates:
        key = _identity(record)
        if key in seen:
            errors.append({"line": line_number, "error": "duplicate account identity"})
            continue
        seen.add(key)
        records.append(record)
    return records, errors


def canonical_account_line(record: dict) -> str:
    if record.get("source_type") != "mailbox":
        payload = {
            key: record.get(key)
            for key in (
                "source_type", "email", "password", "refresh_token", "client_id",
                "access_token", "session_token", "cookies", "plan_type", "codex_phone_status",
                "mail_api_url", "mail_api_key", "two_factor",
            )
            if record.get(key) not in (None, "", [], {})
        }
        if record.get("oauth_credentials"):
            payload.update(record["oauth_credentials"])
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if record.get("mail_api_key"):
        payload = {
            key: record.get(key)
            for key in ("email", "provider", "mail_api_url", "mail_api_key", "two_factor")
            if record.get(key) not in (None, "", [], {})
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if record.get("mail_api_url") or record.get("two_factor"):
        return "\n".join(
            str(record.get(key) or "").strip()
            for key in ("email", "mail_api_url", "two_factor")
        )
    fields = [
        str(record.get("email") or "").strip(),
        str(record.get("password") or "").strip(),
        str(record.get("refresh_token") or "").strip(),
        str(record.get("client_id") or "").strip(),
    ]
    while len(fields) > 2 and not fields[-1]:
        fields.pop()
    return "----".join(fields)


def masked_email(email: str) -> str:
    local, separator, domain = str(email or "").partition("@")
    if not separator:
        return "token:***"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"
