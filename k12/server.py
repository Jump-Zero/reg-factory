"""New standalone Codex K12 service.

This service owns its port, storage and task lifecycle while reusing the
reg-factory account parser and OAuth worker as an implementation library.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
_shared_data_root = Path(os.environ.get("REG_FACTORY_DATA_DIR") or ROOT).resolve()
DATA_ROOT = Path(
    os.environ.get("REG_FACTORY_K12_DATA_DIR")
    or _shared_data_root / "runtime" / "k12"
).resolve()
MAIN_EMAILS_PATH = Path(os.environ.get("REG_FACTORY_EMAILS_FILE") or (_shared_data_root / "emails.txt")).resolve()
STATIC_ROOT = Path(__file__).resolve().parent / "static"
CONFIG_PATH = DATA_ROOT / "config.json"
EMAILS_PATH = DATA_ROOT / "emails.json"
TASKS_PATH = DATA_ROOT / "tasks.json"
RUNTIME_ROOT = DATA_ROOT / "runtime"
TEMP_ROOT = RUNTIME_ROOT / "inputs"

app = FastAPI(title="Reg Factory K12")
app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

STATE_LOCK = asyncio.Lock()
TASKS: dict[str, dict[str, Any]] = {}
EMAILS: dict[str, dict[str, Any]] = {}
PROCESSES: dict[str, asyncio.subprocess.Process] = {}
CONFIG: dict[str, Any] = {}
TASK_SEMAPHORE = asyncio.Semaphore(1)
REFILL_TASK: asyncio.Task | None = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _default_config() -> dict[str, Any]:
    otp_mode = str(os.environ.get("K12_OTP_MODE", "auto") or "auto").strip().lower()
    if otp_mode not in {"auto", "manual"}:
        otp_mode = "auto"
    return {
        "port": int(os.environ.get("K12_PORT", "8806") or 8806),
        "workspace_ids": [item.strip() for item in os.environ.get("K12_WORKSPACE_IDS", "").replace(",", "\n").splitlines() if item.strip()],
        "workspace_route": os.environ.get("K12_WORKSPACE_ROUTE", "request"),
        "run_workspace_join": os.environ.get("K12_RUN_WORKSPACE_JOIN", "1").lower() not in {"0", "false", "no", "off"},
        "group": os.environ.get("K12_SUB2API_GROUP", os.environ.get("SUB2API_GROUP", "k12")),
        "concurrency": max(1, min(5, int(os.environ.get("K12_CONCURRENCY", "1") or 1))),
        "timeout": max(120, min(3600, int(os.environ.get("K12_TIMEOUT", "600") or 600))),
        "phone_attempts": max(1, min(10, int(os.environ.get("K12_PHONE_ATTEMPTS", "3") or 3))),
        "sms_timeout": max(30, min(600, int(os.environ.get("K12_SMS_TIMEOUT", "180") or 180))),
        "sms_provider": os.environ.get("K12_SMS_PROVIDER", "auto"),
        "otp_mode": otp_mode,
        "node": os.environ.get("K12_NODE", "auto"),
        "auto_refill_enabled": os.environ.get("K12_AUTO_REFILL_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
        "refill_threshold": max(1, int(os.environ.get("K12_REFILL_THRESHOLD", "1") or 1)),
        "refill_count": max(1, int(os.environ.get("K12_REFILL_COUNT", "1") or 1)),
        "refill_interval": max(30, int(os.environ.get("K12_REFILL_INTERVAL", "300") or 300)),
        "network_mode": os.environ.get("K12_NETWORK_MODE", "inherit"),
        "network_node": os.environ.get("K12_NETWORK_NODE", ""),
        "sub2api_url": os.environ.get("K12_SUB2API_URL", os.environ.get("SUB2API_URL", "")),
        "sub2api_email": os.environ.get("K12_SUB2API_EMAIL", os.environ.get("SUB2API_EMAIL", "")),
        "sub2api_password": os.environ.get("K12_SUB2API_PASSWORD", os.environ.get("SUB2API_PASSWORD", "")),
        "default_password": os.environ.get("K12_DEFAULT_PASSWORD", ""),
    }


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _public_email(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id", ""),
        "email": record.get("email", ""),
        "status": record.get("status", "free"),
        "last_error": record.get("last_error", ""),
        "updated_at": record.get("updated_at", ""),
        "pool_source": record.get("pool_source", "private"),
        "parent_email": record.get("parent_email", ""),
    }


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in task.items()
        if key not in {"access_token", "password", "refresh_token", "client_id", "raw"}
    }


def _public_config() -> dict[str, Any]:
    public = dict(CONFIG)
    public.pop("sub2api_password", None)
    public.pop("default_password", None)
    public["sub2api_password_present"] = bool(CONFIG.get("sub2api_password"))
    public["default_password_present"] = bool(CONFIG.get("default_password"))
    return public


def _load_state() -> None:
    global CONFIG, EMAILS, TASKS
    saved_config = _read_json(CONFIG_PATH, {})
    CONFIG = {**_default_config(), **(saved_config if isinstance(saved_config, dict) else {})}
    if CONFIG.get("otp_mode") not in {"auto", "manual"}:
        CONFIG["otp_mode"] = "auto"
    CONFIG["workspace_ids"] = [str(item).strip() for item in CONFIG.get("workspace_ids") or [] if str(item).strip()][:100]
    for key, low, high in (("concurrency", 1, 5), ("timeout", 120, 3600), ("phone_attempts", 1, 10), ("sms_timeout", 30, 600), ("refill_threshold", 1, 5000), ("refill_count", 1, 500), ("refill_interval", 30, 86400)):
        try:
            CONFIG[key] = max(low, min(high, int(CONFIG.get(key))))
        except (TypeError, ValueError):
            CONFIG[key] = _default_config()[key]
    raw_emails = _read_json(EMAILS_PATH, {})
    raw_tasks = _read_json(TASKS_PATH, {})
    EMAILS = raw_emails if isinstance(raw_emails, dict) else {}
    TASKS = raw_tasks if isinstance(raw_tasks, dict) else {}


def _configure_shared_email_pool() -> None:
    """Point the shared email allocator at the main project's pool file."""
    try:
        from common import emails as pool

        pool.EMAILS_FILE = str(MAIN_EMAILS_PATH)
    except Exception:
        pass


