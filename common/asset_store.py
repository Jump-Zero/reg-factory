"""Read registered mailboxes and platform credentials through a stable local API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from common.session_export import (
    build_chatgpt2api_account,
    build_cpa_codex_json,
    build_sub2api_content,
    sub2api_expires_at,
)


class AssetError(Exception):
    status_code = 400


class AssetNotFound(AssetError):
    status_code = 404


class AssetExhausted(AssetError):
    status_code = 404


class AssetUnverified(AssetError):
    status_code = 409


_CURSOR_LOCK = threading.Lock()
_RECORD_CACHE_LOCK = threading.Lock()
_RECORD_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_RECORD_CACHE_SECONDS = 5.0
_PLATFORMS = {
    "claude": {
        "key_names": {"sessionKey"},
        "domains": {"claude.ai"},
    },
    "chatgpt": {
        "key_names": {"__Secure-next-auth.session-token"},
        "domains": {"chatgpt.com", "openai.com"},
    },
    "grok": {
        "key_names": {"sso", "sso-rw", "__Secure-next-auth.session-token"},
        "domains": {"grok.com", "x.ai"},
    },
    "kiro": {
        "key_names": set(),
        "domains": set(),
    },
}

EMAIL_PROVIDERS = ("outlook", "icloud", "temporary", "other")
LIFECYCLE_BUCKETS = ("exported", "quarantine")
QUARANTINE_STATUSES = ("banned", "expired", "invalid")
_OUTLOOK_EMAIL_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
_ICLOUD_EMAIL_DOMAINS = {"icloud.com", "me.com", "mac.com"}
_TEMPORARY_EMAIL_MARKERS = (
    "10minutemail", "10minutemail", "guerrillamail", "mailinator", "mail.tm",
    "temp-mail", "tempmail", "yopmail", "sharklasers", "getnada", "inboxbear",
    "dropmail", "moakt", "emailondeck", "minuteinbox", "mohmal",
)


def classify_email_provider(email: str) -> str:
    """Classify an account's registration mailbox without retaining credentials."""
    value = str(email or "").strip().lower()
    domain = value.rsplit("@", 1)[-1] if "@" in value else ""
    if (
        domain in _OUTLOOK_EMAIL_DOMAINS
        or domain.startswith(("outlook.", "hotmail.", "live.", "msn."))
    ):
        return "outlook"
    if domain in _ICLOUD_EMAIL_DOMAINS:
        return "icloud"
    if any(marker in domain for marker in _TEMPORARY_EMAIL_MARKERS):
        return "temporary"
    return "other"


def _data_root() -> Path:
    return Path(os.environ.get("REG_FACTORY_DATA_DIR") or Path.cwd()).resolve()


def _token_root() -> Path:
    configured = os.environ.get("TOKEN_OUTPUT_DIR", "").strip()
    if not configured:
        env_path = Path(os.environ.get("REG_FACTORY_ENV_FILE") or _data_root() / ".env")
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and line.partition("=")[0].strip() == "TOKEN_OUTPUT_DIR":
                    configured = line.partition("=")[2].strip().strip('"').strip("'")
                    break
    configured = configured or "tokens"
    path = Path(configured)
    return path.resolve() if path.is_absolute() else (_data_root() / path).resolve()


def _cursor_path() -> Path:
    return _data_root() / "runtime" / "state" / "asset_api_cursors.json"


def _claim_path() -> Path:
    return _data_root() / "runtime" / "state" / "asset_api_claims.json"


def _lifecycle_manifest_path() -> Path:
    return _data_root() / "runtime" / "state" / "asset_lifecycle.jsonl"


def _lifecycle_root(bucket: str) -> Path:
    normalized = str(bucket or "").strip().lower()
    if normalized not in LIFECYCLE_BUCKETS:
        raise AssetError(f"asset lifecycle bucket must be one of: {', '.join(LIFECYCLE_BUCKETS)}")
    return _data_root() / "runtime" / "assets" / normalized


def _outlook_sale_exclusion_path() -> Path:
    return _data_root() / "runtime" / "state" / "outlook_sale_emails.txt"


def _outlook_registration_exclusion_path() -> Path:
    return _data_root() / "runtime" / "state" / "outlook_registration_emails.txt"


def _exclude_outlook_sale_from_registration(email: str) -> None:
    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        raise AssetError("Outlook 资产缺少可用于平台注册排除的邮箱")
    with _CURSOR_LOCK:
        path = _outlook_sale_exclusion_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            line.strip().lower()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        } if path.is_file() else set()
        if normalized not in existing:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{normalized}\n")


def _read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _cached_records(key: tuple, loader) -> list[dict]:
    now = time.monotonic()
    with _RECORD_CACHE_LOCK:
        cached = _RECORD_CACHE.get(key)
        if cached and cached[0] > now:
            return list(cached[1])
    records = loader()
    with _RECORD_CACHE_LOCK:
        _RECORD_CACHE[key] = (now + _RECORD_CACHE_SECONDS, list(records))
    return list(records)


def invalidate_record_cache(platform: str = "") -> None:
    normalized = str(platform or "").strip().lower()
    with _RECORD_CACHE_LOCK:
        if not normalized:
            _RECORD_CACHE.clear()
            return
        for key in list(_RECORD_CACHE):
            if normalized in key:
                _RECORD_CACHE.pop(key, None)


