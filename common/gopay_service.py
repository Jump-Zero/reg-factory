"""Adapter for the vendored GoPay registration and Midtrans payment engine."""

from __future__ import annotations

import importlib
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from common import custom_sms


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = PROJECT_ROOT / "vendor" / "gopay_engine"
ENGINE_SOURCE = ENGINE_ROOT / "app" / "src"
TERMINAL_STATES = {"success", "failed", "already_registered"}


class GoPayUnavailable(RuntimeError):
    pass


_LOAD_LOCK = threading.RLock()
_COMPONENTS: dict[str, Any] | None = None
_LOAD_ERROR = ""
_BATCH_LOCK = threading.RLock()
_BATCHES: dict[str, dict[str, Any]] = {}


def runtime_dir() -> Path:
    root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or PROJECT_ROOT).resolve()
    return root / "runtime" / "gopay"


def _configure_runtime() -> Path:
    target = runtime_dir()
    target.mkdir(parents=True, exist_ok=True)
    storage = {
        "OPAI_GOPAY_ACCOUNTS_FILE": target / "accounts.json",
        "OPAI_PAYMENT_INBOX_PATH": target / "payment_inbox.json",
        "OPAI_PAYMENT_INBOX_DB_PATH": target / "payment_inbox.db",
        "OPAI_GOPAY_SMS_ENV_FILE": target / "sms.env",
        "OPAI_GOPAY_ENVELOPE_STORE": target / "envelope_links.json",
        "OPAI_GOPAY_DEVICE_PROFILE_ALLOCATIONS_FILE": target / "device_profile_allocations.json",
        "OPAI_MIDTRANS_SNAP_STATE_FILE": target / "midtrans_snap_state.json",
        "OPAI_GOPAY_SUPPORT_BODY_CORPUS": ENGINE_ROOT / "config" / "support_sdk_body_corpus.json",
    }
    for key, path in storage.items():
        os.environ[key] = str(path)
    # The integrated adapter calls the managers in-process; it must never push
    # credentials to a separately configured legacy inbox service.
    os.environ["OPAI_PAYMENT_INBOX_BASE_URL"] = ""
    os.environ.setdefault("OPAI_GOPAY_APP_VERSION", "2.10.0")
    os.environ.setdefault("OPAI_GOPAY_APP_BUILD", "2100")
    os.environ.setdefault("OPAI_GOPAY_SUPPORT_APP_VERSION", "2.10.0")
    os.environ.setdefault("OPAI_GOPAY_SUPPORT_APP_BUILD", "2100")
    os.environ.setdefault("OPAI_GOPAY_SIGNUP_CLIENT_NAME", "gopay:consumer:app")
    os.environ.setdefault("OPAI_GOPAY_SIGNED_UP_COUNTRY", "ID")
    os.environ.setdefault("OPAI_GOPAY_CVS_DOUBLE_SLASH_INITIATE", "1")
    return target


def _load_components() -> dict[str, Any]:
    global _COMPONENTS, _LOAD_ERROR
    with _LOAD_LOCK:
        if _COMPONENTS is not None:
            return _COMPONENTS
        if not ENGINE_SOURCE.is_dir():
            _LOAD_ERROR = f"GoPay engine source is missing: {ENGINE_SOURCE}"
            raise GoPayUnavailable(_LOAD_ERROR)
        target = _configure_runtime()
        source = str(ENGINE_SOURCE)
        if source not in sys.path:
            sys.path.insert(0, source)
        try:
            inbox = importlib.import_module("opai.core.payment_inbox")
            # The upstream manager keeps two legacy history files below this
            # module root. Redirect that root into reg-factory runtime data.
            inbox.PROJECT_ROOT = target
            worker = importlib.import_module("opai.core.gopay_protocol_worker")
            worker.ACCOUNTS_PATH = str(target / "accounts.json")
            worker.ENVELOPE_STORE_FILE = str(target / "envelope_links.json")
            worker.INBOX_URL = ""
            _COMPONENTS = {
                "inbox": inbox,
                "register": inbox._ManualRegisterManager(),
                "payment": inbox._WebPaymentManager(inbox.InboxStore()),
            }
        except Exception as exc:
            _LOAD_ERROR = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise GoPayUnavailable(_LOAD_ERROR) from exc
        _LOAD_ERROR = ""
        return _COMPONENTS


def _mask_phone(value: object) -> str:
    text = str(value or "")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        return text
    return f"+{digits[:3]}***{digits[-4:]}"