def _shared_email_records() -> list[dict[str, Any]]:
    if not MAIN_EMAILS_PATH.is_file():
        return []
    try:
        text = MAIN_EMAILS_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    records, _errors = _normalize_records(text)
    return records


def _refresh_shared_email_pool() -> int:
    """Merge the live main pool into K12 without losing K12 task status."""
    _configure_shared_email_pool()
    added = 0
    try:
        from common import emails as pool

        used = pool._load_used("k12")
    except Exception:
        used = set()
    by_email = {str(item.get("email") or "").lower(): item for item in EMAILS.values()}
    for source in _shared_email_records():
        key = source["email"].lower()
        existing = by_email.get(key)
        if existing:
            existing["pool_source"] = existing.get("pool_source") or "main"
            if key in used and existing.get("status") == "free":
                existing["status"] = "claimed"
            for field in ("password", "account_password", "refresh_token", "client_id", "raw"):
                if source.get(field) and not existing.get(field):
                    existing[field] = source[field]
            continue
        EMAILS[source["id"]] = source
        source["pool_source"] = "main"
        if source["email"].lower() in used:
            source["status"] = "claimed"
        by_email[key] = source
        added += 1
    return added


def _reserve_shared_email(record: dict[str, Any]) -> None:
    """Reserve a K12 mailbox using the project's cross-process allocator."""
    if record.get("shared_reserved"):
        return
    try:
        _configure_shared_email_pool()
        from common import emails as pool

        pool.mark_registration_started("k12", record.get("email"), record.get("password") or "")
        record["shared_reserved"] = True
    except Exception:
        # K12 remains usable with a private imported record if the shared file
        # is unavailable; normal main-pool imports still use the allocator.
        pass


def _mark_shared_result(record: dict[str, Any], success: bool, reason: str = "") -> None:
    """Write the terminal K12 outcome into the shared allocator journal."""
    if record.get("pool_source") != "main" or record.get("shared_result_marked"):
        return
    try:
        _configure_shared_email_pool()
        from common import emails as pool

        email = record.get("email") or ""
        password = record.get("password") or ""
        if success:
            pool.mark_used("k12", email, password)
        else:
            pool.mark_error("k12", email, password, reason or "k12_failed")
        record["shared_result_marked"] = True
    except Exception:
        pass


async def _refill_once() -> dict[str, Any]:
    added = _refresh_shared_email_pool()
    await _persist()
    available = sum(1 for record in EMAILS.values() if record.get("status") == "free")
    return {"added": added, "available": available, "checked_at": _now()}


async def _refill_loop() -> None:
    while True:
        try:
            await _refill_once()
            await asyncio.sleep(max(30, int(CONFIG.get("refill_interval") or 300)))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(30)