def _invalidate_scanner_report() -> None:
    try:
        from common import asset_scanner

        callback = getattr(asset_scanner, "invalidate_report_cache", None)
        if callback:
            callback()
    except Exception:
        pass


def _read_cursors() -> dict[str, int]:
    try:
        value = _read_json(_cursor_path())
        if isinstance(value, dict):
            return {str(key): max(0, int(index)) for key, index in value.items()}
    except Exception:
        pass
    return {}


def _write_cursors(value: dict[str, int]) -> None:
    path = _cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_claims() -> dict[str, set[str]]:
    try:
        value = _read_json(_claim_path())
        scopes = value.get("scopes", {}) if isinstance(value, dict) else {}
        if isinstance(scopes, dict):
            return {
                str(scope): {str(claim) for claim in claims if str(claim)}
                for scope, claims in scopes.items()
                if isinstance(claims, list)
            }
    except Exception:
        pass
    return {}


def _write_claims(value: dict[str, set[str]]) -> None:
    path = _claim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    payload = {
        "version": 1,
        "scopes": {
            scope: sorted(claims)
            for scope, claims in sorted(value.items())
            if claims
        },
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _lifecycle_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _relative_asset_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(_data_root()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _append_lifecycle_event(event: dict) -> None:
    path = _lifecycle_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **event,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _mapping_email(value) -> str:
    if not isinstance(value, dict):
        return ""
    user = value.get("user") if isinstance(value.get("user"), dict) else {}
    credentials = value.get("credentials") if isinstance(value.get("credentials"), dict) else {}
    return str(
        value.get("email")
        or value.get("username")
        or user.get("email")
        or credentials.get("email")
        or ""
    ).strip().lower()


def _move_paths(paths: list[Path], bucket: str, platform: str, stamp: str) -> list[str]:
    destination = _lifecycle_root(bucket) / platform / stamp
    moved = []
    for source in paths:
        if not source.is_file():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / source.name
        suffix = 1
        while target.exists():
            target = destination / f"{source.stem}-{suffix}{source.suffix}"
            suffix += 1
        source.replace(target)
        moved.append(_relative_asset_path(target))
    return moved


def _platform_asset_paths(platform: str, email: str = "", source: str = "") -> list[Path]:
    normalized_email = str(email or "").strip().lower()
    source_names = {
        item.strip()
        for item in re.split(r"\s*,\s*", str(source or ""))
        if item.strip()
    }
    paths: dict[str, Path] = {}

    for record in _cookie_records(platform):
        record_email = str(record.get("email") or "").strip().lower()
        path = record["path"]
        if path.name in source_names or (normalized_email and record_email == normalized_email):
            paths[str(path.resolve()).lower()] = path

    if platform in {"chatgpt", "grok", "kiro"}:
        for record in _token_records(platform):
            path = record["path"]
            record_email = _email_from_session(
                record["data"], path.stem.replace(".session", "")
            ).strip().lower()
            if path.name in source_names or (normalized_email and record_email == normalized_email):
                paths[str(path.resolve()).lower()] = path

    # ChatGPT exports several per-account companion JSON files beside the
    # canonical session. Move all companions that identify the same mailbox.
    directory = _token_root() / platform
    if normalized_email and directory.is_dir():
        for path in directory.glob("*.json"):
            if str(path.resolve()).lower() in paths:
                continue
            try:
                data = _read_json(path)
            except Exception:
                continue
            if _mapping_email(data) == normalized_email:
                paths[str(path.resolve()).lower()] = path

    return sorted(paths.values(), key=lambda item: str(item).lower())


def move_platform_asset(
    platform: str,
    email: str = "",
    source: str = "",
    bucket: str = "exported",
    reason: str = "",
) -> dict:
    """Move one logical platform account out of the active pool, recoverably."""
    platform = str(platform or "").strip().lower()
    if platform not in _PLATFORMS:
        raise AssetError("unsupported platform lifecycle target")
    stamp = _lifecycle_timestamp()
    with _CURSOR_LOCK:
        moved = _move_paths(
            _platform_asset_paths(platform, email=email, source=source),
            bucket,
            platform,
            stamp,
        )
        if moved:
            _append_lifecycle_event({
                "bucket": bucket,
                "platform": platform,
                "email": str(email or "").strip().lower(),
                "source": str(source or ""),
                "reason": str(reason or ""),
                "files": moved,
            })
    if moved:
        invalidate_record_cache(platform)
        _invalidate_scanner_report()
    return {"platform": platform, "email": str(email or "").strip(), "moved": moved}


def move_mailbox_asset(
    email: str,
    bucket: str = "exported",
    reason: str = "",
) -> dict:
    """Move matching four-field mailbox lines out of ``emails.txt``."""
    normalized = str(email or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise AssetError("mailbox lifecycle target requires an email")
    source_path = _data_root() / "emails.txt"
    stamp = _lifecycle_timestamp()
    moved_lines = []
    with _CURSOR_LOCK:
        if source_path.is_file():
            raw_lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
            kept = []
            for raw in raw_lines:
                candidate = raw.lstrip("\ufeff").strip().split("----", 1)[0].strip().lower()
                if candidate == normalized:
                    moved_lines.append(raw.lstrip("\ufeff"))
                else:
                    kept.append(raw)
            if moved_lines:
                temporary = source_path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(
                    ("\n".join(kept) + "\n") if kept else "",
                    encoding="utf-8",
                )
                temporary.replace(source_path)
                destination = _lifecycle_root(bucket) / "outlook" / stamp / "emails.txt"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("\n".join(moved_lines) + "\n", encoding="utf-8")
                relative = _relative_asset_path(destination)
                _append_lifecycle_event({
                    "bucket": bucket,
                    "platform": "outlook",
                    "email": normalized,
                    "reason": str(reason or ""),
                    "files": [relative],
                })
            else:
                relative = ""
        else:
            relative = ""
    if moved_lines:
        invalidate_record_cache()
        _invalidate_scanner_report()
    return {
        "platform": "outlook",
        "email": normalized,
        "moved": [relative] if relative else [],
        "lines": len(moved_lines),
    }


def _platform_asset_index(platform: str) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    by_email: dict[str, dict[str, Path]] = {}
    by_source: dict[str, dict[str, Path]] = {}

    def add(path: Path, email: str = "") -> None:
        resolved = str(path.resolve()).lower()
        by_source.setdefault(path.name, {})[resolved] = path
        normalized = str(email or "").strip().lower()
        if normalized:
            by_email.setdefault(normalized, {})[resolved] = path

    for record in _cookie_records(platform):
        add(record["path"], record.get("email", ""))
    if platform in {"chatgpt", "grok", "kiro"}:
        for record in _token_records(platform):
            path = record["path"]
            add(path, _email_from_session(record["data"], path.stem.replace(".session", "")))
    directory = _token_root() / platform
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                add(path, _mapping_email(_read_json(path)))
            except Exception:
                continue
    return (
        {email: list(paths.values()) for email, paths in by_email.items()},
        {source: list(paths.values()) for source, paths in by_source.items()},
    )


def archive_asset_results(results: list[dict], bucket: str = "exported", reason: str = "") -> dict:
    """Move a batch out of active inventory using one index per platform."""
    _lifecycle_root(bucket)
    grouped: dict[str, list[dict]] = {}
    seen = set()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        platform = str(
            result.get("platform") or ("outlook" if result.get("kind") == "email" else "")
        ).strip().lower()
        email = str(result.get("email") or "").strip().lower()
        source = str(result.get("source") or "")
        identity = (platform, email or source.lower())
        if not platform or identity in seen:
            continue
        seen.add(identity)
        grouped.setdefault(platform, []).append({"email": email, "source": source})

    details = []
    for item in grouped.pop("outlook", []):
        detail = move_mailbox_asset(item["email"], bucket=bucket, reason=reason)
        if detail.get("moved"):
            details.append(detail)

    for platform, items in grouped.items():
        by_email, by_source = _platform_asset_index(platform)
        moved_for_platform = False
        with _CURSOR_LOCK:
            for item in items:
                paths: dict[str, Path] = {}
                for path in by_email.get(item["email"], []):
                    paths[str(path.resolve()).lower()] = path
                for source_name in re.split(r"\s*,\s*", item["source"]):
                    for path in by_source.get(source_name.strip(), []):
                        paths[str(path.resolve()).lower()] = path
                moved = _move_paths(
                    sorted(paths.values(), key=lambda path: str(path).lower()),
                    bucket,
                    platform,
                    _lifecycle_timestamp(),
                )
                if not moved:
                    continue
                moved_for_platform = True
                detail = {"platform": platform, "email": item["email"], "moved": moved}
                details.append(detail)
                _append_lifecycle_event({
                    "bucket": bucket,
                    "platform": platform,
                    "email": item["email"],
                    "source": item["source"],
                    "reason": str(reason or ""),
                    "files": moved,
                })
        if moved_for_platform:
            invalidate_record_cache(platform)

    if details:
        _invalidate_scanner_report()
    return {
        "bucket": bucket,
        "moved_accounts": len(details),
        "moved_files": sum(len(detail["moved"]) for detail in details),
        "details": details,
    }


def quarantine_scan_report(
    report: dict,
    statuses: tuple[str, ...] | list[str] = QUARANTINE_STATUSES,
) -> dict:
    selected_statuses = {
        str(status or "").strip().lower() for status in statuses if str(status or "").strip()
    }
    invalid = selected_statuses.difference(QUARANTINE_STATUSES)
    if invalid:
        raise AssetError(f"unsupported automatic quarantine status: {', '.join(sorted(invalid))}")
    candidates = [
        item for item in (report or {}).get("items", [])
        if isinstance(item, dict) and str(item.get("status") or "").lower() in selected_statuses
    ]
    result = archive_asset_results(candidates, bucket="quarantine", reason="asset_scan")
    result["statuses"] = sorted(selected_statuses)
    result["candidates"] = len(candidates)
    return result


def lifecycle_summary() -> dict:
    counts = {bucket: {"accounts": 0, "files": 0} for bucket in LIFECYCLE_BUCKETS}
    path = _lifecycle_manifest_path()
    if not path.is_file():
        return counts
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        bucket = str(event.get("bucket") or "") if isinstance(event, dict) else ""
        if bucket not in counts:
            continue
        counts[bucket]["accounts"] += 1
        files = event.get("files") if isinstance(event.get("files"), list) else []
        counts[bucket]["files"] += len(files)
    return counts


def _claim_id(scope: str, identity: str) -> str:
    value = f"{scope}\0{str(identity or '').strip().lower()}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _asset_identity(email: str, source: str) -> str:
    normalized_email = str(email or "").strip().lower()
    if normalized_email:
        return f"email:{normalized_email}"
    return f"source:{str(source or '').strip().lower()}"


def _claim_record(
    records: list[dict],
    scope: str,
    identity_for,
    index: int | None,
) -> tuple[int, dict, int, int, bool]:
    """Atomically select and claim one account across all output formats."""
    with _CURSOR_LOCK:
        claims = _read_claims()
        claimed = set(claims.get(scope, set()))
        candidates = []
        seen = set(claimed)
        for record in records:
            claim = _claim_id(scope, identity_for(record))
            if claim in seen:
                continue
            seen.add(claim)
            candidates.append((record, claim))

        total = len(candidates)
        if total <= 0:
            raise AssetExhausted("没有未领取的资产；如需重新使用，请重置领取记录")
        selected = 0 if index is None else index
        if selected < 0 or selected >= total:
            raise AssetNotFound(f"index 超出未领取资产范围：{selected}，可用范围 0-{total - 1}")

        record, claim = candidates[selected]
        claimed.add(claim)
        claims[scope] = claimed
        _write_claims(claims)
    return selected, record, total, total - 1, index is None


def _select_index(total: int, cursor_key: str, index: int | None) -> tuple[int, int, bool]:
    if total <= 0:
        raise AssetNotFound("没有可读取的资产")
    if index is not None:
        if index < 0 or index >= total:
            raise AssetNotFound(f"index 超出范围：{index}，可用范围 0-{total - 1}")
        return index, index + 1, False
    with _CURSOR_LOCK:
        cursors = _read_cursors()
        selected = int(cursors.get(cursor_key, 0))
        if selected >= total:
            raise AssetExhausted(f"顺序游标已取完：{selected}/{total}；请指定 index 或重置游标")
        cursors[cursor_key] = selected + 1
        _write_cursors(cursors)
    return selected, selected + 1, True


def _claim_scope(scope: str) -> str:
    if scope in {"outlook", *_PLATFORMS}:
        return scope
    if scope in {"email", "verified:email"}:
        return "outlook"
    parts = scope.split(":")
    for platform in _PLATFORMS:
        if platform in parts:
            return platform
    return ""


def reset_cursor(scope: str = "all") -> dict:
    normalized = str(scope or "all").strip().lower()
    with _CURSOR_LOCK:
        cursors = _read_cursors()
        claims = _read_claims()
        if normalized == "all":
            removed = sorted(cursors)
            cursors = {}
            claims_removed = sum(len(items) for items in claims.values())
            claim_scopes_removed = sorted(claims)
            claims = {}
        else:
            removed = [normalized] if normalized in cursors else []
            cursors.pop(normalized, None)
            claim_scope = _claim_scope(normalized)
            claim_scopes_removed = [claim_scope] if claim_scope in claims else []
            claims_removed = len(claims.pop(claim_scope, set())) if claim_scope else 0
        _write_cursors(cursors)
        _write_claims(claims)
    return {
        "scope": normalized,
        "removed": removed,
        "remaining": cursors,
        "claim_scopes_removed": claim_scopes_removed,
        "claims_removed": claims_removed,
        "remaining_claims": {key: len(value) for key, value in sorted(claims.items())},
    }


def _mailboxes() -> list[dict]:
    path = _data_root() / "emails.txt"
    if not path.is_file():
        return []
    records = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        records.append({
            "email": parts[0].strip(),
            "email_provider": classify_email_provider(parts[0].strip()),
            "password": parts[1].strip() if len(parts) > 1 else "",
            "refresh_token": parts[2].strip() if len(parts) > 2 else "",
            "client_id": parts[3].strip() if len(parts) > 3 else "",
            "line": line,
        })
    return records


def _mailbox_map() -> dict[str, dict]:
    return {
        str(record.get("email") or "").strip().lower(): record
        for record in _mailboxes()
        if str(record.get("email") or "").strip()
    }


def _mailbox_four_line(record: dict) -> str:
    return "----".join(
        str(record.get(key) or "").strip()
        for key in ("email", "password", "refresh_token", "client_id")
    )


def _no_graph_mailboxes() -> list[dict]:
    path = _data_root() / "outlook_no_graph.txt"
    if not path.is_file():
        return []
    records = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            continue
        email = parts[0].strip()
        password = parts[1].strip()
        records.append({
            "email": email,
            "email_provider": classify_email_provider(email),
            "password": password,
            "refresh_token": "",
            "client_id": "",
            "line": f"{email}----{password}",
        })
    return records


def registered_mailbox_usage() -> dict[str, tuple[str, ...]]:
    """Return mailboxes that were reserved or attempted for another platform."""
    usage: dict[str, set[str]] = {}

    def record(email: str, platform: str) -> None:
        normalized = str(email or "").lstrip("\ufeff").strip().lower()
        if "@" in normalized:
            usage.setdefault(normalized, set()).add(platform)

    root = _data_root()
    registration_path = _outlook_registration_exclusion_path()
    if registration_path.is_file():
        for raw in registration_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            record(raw.strip(), "registration")
    for pattern, prefix in (
        ("emails_used_*.txt", "emails_used_"),
        ("emails_error_*.txt", "emails_error_"),
    ):
        for path in sorted(root.glob(pattern)):
            platform = path.stem.removeprefix(prefix).strip().lower()
            if not platform or platform in {"email", "outlook"}:
                continue
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                email = line.split("----", 1)[0].strip().lower()
                record(email, platform)

    # Successful explicit-account registrations may not pass through an email
    # reservation ledger. Treat their stored tokens and cookies as definitive
    # evidence that the mailbox is no longer pristine.
    for platform in _PLATFORMS:
        for item in _cookie_records(platform):
            record(item.get("email", ""), platform)
        for item in _token_records(platform):
            record(_email_from_session(item.get("data", {})), platform)
    claude_tokens = _token_root() / "claude"
    for path in claude_tokens.glob("*.sessionKey.json") if claude_tokens.is_dir() else ():
        try:
            data = _read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            record(_email_from_session(data, path.stem.split(".")[0]), "claude")

    return {
        email: tuple(sorted(platforms))
        for email, platforms in sorted(usage.items())
    }


def _verification_payload(platform: str, item: dict) -> dict:
    verification = {
        "status": "normal",
        "checked_at": str(item.get("checked_at") or ""),
        "evidence": str(item.get("evidence") or ""),
    }
    if platform == "chatgpt":
        verification.update({
            "plus_trial": str(item.get("plus_trial") or "unknown"),
            "plus_trial_detail": str(item.get("plus_trial_detail") or ""),
            "plus_trial_evidence": str(item.get("plus_trial_evidence") or ""),
            "registration_country": str(item.get("registration_country") or ""),
            "network_node": str(item.get("network_node") or ""),
        })
    return verification


def _verification_indexes(platform: str) -> tuple[dict[str, dict], dict[str, dict]]:
    from common import asset_scanner

    by_email = {}
    by_source = {}
    for item in asset_scanner.get_report().get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("platform") != platform or item.get("status") != "normal":
            continue
        item_email = str(item.get("email") or "").strip().lower()
        verification = _verification_payload(platform, item)
        if item_email:
            by_email[item_email] = verification
        for source in str(item.get("source") or "").split(","):
            normalized_source = source.strip()
            if normalized_source:
                by_source[normalized_source] = verification
    return by_email, by_source


def _verified_records(platform: str, records: list[dict], source_for) -> list[dict]:
    by_email, by_source = _verification_indexes(platform)
    verified = []
    for record in records:
        email = str(record.get("email") or "").strip()
        if not email and isinstance(record.get("data"), dict):
            email = _email_from_session(record["data"])
        verification = by_email.get(email.lower()) or by_source.get(str(source_for(record)).strip())
        if verification:
            verified.append({**record, "_verification": verification})
    if not verified:
        raise AssetUnverified("最近一次号池扫描中没有状态为正常的可领取资产；请先手动扫描，或取消仅正常筛选")
    return verified


def get_email(
    index: int | None = None,
    output_format: str = "json",
    verified_only: bool = False,
    claim_once: bool = False,
    email_provider: str = "",
    pristine_only: bool = False,
    no_graph_only: bool = False,
    _defer_claim: bool = False,
) -> dict:
    output_format = str(output_format or "json").strip().lower()
    if output_format not in {"json", "line", "four", "password"}:
        raise AssetError("邮箱 format 仅支持 json、line、four、password")
    provider_filter = str(email_provider or "").strip().lower()
    if provider_filter and provider_filter not in EMAIL_PROVIDERS:
        raise AssetError("email_provider must be outlook, icloud, temporary, or other")
    records = [
        {**record, "_asset_source": f"emails.txt:{line_number}"}
        for line_number, record in enumerate(_mailboxes(), start=1)
    ]
    if no_graph_only:
        records = [
            {**record, "_asset_source": f"outlook_no_graph.txt:{line_number}"}
            for line_number, record in enumerate(_no_graph_mailboxes(), start=1)
        ]
    if provider_filter:
        records = [record for record in records if record.get("email_provider") == provider_filter]
    registered = registered_mailbox_usage()
    records = [
        record for record in records
        if str(record.get("email") or "").strip().lower() not in registered
    ]
    if not records and registered:
        raise AssetNotFound("没有可单独售卖的邮箱；号池邮箱已被平台注册或尝试使用")
    if no_graph_only and verified_only:
        raise AssetError("no_graph_only 不能与 normal_only 同时使用")
    if verified_only:
        records = _verified_records("outlook", records, lambda record: record["_asset_source"])
    should_claim = (verified_only or claim_once) and not _defer_claim
    if should_claim:
        selected, record, total, remaining, advanced = _claim_record(
            records,
            "outlook",
            lambda record: _asset_identity(record.get("email", ""), record["_asset_source"]),
            index,
        )
        next_index = 0 if remaining else None
    else:
        selected, next_index, advanced = _select_index(len(records), "email", index)
        record = records[selected]
        total = len(records)
    if output_format == "four":
        data = _mailbox_four_line(record)
    elif output_format in {"line", "password"}:
        data = record["line"]
    else:
        data = {
            key: value for key, value in record.items()
            if key != "line" and not key.startswith("_")
        }
    result = {
        "kind": "email",
        "format": output_format,
        "index": selected,
        "total": total,
        "next_index": next_index,
        "cursor_advanced": advanced,
        "email": record.get("email", ""),
        "source": record.get("_asset_source", ""),
        "data": data,
        "email_provider": record.get("email_provider") or classify_email_provider(record.get("email", "")),
    }
    if verified_only:
        result["verification"] = record["_verification"]
    if should_claim:
        result.update({
            "claim_recorded": True,
            "claim_scope": "outlook",
            "remaining": remaining,
        })
    if pristine_only:
        if should_claim:
            _exclude_outlook_sale_from_registration(record.get("email", ""))
        result["pristine"] = True
    if no_graph_only:
        if should_claim:
            _exclude_outlook_sale_from_registration(record.get("email", ""))
        result["no_graph_only"] = True
    return result


def _domain_allowed(domain: str, allowed: set[str]) -> bool:
    normalized = str(domain or "").lstrip(".").lower()
    return any(normalized == item or normalized.endswith(f".{item}") for item in allowed)


def _cookie_directories(platform: str) -> list[Path]:
    root = _data_root() / "cookies"
    directories = [root / platform]
    if platform == "claude":
        directories.append(root)
    return directories


def _account_map(directory: Path) -> dict[str, str]:
    path = directory / "accounts.txt"
    result = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.strip().split("|")
        if len(parts) >= 3 and parts[0] and parts[2]:
            result[parts[2]] = parts[0]
    return result


def _cookie_records(platform: str) -> list[dict]:
    def load() -> list[dict]:
        config = _PLATFORMS[platform]
        records = []
        seen_paths = set()
        for directory in _cookie_directories(platform):
            accounts = _account_map(directory)
            paths = directory.glob("full_*.json") if directory.is_dir() else ()
            for path in paths:
                resolved = str(path.resolve()).lower()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                try:
                    raw_cookies = _read_json(path)
                except Exception:
                    continue
                if not isinstance(raw_cookies, list):
                    continue
                cookies = [
                    item for item in raw_cookies
                    if isinstance(item, dict) and _domain_allowed(item.get("domain", ""), config["domains"])
                ]
                key_cookie = next(
                    (item for item in cookies if item.get("name") in config["key_names"] and item.get("value")),
                    None,
                )
                if not key_cookie:
                    continue
                records.append({
                    "path": path,
                    "email": accounts.get(str(key_cookie["value"]), ""),
                    "email_provider": classify_email_provider(accounts.get(str(key_cookie["value"]), "")),
                    "cookies": cookies,
                })
        return sorted(records, key=lambda item: (item["path"].stat().st_mtime, str(item["path"]).lower()))

    key = ("cookies", str(_data_root()).lower(), platform)
    return _cached_records(key, load)


def _token_records(platform: str) -> list[dict]:
    directory = _token_root() / platform
    pattern = "*.session.json" if platform == "chatgpt" else "*.account.json" if platform == "kiro" else "*.sso.json"

    def load() -> list[dict]:
        records = []
        paths = directory.glob(pattern) if directory.is_dir() else ()
        for path in paths:
            try:
                data = _read_json(path)
            except Exception:
                continue
            if isinstance(data, dict):
                records.append({"path": path, "data": data})
        return sorted(records, key=lambda item: (item["path"].stat().st_mtime, str(item["path"]).lower()))

    key = ("tokens", str(directory.resolve()).lower(), platform, pattern)
    return _cached_records(key, load)


def _cookie_header(cookies: list[dict]) -> str:
    return "; ".join(
        f"{item.get('name')}={item.get('value')}"
        for item in cookies if item.get("name") and item.get("value") is not None
    )


def _standard_cookie(cookie: dict) -> dict:
    """Convert a stored Playwright cookie to a browser-extension import record."""
    same_site = {
        "none": "no_restriction",
        "no_restriction": "no_restriction",
        "lax": "lax",
        "strict": "strict",
        "unspecified": "unspecified",
    }.get(str(cookie.get("sameSite") or "").strip().lower(), "unspecified")
    secure = bool(cookie.get("secure", False))
    if same_site == "no_restriction":
        secure = True

    expiration = cookie.get("expirationDate", cookie.get("expires"))
    try:
        expiration = float(expiration)
        if expiration <= 0:
            expiration = None
    except (TypeError, ValueError):
        expiration = None

    domain = str(cookie.get("domain") or "")
    result = {
        "domain": domain,
        "hostOnly": bool(cookie.get("hostOnly", not domain.startswith("."))),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "name": str(cookie.get("name") or ""),
        "path": str(cookie.get("path") or "/"),
        "sameSite": same_site,
        "secure": secure,
        "session": bool(cookie.get("session", expiration is None)),
        "storeId": str(cookie.get("storeId", "0")),
        "value": str(cookie.get("value") or ""),
    }
    if expiration is not None:
        result["expirationDate"] = expiration
        result["session"] = False
    return result


def _email_from_session(session: dict, fallback: str = "") -> str:
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    return str(user.get("email") or session.get("email") or fallback).strip()


def _email_provider_from_session(session: dict, fallback: str = "") -> str:
    explicit = str(session.get("email_provider") or "").strip().lower() if isinstance(session, dict) else ""
    if explicit in EMAIL_PROVIDERS:
        return explicit
    return classify_email_provider(_email_from_session(session, fallback))


def _chatgpt_registration_mailbox_map(records: list[dict]) -> dict[str, dict]:
    """Merge static Outlook mailboxes with dynamic iCloud registrations."""
    mailboxes = _mailbox_map()
    account_passwords = {}
    for directory in _cookie_directories("chatgpt"):
        path = directory / "accounts.txt"
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.strip().split("|", 2)
            if len(parts) >= 2 and parts[0].strip():
                account_passwords[parts[0].strip().lower()] = parts[1].strip()

    for record in records:
        session = record["data"]
        fallback = record["path"].stem.replace(".session", "")
        email = _email_from_session(session, fallback)
        normalized = email.strip().lower()
        provider = _email_provider_from_session(session, fallback)
        if not normalized or normalized in mailboxes or provider != "icloud":
            continue
        mailboxes[normalized] = {
            "email": email,
            "email_provider": "icloud",
            "password": account_passwords.get(normalized, ""),
            "refresh_token": "",
            "client_id": "",
        }
    return mailboxes


def get_platform_asset(
    platform: str,
    output_format: str = "raw",
    index: int | None = None,
    verified_only: bool = False,
    claim_once: bool = False,
    codex_phone_status: str = "",
    email_provider: str = "",
    _defer_claim: bool = False,
) -> dict:
    platform = str(platform or "").strip().lower()
    output_format = str(output_format or "raw").strip().lower()
    if platform not in _PLATFORMS:
        raise AssetError("platform 仅支持 claude、chatgpt、grok、kiro")
    phone_filter = str(codex_phone_status or "").strip().lower()
    if phone_filter not in {"", "verified", "not_verified"}:
        raise AssetError("codex_phone_status 仅支持 verified 或 not_verified")
    if phone_filter and platform != "chatgpt":
        raise AssetError("codex_phone_status 只适用于 ChatGPT")

    provider_filter = str(email_provider or "").strip().lower()
    if provider_filter and provider_filter not in EMAIL_PROVIDERS:
        raise AssetError("email_provider must be outlook, icloud, temporary, or other")

    token_formats = {"session", "sub2api", "cpa", "chatgpt2api", "email_four"}
    should_claim = (verified_only or claim_once) and not _defer_claim
    if output_format in {"raw", "cookies", "header"}:
        records = _cookie_records(platform)
        if provider_filter:
            records = [record for record in records if record.get("email_provider") == provider_filter]
        if phone_filter == "verified":
            records = []
        if verified_only:
            records = _verified_records(platform, records, lambda record: record["path"].name)
        if should_claim:
            selected, record, total, remaining, advanced = _claim_record(
                records,
                platform,
                lambda record: _asset_identity(record.get("email", ""), record["path"].name),
                index,
            )
            next_index = 0 if remaining else None
        else:
            cursor_key = f"cookie:{platform}:{output_format}"
            selected, next_index, advanced = _select_index(len(records), cursor_key, index)
            record = records[selected]
            total = len(records)
        if output_format == "raw":
            data = record["cookies"]
        elif output_format == "cookies":
            data = [_standard_cookie(cookie) for cookie in record["cookies"]]
        else:
            data = _cookie_header(record["cookies"])
        email = record["email"]
        source = record["path"].name
        extra = {}
    elif output_format in token_formats:
        if platform == "claude":
            raise AssetError("Claude 不支持 session、sub2api、cpa 或 chatgpt2api 格式，请使用 cookies/raw/header")
        if platform == "grok" and output_format not in {"session", "sub2api"}:
            raise AssetError("Grok 仅支持 cookies、raw、header、session、sub2api 格式")
        if output_format == "email_four" and platform != "chatgpt":
            raise AssetError("email_four 仅适用于 ChatGPT 注册邮箱")
        records = _token_records(platform)
        mailbox_records = (
            _chatgpt_registration_mailbox_map(records)
            if output_format == "email_four"
            else {}
        )
        if output_format == "email_four":
            records = [
                record for record in records
                if _email_from_session(
                    record["data"], record["path"].stem.replace(".session", "")
                ).strip().lower() in mailbox_records
            ]
        if provider_filter and output_format == "email_four":
            records = [
                record for record in records
                if mailbox_records[
                    _email_from_session(
                        record["data"], record["path"].stem.replace(".session", "")
                    ).strip().lower()
                ].get("email_provider") == provider_filter
            ]
        elif provider_filter:
            records = [
                record for record in records
                if _email_provider_from_session(record["data"], record["path"].stem.replace(".session", "")) == provider_filter
            ]
        if output_format == "email_four" and not records:
            provider_label = f" {provider_filter}" if provider_filter else ""
            raise AssetNotFound(f"没有找到可导出的 ChatGPT{provider_label} 注册邮箱记录")
        if phone_filter:
            records = [
                record for record in records
                if str(record["data"].get("codex_phone_status") or "not_verified").strip().lower() == phone_filter
            ]
        if verified_only:
            records = _verified_records(
                platform,
                records,
                lambda record: record["path"].name,
            )
        if should_claim:
            selected, record, total, remaining, advanced = _claim_record(
                records,
                platform,
                lambda record: _asset_identity(
                    _email_from_session(
                        record["data"], record["path"].stem.replace(".session", "")
                    ),
                    record["path"].name,
                ),
                index,
            )
            next_index = 0 if remaining else None
        else:
            cursor_key = f"cookie:{platform}:{output_format}"
            selected, next_index, advanced = _select_index(len(records), cursor_key, index)
            record = records[selected]
            total = len(records)
        session = record["data"]
        source = record["path"].name
        email = _email_from_session(session, record["path"].stem.replace(".session", ""))
        extra = {
            "codex_phone_status": str(session.get("codex_phone_status") or "not_verified").strip().lower(),
        }
        if output_format == "session":
            data = session
        elif output_format == "email_four":
            mailbox = mailbox_records[email.strip().lower()]
            data = _mailbox_four_line(mailbox)
            extra["email_provider"] = mailbox.get("email_provider") or classify_email_provider(email)
        elif platform == "grok":
            data = {"sso_tokens": [str(session.get("sso") or "")], "name": email}
        elif platform == "kiro":
            data = session
        elif output_format == "sub2api":
            data = {
                "content": build_sub2api_content(session),
                "expires_at": sub2api_expires_at(session),
            }
        elif output_format == "cpa":
            converted = build_cpa_codex_json(session, email=email)
            data = converted["auth_json"]
            extra["file_name"] = converted["file_name"]
        else:
            data = build_chatgpt2api_account(session, email=email)
    else:
        raise AssetError("format 仅支持 raw、cookies、header、session、sub2api、cpa、chatgpt2api、email_four")

    result = {
        "kind": "platform_cookie",
        "platform": platform,
        "format": output_format,
        "index": selected,
        "total": total,
        "next_index": next_index,
        "cursor_advanced": advanced,
        "email": email,
        "email_provider": extra.get("email_provider") or (_email_provider_from_session(session, email) if output_format in token_formats else classify_email_provider(email)),
        "source": source,
        "data": data,
        **extra,
    }
    if verified_only:
        result["verification"] = record["_verification"]
    if should_claim:
        result.update({
            "claim_recorded": True,
            "claim_scope": platform,
            "remaining": remaining,
        })
    return result


def export_batch(
    resource: str,
    output_format: str = "",
    limit: int = 100,
    verified_only: bool = False,
    email_provider: str = "",
    codex_phone_status: str = "",
    include_claimed: bool = False,
) -> list[dict]:
    """Build and claim a bounded batch without reparsing files per account."""
    resource = str(resource or "").strip().lower()
    if resource not in {"emails", *_PLATFORMS}:
        raise AssetError("resource must be emails, claude, chatgpt, grok, or kiro")
    try:
        bounded_limit = min(500, max(1, int(limit)))
    except (TypeError, ValueError) as exc:
        raise AssetError("limit must be an integer") from exc
    output_format = str(output_format or ("four" if resource == "emails" else "raw")).strip().lower()
    scope = "outlook" if resource == "emails" else resource
    with _CURSOR_LOCK:
        existing_claims = set(_read_claims().get(scope, set()))
    blocked = set() if include_claimed else existing_claims
    results = []
    selected_claims = set()
    index = 0
    total = None
    while len(results) < bounded_limit and (total is None or index < total):
        try:
            if resource == "emails":
                result = get_email(
                    index=index,
                    output_format=output_format,
                    verified_only=verified_only,
                    claim_once=False,
                    email_provider=email_provider,
                    _defer_claim=True,
                )
            else:
                result = get_platform_asset(
                    resource,
                    output_format=output_format,
                    index=index,
                    verified_only=verified_only,
                    claim_once=False,
                    email_provider=email_provider,
                    codex_phone_status=codex_phone_status,
                    _defer_claim=True,
                )
        except (AssetExhausted, AssetNotFound, AssetUnverified):
            if not results:
                raise
            break
        total = int(result.get("total") or 0)
        index += 1
        identity = _asset_identity(result.get("email", ""), result.get("source", ""))
        claim = _claim_id(scope, identity)
        if claim in blocked or claim in selected_claims:
            continue
        selected_claims.add(claim)
        results.append(result)
    if not results:
        raise AssetExhausted("没有可批量导出的未领取资产")
    with _CURSOR_LOCK:
        claims = _read_claims()
        claims.setdefault(scope, set()).update(selected_claims)
        _write_claims(claims)
    remaining = max(0, (total or len(results)) - index)
    for result in results:
        result.update({
            "claim_recorded": True,
            "claim_scope": scope,
            "remaining": remaining,
        })
    return results


def summary() -> dict:
    claims = _read_claims()
    return {
        "emails": len(_mailboxes()),
        "platforms": {
            platform: {
                "cookies": len(_cookie_records(platform)),
                "sessions": len(_token_records(platform)) if platform in {"chatgpt", "grok", "kiro"} else 0,
            }
            for platform in _PLATFORMS
        },
        "cursors": _read_cursors(),
        "claims": {scope: len(items) for scope, items in sorted(claims.items())},
        "lifecycle": lifecycle_summary(),
    }