def _public_job(job: dict[str, Any], *, reveal_phone: bool = True) -> dict[str, Any]:
    clean = dict(job or {})
    for key in (
        "_otp", "_cancel_requested", "pin", "proxy", "midtrans_url",
        "payment_fingerprint", "context_run_id", "context_email",
    ):
        clean.pop(key, None)
    if not reveal_phone and clean.get("phone"):
        clean["phone"] = _mask_phone(clean["phone"])
    result = clean.get("result")
    if isinstance(result, dict):
        clean["result"] = {
            key: value
            for key, value in result.items()
            if key not in {"pin", "access_token", "refresh_token", "client", "proxy"}
        }
    return clean


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "phone",
        "local",
        "customer_id",
        "account_id",
        "registered_at",
        "balance",
        "use_status",
        "use_label",
        "use_message",
        "midtrans_binding_status",
        "midtrans_binding_updated_at",
    }
    return {key: value for key, value in account.items() if key in allowed}


def status() -> dict[str, Any]:
    ready = True
    error = ""
    try:
        components = _load_components()
        accounts = components["inbox"]._load_gopay_accounts()
        register_jobs = components["register"].list()
        payment_jobs = components["payment"].list()
    except GoPayUnavailable as exc:
        ready = False
        error = str(exc)
        accounts = []
        register_jobs = []
        payment_jobs = []
    pool = custom_sms.summary()
    with _BATCH_LOCK:
        running_batches = sum(item.get("status") == "running" for item in _BATCHES.values())
    return {
        "ready": ready,
        "error": error or _LOAD_ERROR,
        "engine_root": str(ENGINE_ROOT),
        "data_root": str(runtime_dir()),
        "accounts": len(accounts),
        "available_accounts": sum(item.get("use_status") == "available" for item in accounts),
        "balance_rp": sum(int(item.get("balance") or 0) for item in accounts),
        "phones": int(pool.get("total") or 0),
        "available_phones": int(pool.get("available") or 0),
        "running": sum(item.get("status") in {"running", "waiting_otp"} for item in register_jobs)
        + sum(item.get("status") in {"running", "waiting_otp"} for item in payment_jobs)
        + running_batches,
    }


def accounts() -> list[dict[str, Any]]:
    rows = _load_components()["inbox"]._load_gopay_accounts()
    return [_public_account(item) for item in rows]


def refresh_balance(phone: str) -> dict[str, Any]:
    return _load_components()["inbox"]._refresh_gopay_balance(phone)


def delete_account(phone: str) -> bool:
    components = _load_components()
    inbox = components["inbox"]
    target = re.sub(r"\D", "", str(phone or ""))
    if any(
        re.sub(r"\D", "", str(job.get("phone") or "")) == target
        and str(job.get("status") or "") in {"running", "waiting_otp"}
        for job in components["payment"].list()
    ):
        raise ValueError("账号正在执行支付，不能删除")
    rows = inbox._load_gopay_accounts_raw()
    kept = [
        row
        for row in rows
        if re.sub(r"\D", "", str(row.get("phone") or "")) != target
    ]
    if len(kept) == len(rows):
        return False
    inbox._write_gopay_accounts_raw(kept)
    return True


def clear_accounts() -> int:
    components = _load_components()
    if any(
        str(job.get("status") or "") in {"running", "waiting_otp"}
        for job in components["payment"].list()
    ):
        raise ValueError("存在正在运行的 GoPay 支付，不能清空账号")
    inbox = components["inbox"]
    rows = inbox._load_gopay_accounts_raw()
    inbox._write_gopay_accounts_raw([])
    return len(rows)


def sms_status() -> dict[str, Any]:
    return _load_components()["inbox"]._sms_api_status(include_balance=False)


def save_sms_config(data: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "api_key": str(data.get("api_key") or "").strip(),
        "api_key_file": str(data.get("api_key_file") or "").strip(),
        "api_base_url": str(data.get("api_base_url") or "https://smsbower.page").strip(),
        "service": str(data.get("service") or "ni").strip(),
        "country": str(data.get("country") or "6").strip(),
    }
    return _load_components()["inbox"]._write_sms_config(allowed)


def phone_pool() -> dict[str, Any]:
    return custom_sms.summary()


def import_phones(text: str) -> dict[str, Any]:
    return custom_sms.import_text(text)


def delete_phone(phone: str) -> bool:
    return custom_sms.delete(phone)


def clear_phones() -> int:
    return custom_sms.clear()


def _pin(value: object) -> str:
    pin = str(value or "147258").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise ValueError("GoPay PIN 必须是 6 位数字")
    return pin