def _normalize_records(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from common.account_records import parse_account_text

    records, errors = parse_account_text(text, plus_credentials=True)
    result = []
    for item in records:
        email = str(item.get("email") or "").strip().lower()
        if not email:
            continue
        result.append({
            "id": secrets.token_hex(10),
            "email": email,
            "password": str(item.get("password") or CONFIG.get("default_password") or ""),
            "account_password": str(item.get("account_password") or ""),
            "refresh_token": str(item.get("refresh_token") or ""),
            "client_id": str(item.get("client_id") or ""),
            "raw": str(item.get("raw") or ""),
            "status": "free",
            "last_error": "",
            "updated_at": _now(),
        })
    return result, errors


async def _persist() -> None:
    async with STATE_LOCK:
        await asyncio.to_thread(_write_json, CONFIG_PATH, CONFIG)
        await asyncio.to_thread(_write_json, EMAILS_PATH, EMAILS)
        await asyncio.to_thread(_write_json, TASKS_PATH, TASKS)


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["REG_FACTORY_DATA_DIR"] = str(DATA_ROOT)
    env["REG_FACTORY_ENV_FILE"] = str(DATA_ROOT / ".env")
    env["REG_FACTORY_PLATFORM"] = "chatgpt"
    env["SUB2API_GROUP"] = str(CONFIG.get("group") or "k12")
    for key, config_key in (
        ("SUB2API_URL", "sub2api_url"),
        ("SUB2API_EMAIL", "sub2api_email"),
        ("SUB2API_PASSWORD", "sub2api_password"),
        ("DEFAULT_PASSWORD", "default_password"),
    ):
        value = str(CONFIG.get(config_key) or "").strip()
        if value:
            env[key] = value
    if CONFIG.get("workspace_ids"):
        env["K12_WORKSPACE_IDS"] = "\n".join(str(item) for item in CONFIG["workspace_ids"])
    mode = str(CONFIG.get("network_mode") or "inherit").strip().lower()
    node = str(CONFIG.get("network_node") or "").strip()
    if mode == "direct":
        # proxy_switch treats ``none`` as a true no-proxy mode; ``direct`` is
        # reserved there as an alias for residential compatibility.
        env["CHATGPT_PROXY_MODE"] = "none"
        env["REG_FACTORY_NO_CLASH"] = "1"
    elif mode not in {"inherit", "global", ""}:
        env["CHATGPT_PROXY_MODE"] = mode
    if node and mode in {"clash_auto", "clash_fixed", ""}:
        env["CHATGPT_PROXY_MODE"] = "clash_fixed"
        env["CHATGPT_CLASH_FIXED_NODE"] = node
    try:
        from common import proxy_switch

        env = proxy_switch.platform_environment(env, "chatgpt")
    except Exception:
        pass
    return env


def _account_line(record: dict[str, Any]) -> str:
    raw = str(record.get("raw") or "").strip()
    if raw:
        return raw
    fields = [str(record.get("email") or ""), str(record.get("password") or "")]
    if record.get("refresh_token"):
        fields.extend([str(record.get("client_id") or ""), str(record.get("refresh_token") or "")])
    return "----".join(fields)


async def _run_task(task_id: str, record_id: str) -> None:
    async with TASK_SEMAPHORE:
        await _run_task_limited(task_id, record_id)


async def _run_task_limited(task_id: str, record_id: str) -> None:
    task = TASKS[task_id]
    record = EMAILS[record_id]
    if task.get("status") == "canceled":
        if record.get("status") in {"queued", "running"}:
            record.update({"status": "free", "updated_at": _now()})
        return
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    input_path = TEMP_ROOT / f"{task_id}.txt"
    result_path = RUNTIME_ROOT / "results" / f"{task_id}.jsonl"
    otp_path = RUNTIME_ROOT / "otp" / f"{task_id}.txt"
    input_path.write_text(_account_line(record) + "\n", encoding="utf-8")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-u",
        "--task",
        "tools/import_plus_codex.py",
    ] if getattr(sys, "frozen", False) else [
        sys.executable,
        "-u",
        str(ROOT / "tools" / "import_plus_codex.py"),
    ]
    command.extend([
        "--accounts-file", str(input_path),
        "--group", str(CONFIG.get("group") or "k12"),
        "--concurrency", "1",
        "--node", str(CONFIG.get("node") or "auto"),
        "--sms-provider", str(CONFIG.get("sms_provider") or "auto"),
        "--phone-attempts", str(CONFIG.get("phone_attempts") or 3),
        "--sms-timeout", str(CONFIG.get("sms_timeout") or 180),
        "--timeout", str(CONFIG.get("timeout") or 600),
        "--workspace-ids", *[str(item) for item in CONFIG.get("workspace_ids") or []],
        "--workspace-route", str(CONFIG.get("workspace_route") or "request"),
        "--results", str(result_path),
        "--delete-input",
    ])
    if str(CONFIG.get("otp_mode") or "auto").lower() == "manual":
        command.extend(["--otp-file", str(otp_path)])
    if CONFIG.get("run_workspace_join"):
        command.append("--run-workspace-join")
    task.update({"status": "running", "started_at": _now(), "updated_at": _now()})
    record.update({"status": "running", "updated_at": _now(), "last_error": ""})
    await _persist()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(ROOT),
            env=_child_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        PROCESSES[task_id] = process
        assert process.stdout is not None
        async for line in process.stdout:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                task.setdefault("logs", []).append(text[:1000])
                task["logs"] = task["logs"][-300:]
                task["updated_at"] = _now()
                await _persist()
        code = await process.wait()
        result_lines = [line for line in result_path.read_text(encoding="utf-8").splitlines() if line.strip()] if result_path.is_file() else []
        if result_lines:
            try:
                summary = json.loads(result_lines[-1])
                for key in ("stage", "phone_status", "plan_type", "sub2api_account_id", "workspace_results", "message"):
                    if key in summary:
                        task[key] = summary[key]
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        canceled = task.get("status") == "canceled"
        task["status"] = "canceled" if canceled else ("success" if code == 0 else "failed")
        task["returncode"] = code
        record["status"] = "free" if canceled else ("success" if code == 0 else "failed")
        if code != 0 and not canceled:
            record["last_error"] = next((line for line in reversed(task.get("logs", [])) if "[FAIL]" in line), "K12 task failed")[:500]
        _mark_shared_result(record, code == 0 and not canceled, record.get("last_error") or ("k12_canceled" if canceled else "k12_failed"))
    except asyncio.CancelledError:
        task["status"] = "canceled"
        record["status"] = "free"
        _mark_shared_result(record, False, "k12_canceled")
        if process:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        raise
    except Exception as exc:
        message = str(exc)[:500] or type(exc).__name__
        task.update({"status": "failed", "returncode": -1, "message": message})
        record.update({"status": "failed", "last_error": message})
        _mark_shared_result(record, False, message)
    finally:
        PROCESSES.pop(task_id, None)
        input_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)
        otp_path.unlink(missing_ok=True)
        task.update({"finished_at": _now(), "updated_at": _now()})
        record["updated_at"] = _now()
        await _persist()


