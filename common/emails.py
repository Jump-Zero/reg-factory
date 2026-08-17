# -*- coding: utf-8 -*-
"""
common/emails.py — 邮箱供给（平台独立的占用记录）

读取 emails.txt（email----password----refresh_token----client_id），
每个平台用独立的 emails_used_<platform>.txt 记录已占用，互不干扰。
线程安全。
"""

import os
import sys
import threading

from common.file_lock import append_line, file_lock

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

EMAILS_FILE = "emails.txt"
_lock = threading.Lock()


def _used_file(platform):
    return f"emails_used_{platform}.txt"


def _error_file(platform):
    return f"emails_error_{platform}.txt"


def _outlook_sale_file():
    root = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or "."
    return os.path.join(root, "runtime", "state", "outlook_sale_emails.txt")


def _outlook_registration_file():
    root = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or "."
    return os.path.join(root, "runtime", "state", "outlook_registration_emails.txt")


def _exclude_from_outlook_sale(platform, email):
    if str(platform or "").strip().lower() in {"", "email", "outlook"}:
        return
    append_line(_outlook_registration_file(), str(email or "").strip().lower())


def mark_registration_started(platform, email, password=""):
    """Permanently reserve an explicit mailbox for a platform registration."""
    normalized_platform = str(platform or "").strip().lower()
    if normalized_platform in {"", "email", "outlook"}:
        return
    _exclude_from_outlook_sale(normalized_platform, email)
    append_line(_used_file(normalized_platform), f"{email}----{password}----reserved")


def _load_used(platform):
    used = set()
    for fp in [
        _used_file(platform),
        _error_file(platform),
        _outlook_sale_file(),
        _outlook_registration_file(),
    ]:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        used.add(line.split("----")[0].strip().lower())
    return used


def next_email(platform):
    """取下一个未被该平台占用的邮箱，返回 (email, password, refresh_token, client_id) 或 None。
    取出即标记 reserved，防止并发重复。"""
    with _lock, file_lock(f"{EMAILS_FILE}.{platform}.reserve"):
        if not os.path.exists(EMAILS_FILE):
            print(f"  [email] {EMAILS_FILE} not found")
            return None
        used = _load_used(platform)
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("----")
                email = parts[0].strip()
                if email.lower() in used:
                    continue
                password = parts[1].strip() if len(parts) >= 2 else ""
                token = parts[2].strip() if len(parts) >= 3 else ""
                client_id = parts[3].strip() if len(parts) >= 4 else ""
                append_line(_used_file(platform), f"{email}----{password}----reserved")
                _exclude_from_outlook_sale(platform, email)
                print(f"  [email] picked for {platform}: {email}")
                return email, password, token, client_id
        print(f"  [email] no unused emails left for {platform}")
        return None


def latest_email(platform, require_token=False, validate_token=False):
    """Reserve the newest unused mailbox, optionally requiring a working Graph RT."""
    with _lock, file_lock(f"{EMAILS_FILE}.{platform}.reserve"):
        if not os.path.exists(EMAILS_FILE):
            print(f"  [email] {EMAILS_FILE} not found")
            return None
        used = _load_used(platform)
        with open(EMAILS_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        for line in reversed(lines):
            parts = line.split("----")
            email = parts[0].strip()
            if email.lower() in used:
                continue
            password = parts[1].strip() if len(parts) >= 2 else ""
            token = parts[2].strip() if len(parts) >= 3 else ""
            client_id = parts[3].strip() if len(parts) >= 4 else ""
            if require_token and (not token or not client_id):
                continue
            if validate_token:
                from common.mailbox import check_mailbox_access
                validation = check_mailbox_access(email, token, client_id) if token else {
                    "ok": False, "permanent": True, "reason": "missing_refresh_token"
                }
                if not validation["ok"]:
                    reason = validation.get("reason") or "refresh_token_unusable"
                    if validation.get("permanent"):
                        append_line(_error_file(platform), f"{email}----{password}----{reason}")
                        used.add(email.lower())
                        print(f"  [email] quarantined unusable rt for {platform}: {email} ({reason})")
                    else:
                        print(f"  [email] skip mailbox after transient rt check: {email} ({reason})")
                    continue
                folders = validation.get("folder_status") or {}
                folder_log = ", ".join(
                    f"{name}={status}" for name, status in folders.items()
                )
                if folder_log:
                    print(f"  [email] Graph mailbox readable: {email} ({folder_log})")
            append_line(_used_file(platform), f"{email}----{password}----reserved")
            _exclude_from_outlook_sale(platform, email)
            print(f"  [email] picked latest for {platform}: {email} (rt={'yes' if token else 'no'})")
            return email, password, token, client_id
        print(f"  [email] no unused latest mailbox for {platform} "
              f"(require_token={require_token}, validate_token={validate_token})")
        return None


def mark_used(platform, email, password=""):
    append_line(_used_file(platform), f"{email}----{password}----ok")
    _exclude_from_outlook_sale(platform, email)


REGISTER_PLATFORMS = ("claude", "chatgpt", "grok", "kiro", "tri")


def _read_blocklist_emails(path):
    emails = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    emails.add(line.split("----")[0].strip().lower())
    return emails


def recycle_reserved(platforms=None):
    """清除平台 used 文件中的 reserved 行（保留 ok 行），释放未完成注册的邮箱。

    同时从 outlook_registration_emails.txt 移除未在任何平台标记 ok/error、
    也未列入销售排除的邮箱，否则它们仍会被 _load_used 全局屏蔽。
    返回 {platform: {"recycled": N, "kept": N}, "released": N}。
    """
    targets = [str(p).strip().lower() for p in (platforms or REGISTER_PLATFORMS)]
    targets = [p for p in targets if p and p not in {"email", "outlook"}]
    result = {}
    with _lock:
        protected = set()
        for platform in REGISTER_PLATFORMS:
            protected |= _read_blocklist_emails(_error_file(platform))
            if os.path.exists(_used_file(platform)):
                with open(_used_file(platform), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and not line.endswith("----reserved"):
                            protected.add(line.split("----")[0].strip().lower())
        protected |= _read_blocklist_emails(_outlook_sale_file())

        recycled_all = set()
        for platform in targets:
            path = _used_file(platform)
            if not os.path.exists(path):
                result[platform] = {"recycled": 0, "kept": 0}
                continue
            with file_lock(f"{EMAILS_FILE}.{platform}.reserve"):
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                kept, recycled = [], 0
                for raw in lines:
                    stripped = raw.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if stripped.endswith("----reserved"):
                        recycled += 1
                        recycled_all.add(stripped.split("----")[0].strip().lower())
                    else:
                        kept.append(raw)
                with open(path, "w", encoding="utf-8") as f:
                    if kept:
                        f.write("\n".join(kept) + "\n")
            result[platform] = {"recycled": recycled, "kept": len(kept)}

        released = recycled_all - protected
        reg_path = _outlook_registration_file()
        if released and os.path.exists(reg_path):
            with file_lock(reg_path):
                with open(reg_path, "r", encoding="utf-8") as f:
                    reg_lines = [line for line in f.read().splitlines() if line.strip()]
                kept_reg = [l for l in reg_lines if l.strip().lower() not in released]
                if len(kept_reg) != len(reg_lines):
                    with open(reg_path, "w", encoding="utf-8") as f:
                        if kept_reg:
                            f.write("\n".join(kept_reg) + "\n")
        result["released"] = len(released)
    return result


def mark_error(platform, email, password="", reason=""):
    append_line(_error_file(platform), f"{email}----{password}----{reason}")
    _exclude_from_outlook_sale(platform, email)