def _feed_custom_sms(job_id: str, pkey: str) -> None:
    manager = _load_components()["register"]
    seen: set[str] = set()
    handled_prompt = ""
    while True:
        job = manager.get(job_id) or {}
        state = str(job.get("status") or "")
        if state in TERMINAL_STATES:
            if state in {"success", "already_registered"}:
                custom_sms.complete(pkey)
            else:
                custom_sms.release(pkey)
            return
        prompt = job.get("prompt") if isinstance(job.get("prompt"), dict) else {}
        prompt_key = f"{prompt.get('purpose')}:{prompt.get('started_at')}"
        if state == "waiting_otp" and prompt_key and prompt_key != handled_prompt:
            handled_prompt = prompt_key
            timeout = max(10, min(300, int(prompt.get("timeout") or 180)))
            code = custom_sms.get_code(
                pkey,
                max_wait=timeout,
                interval=3,
                consume=False,
                seen=seen,
            )
            if code:
                manager.submit_otp(job_id, code)
        time.sleep(1)


def start_registration(data: dict[str, Any]) -> dict[str, Any]:
    components = _load_components()
    source = str(data.get("source") or "custom").strip().lower()
    if source not in {"custom", "manual", "smsbower"}:
        raise ValueError("号码来源必须是 custom、manual 或 smsbower")
    pin = _pin(data.get("pin"))
    phone = str(data.get("phone") or "").strip()
    pkey = ""
    engine_source = "pool"
    if source == "custom":
        rental = custom_sms.claim()
        if rental is None:
            raise ValueError("自定义号码池没有可用号码")
        digits, _country, pkey = rental
        phone = f"+{digits}"
    elif source == "smsbower":
        engine_source = "smsbower"
        phone = ""
    elif not phone:
        raise ValueError("手动模式必须填写手机号")
    country_code = str(data.get("country_code") or "62").strip().lstrip("+") or "62"
    try:
        job = components["register"].start(
            source=engine_source,
            phone=phone,
            pin=pin,
            country_code=country_code,
            signed_up_country=str(data.get("signed_up_country") or "ID").strip() or "ID",
            force_live=country_code != "62",
            login_existing=bool(data.get("login_existing")),
            proxy=str(data.get("proxy") or "").strip(),
        )
    except Exception:
        if pkey:
            custom_sms.release(pkey)
        raise
    if pkey:
        threading.Thread(
            target=_feed_custom_sms,
            args=(str(job.get("id") or ""), pkey),
            daemon=True,
            name=f"gopay-custom-sms-{job.get('id')}",
        ).start()
    return _public_job(job)


def registration_jobs() -> list[dict[str, Any]]:
    jobs = [_public_job(item) for item in _load_components()["register"].list()]
    with _BATCH_LOCK:
        batches = [dict(item) for item in _BATCHES.values()]
    batches.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return batches + jobs


def submit_registration_otp(job_id: str, code: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4,8}", str(code or "").strip()):
        raise ValueError("OTP 必须是 4 至 8 位数字")
    job = _load_components()["register"].submit_otp(job_id, str(code).strip())
    if not job:
        raise LookupError("注册任务不存在或已结束")
    return _public_job(job)


def _run_batch(batch_id: str, data: dict[str, Any]) -> None:
    count = int(data["count"])
    workers = int(data["workers"])

    def one(_: int) -> None:
        try:
            job = start_registration(data)
            job_id = str(job.get("id") or "")
            with _BATCH_LOCK:
                batch = _BATCHES[batch_id]
                batch["started"] += 1
                batch.setdefault("job_ids", []).append(job_id)
            deadline = time.time() + 1200
            while time.time() < deadline:
                current = _load_components()["register"].get(job_id) or {}
                if current.get("status") in TERMINAL_STATES:
                    if current.get("status") == "success":
                        with _BATCH_LOCK:
                            _BATCHES[batch_id]["succeeded"] += 1
                    elif current.get("status") == "already_registered":
                        with _BATCH_LOCK:
                            _BATCHES[batch_id]["existing"] += 1
                    return
                time.sleep(1)
            raise TimeoutError("GoPay 注册任务超时")
        except Exception as exc:
            with _BATCH_LOCK:
                batch = _BATCHES[batch_id]
                batch["failed"] += 1
                batch["last_error"] = str(exc)[:200]
        finally:
            with _BATCH_LOCK:
                _BATCHES[batch_id]["finished"] += 1

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gopay-batch") as pool:
        futures = [pool.submit(one, index) for index in range(count)]
        for future in as_completed(futures):
            future.result()
    with _BATCH_LOCK:
        _BATCHES[batch_id]["status"] = "done"
        _BATCHES[batch_id]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