@app.on_event("startup")
async def startup() -> None:
    global TASK_SEMAPHORE, REFILL_TASK
    _load_state()
    _configure_shared_email_pool()
    _refresh_shared_email_pool()
    recovered = 0
    for task in TASKS.values():
        if task.get("status") not in {"running", "queued"}:
            continue
        task.update({"status": "failed", "returncode": -1, "message": "K12 服务重启，任务未完成", "updated_at": _now(), "finished_at": _now()})
        record = EMAILS.get(str(task.get("email_id") or ""))
        if record and record.get("status") in {"running", "queued"}:
            record.update({"status": "failed", "last_error": "K12 service restarted", "updated_at": _now()})
            _mark_shared_result(record, False, "k12_service_restarted")
        recovered += 1
    TASK_SEMAPHORE = asyncio.Semaphore(max(1, min(5, int(CONFIG.get("concurrency") or 1))))
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if recovered:
        await _persist()
    if CONFIG.get("auto_refill_enabled"):
        REFILL_TASK = asyncio.create_task(_refill_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    global REFILL_TASK
    if REFILL_TASK and not REFILL_TASK.done():
        REFILL_TASK.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await REFILL_TASK
    REFILL_TASK = None
    for process in list(PROCESSES.values()):
        with contextlib.suppress(ProcessLookupError):
            process.terminate()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_ROOT / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "reg-factory-k12", "version": "1.0.0", "port": CONFIG.get("port", 8806)}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {"config": _public_config()}


@app.patch("/api/config")
async def update_config(request: Request) -> dict[str, Any]:
    global TASK_SEMAPHORE, REFILL_TASK
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse({"error": "config must be an object"}, status_code=400)
    if "workspace_ids" in payload:
        from k12.workspace import workspace_ids
        CONFIG["workspace_ids"] = workspace_ids(payload["workspace_ids"])
    for key in ("workspace_route", "group", "node", "sms_provider", "otp_mode", "sub2api_url", "sub2api_email", "network_mode", "network_node"):
        if key in payload and payload[key] is not None:
            value = str(payload[key]).strip()
            if key == "otp_mode" and value not in {"auto", "manual"}:
                return JSONResponse({"error": "otp_mode must be auto or manual"}, status_code=400)
            CONFIG[key] = value
    for key in ("run_workspace_join", "auto_refill_enabled", "refill_threshold", "refill_count", "refill_interval", "concurrency", "timeout", "phone_attempts", "sms_timeout"):
        if key in payload:
            value = payload[key]
            if key in {"run_workspace_join", "auto_refill_enabled"}:
                CONFIG[key] = bool(value)
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                return JSONResponse({"error": f"{key} must be an integer"}, status_code=400)
            limits = {
                "refill_threshold": (1, 5000), "refill_count": (1, 500),
                "refill_interval": (30, 86400), "concurrency": (1, 5),
                "timeout": (120, 3600), "phone_attempts": (1, 10),
                "sms_timeout": (30, 600),
            }
            low, high = limits[key]
            CONFIG[key] = max(low, min(high, value))
    if payload.get("sub2api_password"):
        CONFIG["sub2api_password"] = str(payload["sub2api_password"]).strip()
    if payload.get("default_password"):
        CONFIG["default_password"] = str(payload["default_password"]).strip()
    TASK_SEMAPHORE = asyncio.Semaphore(max(1, min(5, int(CONFIG.get("concurrency") or 1))))
    if CONFIG.get("auto_refill_enabled") and (REFILL_TASK is None or REFILL_TASK.done()):
        REFILL_TASK = asyncio.create_task(_refill_loop())
    elif not CONFIG.get("auto_refill_enabled") and REFILL_TASK and not REFILL_TASK.done():
        REFILL_TASK.cancel()
    await _persist()
    return {"config": _public_config()}