def start_batch(data: dict[str, Any]) -> dict[str, Any]:
    source = str(data.get("source") or "custom").strip().lower()
    if source not in {"custom", "smsbower"}:
        raise ValueError("批量任务只支持自定义号码池或 SMSBower")
    count = max(1, min(50, int(data.get("count") or 1)))
    workers = max(1, min(5, int(data.get("workers") or 1)))
    normalized = dict(data)
    normalized.update({"source": source, "count": count, "workers": workers, "pin": _pin(data.get("pin"))})
    batch_id = uuid.uuid4().hex[:12]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    batch = {
        "id": batch_id,
        "kind": "batch",
        "source": source,
        "count": count,
        "workers": workers,
        "started": 0,
        "finished": 0,
        "succeeded": 0,
        "existing": 0,
        "failed": 0,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "job_ids": [],
    }
    with _BATCH_LOCK:
        _BATCHES[batch_id] = batch
    threading.Thread(
        target=_run_batch,
        args=(batch_id, normalized),
        daemon=True,
        name=f"gopay-batch-{batch_id}",
    ).start()
    return dict(batch)


def start_relogin(phone: str, data: dict[str, Any]) -> dict[str, Any]:
    inbox = _load_components()["inbox"]
    account, _index = inbox._find_gopay_account(phone)
    if not account:
        raise LookupError("GoPay 账号不存在")
    payload = {
        "source": "manual",
        "phone": account.get("phone") or phone,
        "pin": data.get("pin") or account.get("pin") or "147258",
        "country_code": str(account.get("country_code") or "62").lstrip("+"),
        "login_existing": True,
        "proxy": data.get("proxy") or account.get("proxy") or "",
    }
    return start_registration(payload)


def payment_jobs() -> list[dict[str, Any]]:
    return [_public_job(item) for item in _load_components()["payment"].list()]


def payment_job(job_id: str) -> dict[str, Any] | None:
    job = _load_components()["payment"].get(job_id)
    return _public_job(job) if job else None


def cancel_payment_context(context_run_id: str) -> int:
    target = str(context_run_id or "").strip()
    if not target:
        return 0
    manager = _load_components()["payment"]
    cancelled = 0
    with manager._lock:
        for job_id, job in manager._jobs.items():
            if (
                str(job.get("context_run_id") or "") == target
                and str(job.get("status") or "") in {"running", "waiting_otp"}
            ):
                job["_cancel_requested"] = True
                job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                condition = manager._conds.get(job_id)
                if condition:
                    condition.notify_all()
                cancelled += 1
        if cancelled:
            manager._save_state_locked()
    return cancelled


def _payment_account(phone: str = "") -> dict[str, Any]:
    inbox = _load_components()["inbox"]
    requested = re.sub(r"\D", "", str(phone or ""))
    public = inbox._load_gopay_accounts()
    candidates = [
        item
        for item in public
        if item.get("use_status") == "available" and int(item.get("balance") or 0) >= 1
    ]
    if requested:
        candidates = [
            item
            for item in candidates
            if re.sub(r"\D", "", str(item.get("phone") or "")) == requested
        ]
    if not candidates:
        if requested:
            raise LookupError("指定的 GoPay 账号不可用、余额不足或已绑定其他支付任务")
        raise LookupError("GoPay 钱包池没有可用且余额充足的账号")
    candidates.sort(key=lambda item: (-int(item.get("balance") or 0), str(item.get("registered_at") or "")))
    account, _index = inbox._find_gopay_account(str(candidates[0].get("phone") or ""))
    if not account:
        raise LookupError("GoPay 账号凭据不存在")
    return account


def start_payment(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("confirm_payment") is not True:
        raise ValueError("真实 GoPay 支付必须显式确认")
    midtrans_url = str(data.get("midtrans_url") or "").strip()
    if not midtrans_url:
        raise ValueError("Midtrans 链接不能为空")
    account = _payment_account(str(data.get("phone") or ""))
    pin = data.get("pin") or account.get("pin") or ""
    if not pin:
        raise ValueError("GoPay 账号缺少本地 PIN，无法执行支付")
    components = _load_components()
    job = components["payment"].start(
        phone=str(account.get("phone") or ""),
        pin=_pin(pin),
        midtrans_url=midtrans_url,
        proxy=str(data.get("proxy") or account.get("proxy") or "").strip(),
    )
    context_email = str(data.get("context_email") or "").strip().lower()
    context_run_id = str(data.get("context_run_id") or "").strip()
    if context_email or context_run_id:
        manager = components["payment"]
        with manager._lock:
            stored = manager._jobs.get(str(job.get("id") or ""))
            if stored:
                if context_email:
                    stored["context_email"] = context_email
                if context_run_id:
                    stored["context_run_id"] = context_run_id
                manager._save_state_locked()
                job = manager.get(str(job.get("id") or "")) or job
    return _public_job(job)


def submit_payment_otp(job_id: str, code: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4,8}", str(code or "").strip()):
        raise ValueError("OTP 必须是 4 至 8 位数字")
    job = _load_components()["payment"].submit_otp(job_id, str(code).strip())
    if not job:
        raise LookupError("支付任务不存在或已结束")
    return _public_job(job)