@app.get("/api/emails")
async def list_emails() -> dict[str, Any]:
    return {"items": [_public_email(item) for item in EMAILS.values()]}


@app.post("/api/emails/import")
async def import_emails(request: Request) -> dict[str, Any]:
    payload = await request.json()
    records, errors = _normalize_records(str((payload or {}).get("text") or ""))
    existing = {item.get("email", "").lower() for item in EMAILS.values()}
    added = 0
    for record in records:
        if record["email"] in existing:
            continue
        EMAILS[record["id"]] = record
        existing.add(record["email"])
        added += 1
    await _persist()
    return {"added": added, "skipped": len(records) - added, "invalid": len(errors), "errors": errors[:20], "total": len(EMAILS)}


def _summary() -> dict[str, Any]:
    email_counts = {status: 0 for status in ("free", "queued", "running", "success", "failed", "banned", "claimed")}
    for record in EMAILS.values():
        status = str(record.get("status") or "free")
        email_counts[status] = email_counts.get(status, 0) + 1
    task_counts = {status: 0 for status in ("queued", "running", "success", "failed", "canceled")}
    for task in TASKS.values():
        status = str(task.get("status") or "queued")
        task_counts[status] = task_counts.get(status, 0) + 1
    return {
        "emails": {"total": len(EMAILS), **email_counts},
        "tasks": {"total": len(TASKS), **task_counts},
        "workspace_ids": len(CONFIG.get("workspace_ids") or []),
        "data_root": str(DATA_ROOT),
    }


def _new_task(record: dict[str, Any], source: str = "") -> dict[str, Any]:
    task_id = f"k12_{uuid.uuid4().hex[:12]}"
    task = {
        "id": task_id,
        "email_id": record["id"],
        "email": record["email"],
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "logs": ([f"retry source={source}"] if source else []),
        "otp_mode": str(CONFIG.get("otp_mode") or "auto"),
        "waiting_otp": str(CONFIG.get("otp_mode") or "auto") == "manual",
    }
    TASKS[task_id] = task
    return task


def _alias_email(record: dict[str, Any], index: int) -> dict[str, Any]:
    email = str(record.get("email") or "")
    local, separator, domain = email.partition("@")
    if not separator:
        raise ValueError("invalid parent email")
    alias = f"{local}+k12{index}@{domain}"
    return {
        **record,
        "id": secrets.token_hex(10),
        "email": alias,
        "raw": f"{alias}----alias-of----{email}",
        "parent_email": email,
        "status": "free",
        "last_error": "",
        "updated_at": _now(),
    }


@app.get("/api/summary")
async def summary() -> dict[str, Any]:
    return _summary()


@app.get("/api/factory/status")
async def factory_status() -> dict[str, Any]:
    source = MAIN_EMAILS_PATH
    return {
        "detected": True,
        "email_pool_present": source.is_file(),
        "email_count": len([line for line in source.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]) if source.is_file() else 0,
        "token_count": len(list((_shared_data_root / "tokens").rglob("*.json"))) if (_shared_data_root / "tokens").is_dir() else 0,
    }


def _proxy_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["REG_FACTORY_PLATFORM"] = "chatgpt"
    return env


@app.get("/api/proxy")
async def proxy_status() -> dict[str, Any]:
    try:
        from common import proxy_switch

        env = _proxy_environment()
        mode = proxy_switch.proxy_mode(env)
        nodes = proxy_switch.available_clash_nodes(environ=env) if mode not in {"residential", "direct"} else []
        return {
            "ok": True,
            "mode": mode,
            "current": proxy_switch.current_node(environ=env),
            "fixed": proxy_switch.fixed_node(env),
            "nodes": nodes,
            "configured_node": CONFIG.get("network_node") or "",
            "configured_mode": CONFIG.get("network_mode") or "inherit",
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:180], "nodes": []}, status_code=503)


@app.post("/api/proxy/select")
async def select_proxy(request: Request) -> dict[str, Any]:
    payload = await request.json()
    mode = str((payload or {}).get("mode") or "inherit").strip().lower()
    node = str((payload or {}).get("node") or "").strip()
    if mode not in {"inherit", "clash_auto", "clash_fixed", "residential", "direct"}:
        return JSONResponse({"error": "不支持的网络模式"}, status_code=400)
    if mode == "clash_fixed" and not node:
        return JSONResponse({"error": "固定节点模式必须选择节点"}, status_code=400)
    try:
        from common import proxy_switch

        env = _proxy_environment()
        if node:
            proxy_switch.pin_fixed_node(node, "chatgpt", env)
        CONFIG["network_mode"] = mode
        CONFIG["network_node"] = node
        await _persist()
        return await proxy_status()
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:180]}, status_code=400)


@app.get("/api/refill/status")
async def refill_status() -> dict[str, Any]:
    free = sum(1 for record in EMAILS.values() if record.get("status") == "free")
    return {
        "enabled": bool(CONFIG.get("auto_refill_enabled")),
        "running": bool(REFILL_TASK and not REFILL_TASK.done()),
        "free": free,
        "threshold": CONFIG.get("refill_threshold", 1),
        "count": CONFIG.get("refill_count", 1),
        "interval": CONFIG.get("refill_interval", 300),
    }


@app.post("/api/refill/start")
async def start_refill() -> dict[str, Any]:
    result = await _refill_once()
    return {"result": result, "status": await refill_status()}


@app.post("/api/factory/import-emails")
async def import_factory_emails() -> dict[str, Any]:
    source = MAIN_EMAILS_PATH
    if not source.is_file():
        return JSONResponse({"error": "主项目 emails.txt 不存在"}, status_code=404)
    records, errors = _normalize_records(source.read_text(encoding="utf-8-sig"))
    existing = {item.get("email", "").lower() for item in EMAILS.values()}
    added = 0
    for record in records:
        if record["email"] in existing:
            continue
        EMAILS[record["id"]] = record
        existing.add(record["email"])
        added += 1
    await _persist()
    return {"added": added, "invalid": len(errors), "total": len(EMAILS)}


@app.post("/api/emails/delete")
async def delete_emails(request: Request) -> dict[str, Any]:
    payload = await request.json()
    ids = {str(item) for item in (payload or {}).get("ids") or [] if str(item)}
    status = str((payload or {}).get("status") or "").strip().lower()
    if status:
        ids.update(record_id for record_id, record in EMAILS.items() if record.get("status") == status)
    removed = 0
    skipped = 0
    for record_id in list(ids):
        record = EMAILS.get(record_id)
        if not record:
            continue
        if record.get("status") == "running":
            skipped += 1
            continue
        EMAILS.pop(record_id, None)
        removed += 1
    await _persist()
    return {"removed": removed, "skipped_running": skipped, "total": len(EMAILS)}


@app.delete("/api/emails/{email_id}")
async def delete_email(email_id: str) -> dict[str, Any]:
    return await delete_emails(_json_request({"ids": [email_id]}))


class _json_request:
    def __init__(self, value: dict[str, Any]):
        self.value = value

    async def json(self) -> dict[str, Any]:
        return self.value


@app.post("/api/emails/split")
async def split_emails(request: Request) -> dict[str, Any]:
    payload = await request.json()
    ids = [str(item) for item in (payload or {}).get("ids") or [] if str(item)]
    count = max(1, min(50, int((payload or {}).get("count") or 4)))
    by_email = {record.get("email", "").lower() for record in EMAILS.values()}
    created = 0
    for record_id in ids:
        parent = EMAILS.get(record_id)
        if not parent or parent.get("status") == "running":
            continue
        for index in range(1, count + 1):
            child = _alias_email(parent, index)
            if child["email"].lower() in by_email:
                continue
            EMAILS[child["id"]] = child
            by_email.add(child["email"].lower())
            created += 1
    await _persist()
    return {"created": created, "total": len(EMAILS)}


@app.get("/api/tasks")
async def list_tasks() -> dict[str, Any]:
    return {"items": [_public_task(item) for item in sorted(TASKS.values(), key=lambda value: value.get("created_at", ""), reverse=True)]}


def _token_for_email(email: str) -> str:
    token_root = DATA_ROOT / "tokens" / "chatgpt"
    if not token_root.is_dir():
        return ""
    target = str(email or "").strip().lower()
    for path in token_root.glob("*.session.json"):
        record = _read_json(path, {})
        if not isinstance(record, dict):
            continue
        user = record.get("user") if isinstance(record.get("user"), dict) else {}
        current = str(user.get("email") or record.get("email") or "").strip().lower()
        if current == target:
            return str(record.get("accessToken") or record.get("access_token") or "").strip()
    return ""


def _jwt_expired(token: str) -> bool:
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return float(claims.get("exp") or 0) <= time.time()
    except (IndexError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return False


@app.post("/api/tasks/check-at")
async def check_task_tokens(request: Request) -> dict[str, Any]:
    payload = await request.json()
    requested = {str(item) for item in (payload or {}).get("task_ids") or [] if str(item)}
    items = [task for task in TASKS.values() if not requested or task.get("id") in requested]
    results = []
    for task in items:
        token = _token_for_email(str(task.get("email") or ""))
        if not token:
            result = {"task_id": task.get("id"), "email": task.get("email"), "ok": False, "inactive": True, "status": 0, "message": "未找到本地 access token"}
        elif _jwt_expired(token):
            result = {"task_id": task.get("id"), "email": task.get("email"), "ok": False, "inactive": True, "status": 401, "message": "access token 已过期"}
        else:
            try:
                from common.asset_scanner import _scan_chatgpt_plus_trial
                checked = await asyncio.to_thread(_scan_chatgpt_plus_trial, {"email": task.get("email")}, token, 20)
                ok = checked.get("plus_trial") not in {"unknown"}
                result = {"task_id": task.get("id"), "email": task.get("email"), "ok": ok, "inactive": not ok, "status": 200 if ok else 401, "message": str(checked.get("plus_trial_detail") or "AT 检查完成")}
            except Exception as exc:
                result = {"task_id": task.get("id"), "email": task.get("email"), "ok": False, "inactive": True, "status": 0, "message": f"AT 检查失败: {type(exc).__name__}"}
        task["at_status"] = "alive" if result["ok"] else "inactive"
        task["at_checked_at"] = _now()
        task["at_message"] = result["message"]
        results.append(result)
    await _persist()
    return {"checked": len(results), "ok": sum(1 for item in results if item["ok"]), "inactive": sum(1 for item in results if item["inactive"]), "items": results}


@app.post("/api/tasks/repair-at")
async def repair_inactive_at(request: Request) -> dict[str, Any]:
    """Queue a fresh OAuth run for accounts whose last AT check was inactive."""
    payload = await request.json()
    requested = {str(item) for item in (payload or {}).get("task_ids") or [] if str(item)}
    candidates = [
        task for task in TASKS.values()
        if task.get("at_status") == "inactive" and (not requested or task.get("id") in requested)
    ]
    seen: set[str] = set()
    created = []
    for source in sorted(candidates, key=lambda item: item.get("at_checked_at", ""), reverse=True):
        email_id = str(source.get("email_id") or "")
        if not email_id or email_id in seen:
            continue
        record = EMAILS.get(email_id)
        if not record or record.get("status") in {"running", "queued"}:
            continue
        seen.add(email_id)
        task = _new_task(record, source="repair-at")
        record.update({"status": "queued", "updated_at": _now(), "last_error": ""})
        created.append(_public_task(task))
        asyncio.create_task(_run_task(task["id"], record["id"]))
        if len(created) >= 100:
            break
    await _persist()
    return {"tasks": created, "count": len(created)}


@app.get("/api/data/export")
async def export_data() -> Response:
    bundle = {
        "version": "1.0",
        "exported_at": _now(),
        "config": dict(CONFIG),
        "emails": list(EMAILS.values()),
        "tasks": list(TASKS.values()),
    }
    content = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="k12-data-{time.strftime("%Y%m%d-%H%M%S")}.json"'},
    )


@app.post("/api/data/import")
async def import_data(request: Request) -> dict[str, Any]:
    try:
        bundle = await request.json()
    except Exception:
        return JSONResponse({"error": "导入文件必须是 JSON"}, status_code=400)
    if not isinstance(bundle, dict):
        return JSONResponse({"error": "导入数据格式不正确"}, status_code=400)
    imported_emails = bundle.get("emails") if isinstance(bundle.get("emails"), list) else []
    imported_tasks = bundle.get("tasks") if isinstance(bundle.get("tasks"), list) else []
    if len(imported_emails) > 5000 or len(imported_tasks) > 10000:
        return JSONResponse({"error": "导入数据超过上限"}, status_code=413)
    backup = DATA_ROOT / "runtime" / f"backup-{int(time.time())}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({"config": CONFIG, "emails": EMAILS, "tasks": TASKS}, ensure_ascii=False), encoding="utf-8")
    EMAILS.clear()
    TASKS.clear()
    for item in imported_emails:
        if not isinstance(item, dict) or not item.get("email"):
            continue
        record = dict(item)
        record.setdefault("id", secrets.token_hex(10))
        record.setdefault("status", "free")
        EMAILS[str(record["id"])] = record
    for item in imported_tasks:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        TASKS[str(item["id"])] = dict(item)
    imported_config = bundle.get("config") if isinstance(bundle.get("config"), dict) else {}
    for key in ("workspace_ids", "workspace_route", "run_workspace_join", "group", "concurrency", "timeout", "phone_attempts", "sms_timeout", "sms_provider", "otp_mode", "node", "network_mode", "network_node", "auto_refill_enabled", "refill_threshold", "refill_count", "refill_interval", "sub2api_url", "sub2api_email", "sub2api_password", "default_password"):
        if key in imported_config:
            CONFIG[key] = imported_config[key]
    await _persist()
    return {"emails": len(EMAILS), "tasks": len(TASKS), "backup": str(backup)}


@app.post("/api/tasks")
async def create_tasks(request: Request) -> dict[str, Any]:
    payload = await request.json()
    _refresh_shared_email_pool()
    requested = [str(item) for item in (payload or {}).get("email_ids") or [] if str(item)]
    selected = [EMAILS[item] for item in requested if item in EMAILS and EMAILS[item].get("status") == "free"]
    if not selected:
        try:
            count = max(1, min(500, int((payload or {}).get("count") or 1)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "count 必须是整数"}, status_code=400)
        selected = [item for item in EMAILS.values() if item.get("status") == "free"][:count]
    if CONFIG.get("run_workspace_join") and not CONFIG.get("workspace_ids"):
        return JSONResponse({"error": "请先配置 K12 Workspace ID"}, status_code=400)
    created = []
    for record in selected:
        if record.get("status") != "free":
            continue
        if record.get("pool_source") == "main":
            _reserve_shared_email(record)
        task = _new_task(record)
        task_id = task["id"]
        record.update({"status": "queued", "updated_at": _now(), "last_error": ""})
        created.append(_public_task(task))
        asyncio.create_task(_run_task(task_id, record["id"]))
    await _persist()
    return {"tasks": created, "count": len(created)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return {"task": _public_task(task)}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    process = PROCESSES.get(task_id)
    if process:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    task["status"] = "canceled"
    if not process:
        record = EMAILS.get(str(task.get("email_id") or ""))
        if record and record.get("status") in {"queued", "running"}:
            record.update({"status": "free", "updated_at": _now()})
            _mark_shared_result(record, False, "k12_canceled")
    await _persist()
    return {"task": _public_task(task)}


@app.post("/api/tasks/{task_id}/otp")
async def submit_otp(task_id: str, request: Request) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    if task.get("otp_mode") != "manual" or not task.get("waiting_otp"):
        return JSONResponse({"error": "task is not waiting for manual OTP"}, status_code=409)
    payload = await request.json()
    code = str((payload or {}).get("code") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return JSONResponse({"error": "OTP 必须是 6 位数字"}, status_code=400)
    path = RUNTIME_ROOT / "otp" / f"{task_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    task["waiting_otp"] = False
    task["updated_at"] = _now()
    await _persist()
    return {"ok": True, "task": _public_task(task)}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str) -> dict[str, Any]:
    source = TASKS.get(task_id)
    if not source:
        return JSONResponse({"error": "task not found"}, status_code=404)
    if source.get("status") not in {"failed", "canceled"}:
        return JSONResponse({"error": "只能重试失败或已取消的任务"}, status_code=409)
    record = EMAILS.get(str(source.get("email_id") or ""))
    if not record:
        return JSONResponse({"error": "邮箱记录不存在"}, status_code=404)
    if record.get("status") == "running":
        return JSONResponse({"error": "邮箱正在运行"}, status_code=409)
    task = _new_task(record, source=task_id)
    if record.get("pool_source") == "main":
        _reserve_shared_email(record)
    record.update({"status": "queued", "updated_at": _now()})
    await _persist()
    asyncio.create_task(_run_task(task["id"], record["id"]))
    return {"task": _public_task(task)}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    if task.get("status") not in {"failed", "canceled"}:
        return JSONResponse({"error": "只能删除失败或已取消的任务"}, status_code=409)
    TASKS.pop(task_id, None)
    await _persist()
    return {"removed": 1}


@app.post("/api/tasks/clear-failed")
async def clear_failed_tasks() -> dict[str, Any]:
    failed = [task_id for task_id, task in TASKS.items() if task.get("status") == "failed"]
    for task_id in failed:
        TASKS.pop(task_id, None)
    await _persist()
    return {"removed": len(failed)}


@app.get("/api/tasks/{task_id}/logs")
async def task_logs(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    return {"task": _public_task(task), "logs": task.get("logs", [])}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("k12.server:app", host="127.0.0.1", port=int(os.environ.get("K12_PORT", "8806")), reload=False)
