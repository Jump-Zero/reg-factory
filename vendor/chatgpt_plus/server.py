"""Standalone direct-bind HTTP service on 127.0.0.1:5601."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from standalone_flow import (
    allocate_fingerprint,
    allocate_fingerprints,
    preflight,
    resolve_account,
    run_flow,
    validate_payload,
)
from standalone_core.fingerprint_store import optimize_fingerprint_store

ROOT = Path(__file__).resolve().parent
ALIGNED_BATCH_LIMIT = 27
QUEUE_IMPORT_LIMIT = 100
TASKS: dict[str, dict[str, Any]] = {}
TASKS_LOCK = threading.Lock()
QUEUE_LOCK = threading.Lock()
STEP_EVENT_RE = re.compile(r"^FLOW_STEP:([a-z_]+):(start|done|error):(.*)$")
STEP_LABELS = {
    "proxy": "分配代理",
    "card_method": "生成支付方式",
    "fingerprint": "加载指纹",
    "first_link": "首次提链",
    "bind": "绑卡",
    "payment_prepare": "验证支付方式",
    "second_link": "二次提链",
    "payment": "提交支付",
    "verify": "结果校验",
}


def _queue_path() -> Path:
    raw = os.environ.get("REG_FACTORY_PLUS_QUEUE_FILE", "").strip()
    return Path(raw).resolve() if raw else ROOT / "registration_queue.json"


def _registration_queue() -> dict[str, Any]:
    path = _queue_path()
    if not path.is_file():
        return {"ok": True, "max_concurrency": ALIGNED_BATCH_LIMIT, "items": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "max_concurrency": ALIGNED_BATCH_LIMIT, "items": []}
    items = []
    for entry in payload.get("items", []):
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            continue
        try:
            session = json.loads(Path(str(entry.get("session_path") or "")).read_text(encoding="utf-8"))
            token = str(session.get("accessToken") or "").strip()
        except (OSError, ValueError, TypeError):
            continue
        if token:
            items.append({"id": str(entry.get("id") or ""), "email": str(entry.get("email") or ""), "access_token": token})
            if len(items) >= QUEUE_IMPORT_LIMIT:
                break
    return {"ok": True, "max_concurrency": ALIGNED_BATCH_LIMIT, "items": items}


def _runtime_defaults() -> dict[str, Any]:
    link_proxy, bind_proxy = _runtime_proxy_values()
    link_node = str(os.environ.get("REG_FACTORY_PLUS_LINK_CLASH_NODE") or "").strip()
    bind_node = str(os.environ.get("REG_FACTORY_PLUS_BIND_CLASH_NODE") or "").strip()
    fixed_node_serialized = bool(
        link_node and bind_node and link_node != bind_node and link_proxy == bind_proxy
    )
    return {
        "ok": True,
        "proxy_inherited": bool(link_proxy or bind_proxy),
        "link_proxy_inherited": bool(link_proxy),
        "bind_proxy_inherited": bool(bind_proxy),
        "proxy_split": bool(link_proxy and bind_proxy and (link_proxy != bind_proxy or link_node != bind_node)),
        "link_clash_node": link_node,
        "bind_clash_node": bind_node,
        "fixed_node_serialized": fixed_node_serialized,
        "route_notice": (
            "两个固定节点共用同一 Clash 监听端口，正式提链/绑卡会保护节点切换并串行执行；"
            "要并发请给两个阶段配置不同监听端口，或改用住宅 IP/Clash 当前节点。"
            if fixed_node_serialized else ""
        ),
        "max_concurrency": ALIGNED_BATCH_LIMIT,
    }


def _runtime_proxy_values() -> tuple[str, str]:
    legacy = str(
        os.environ.get("REG_FACTORY_PLUS_PROXY")
        or os.environ.get("REG_FACTORY_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    link_proxy = str(os.environ.get("REG_FACTORY_PLUS_LINK_PROXY") or legacy).strip()
    bind_proxy = str(os.environ.get("REG_FACTORY_PLUS_BIND_PROXY") or legacy).strip()
    return link_proxy, bind_proxy


def _with_runtime_proxy(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply stage-specific parent egress without exposing proxy controls."""
    link_proxy, bind_proxy = _runtime_proxy_values()
    result = dict(payload)
    if not result.get("bind_proxy_pool") and not result.get("proxy_pool"):
        result["bind_proxy_pool"] = [bind_proxy] if bind_proxy else []
    if not result.get("promo_proxy_pool"):
        result["promo_proxy_pool"] = [link_proxy] if link_proxy else []
    return result


def _ack_registration(queue_id: str) -> bool:
    path = _queue_path()
    with QUEUE_LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        changed = False
        for entry in payload.get("items", []):
            if isinstance(entry, dict) and entry.get("id") == queue_id:
                entry["status"] = "opened"
                changed = True
                break
        if changed:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        return changed


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _safe_error(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)(access_token|authorization|client_secret|payment_method|card_number|cvc)([=:])[^&\s,}]+",
        r"\1\2<REDACTED>",
        text,
    )
    lower = text.lower()
    if "curl: (35)" in lower or "tls connect error" in lower or "openssl_internal:invalid library" in lower:
        return "代理 TLS 握手失败，请检查代理协议、地址、端口和有效期。"
    if "card_declined" in lower or "your card was declined" in lower:
        return "绑卡未完成：发卡行拒绝了卡片，账号没有新增支付方式。"
    if "secret_key_required" in lower:
        return "支付接口权限不匹配，当前步骤没有获得所需的服务端权限。"
    if "invalidated" in lower or "token expired" in lower or "http 401" in lower:
        return "账号凭证已失效，请删除后重新导入。"
    if "at does not contain an account id" in lower or "at 缺少账号 id" in lower:
        return "该条不是可用的 AT（缺少账号 ID），请导入 access_token。"
    if "零元校验失败" in lower or "expected zero amount" in lower:
        return "该账号当前未命中 Plus 免费优惠，Checkout 返回非零金额；已停止，未进行绑卡或扣款。"
    if "预检提链固定节点" in text or "预检提链节点连通性失败" in text:
        return text[:500]
    if "timeout" in lower or "timed out" in lower:
        return "请求超时，请检查代理速度后重试。"
    if "preflight bind proxy pool exhausted" in lower:
        return "预检失败：绑卡/支付代理池候选均连接失败，请检查节点状态、协议和有效期。"
    if "提链代理池已耗尽" in lower:
        return "预检失败：提链代理池候选均连接失败，请检查节点状态、协议和有效期。"
    if "proxy" in lower and any(word in lower for word in ("connect", "failed", "error")):
        return "代理连接失败，请检查代理格式、节点状态和网络。"
    if "checkout confirm precondition" in lower:
        return "订阅尚未提交：缺少当前 Checkout 会话生成的动态校验数据。"
    if "checkout confirm" in lower and "server returned blocked" in lower:
        return "订阅请求已提交，账号风控返回 status=blocked；订阅未生效，未进入 Stripe Intent，自动重试已关闭。"
    if "checkout confirm" in lower and "http 403" in lower:
        return "提交支付确认被拒绝（HTTP 403）：当前 Checkout 的会话校验数据无效或缺失。"
    if "confirmation token" in lower and "http 403" in lower:
        return "Stripe Confirmation Token 请求被拒绝（HTTP 403），请检查支付方式与商户公钥是否匹配。"
    if "http 403" in lower:
        return "请求被拒绝（HTTP 403），请检查账号状态和接口权限。"
    if "http 402" in lower:
        return "支付请求被拒绝（HTTP 402），请检查卡片状态。"
    if "failed to perform, curl" in lower:
        return "网络请求失败，请检查代理和本机网络。"
    return text[:500]


def _checkout_link(result: dict[str, Any]) -> str:
    """Return a canonical Checkout link even when one response field is absent."""
    direct = str(result.get("checkout_link") or "").strip()
    if direct.startswith("https://chatgpt.com/checkout/"):
        return direct
    checkout_id = str(result.get("checkout_id") or "").strip()
    processor = str(result.get("processor_entity") or "").strip()
    if (
        re.fullmatch(r"(?:oaics_|cs_)[A-Za-z0-9_-]+", checkout_id)
        and re.fullmatch(r"[A-Za-z0-9_-]+", processor)
    ):
        return f"https://chatgpt.com/checkout/{processor}/{checkout_id}"
    return ""


def _set_task(task_id: str, **values: Any) -> None:
    with TASKS_LOCK:
        task = TASKS.setdefault(task_id, {"id": task_id})
        task.update(values)


def _initial_steps(mode: str) -> list[dict[str, str]]:
    keys = ["proxy"]
    if mode != "link_only":
        keys.append("card_method")
    keys.extend(["fingerprint", "first_link"])
    if mode == "bind_only":
        keys.append("bind")
    elif mode == "link_pay":
        keys.extend(["payment_prepare", "second_link", "payment", "verify"])
    elif mode != "link_only":
        keys.extend(["bind", "second_link", "payment", "verify"])
    return [
        {
            "key": key,
            "label": (
                "刷新支付链接"
                if mode == "link_pay" and key == "second_link"
                else STEP_LABELS[key]
            ),
            "status": "done" if key in {"proxy", "card_method"} else "pending",
            "detail": (
                ("提链代理已分配" if mode == "link_only" else "双代理已分配") if key == "proxy" else
                "浏览器支付方式已创建" if key == "card_method" else "等待执行"
            ),
        }
        for key in keys
    ]


def _update_step(task_id: str, key: str, status: str, detail: str) -> None:
    with TASKS_LOCK:
        task = TASKS.setdefault(task_id, {"id": task_id})
        steps = list(task.get("steps") or [])
        step = next((item for item in steps if item.get("key") == key), None)
        if step is None:
            step = {
                "key": key,
                "label": STEP_LABELS.get(key, key),
                "status": "pending",
                "detail": "等待执行",
            }
            steps.append(step)
        step["status"] = status
        step["detail"] = _safe_error(detail)[:240] or step.get("detail", "")
        done = sum(1 for item in steps if item.get("status") == "done")
        progress = min(99, max(5, round(done * 100 / max(1, len(steps)))))
        task.update(
            steps=steps,
            progress=progress,
            current_step=key,
            stage=f"{step['label']}：{step['detail']}",
        )


def _update_live_log(task_id: str, logs: list[str], text: str) -> None:
    with TASKS_LOCK:
        task = TASKS.setdefault(task_id, {"id": task_id})
        steps = list(task.get("steps") or [])
        running = next(
            (item for item in reversed(steps) if item.get("status") == "running"),
            None,
        )
        if running is not None:
            running["detail"] = text[:240]
            task["stage"] = f"{running['label']}：{running['detail']}"
        task["steps"] = steps
        task["logs"] = logs[-40:]


def _fail_running_step(task_id: str, error: str) -> None:
    with TASKS_LOCK:
        task = TASKS.setdefault(task_id, {"id": task_id})
        steps = list(task.get("steps") or [])
        running = next(
            (item for item in reversed(steps) if item.get("status") == "running"),
            None,
        )
        if running is not None:
            running["status"] = "error"
            running["detail"] = error[:240]
            task["current_step"] = running.get("key")
            task["stage"] = f"{running['label']}失败：{error[:180]}"
        task["steps"] = steps


def _run_task(
    task_id: str,
    payload: dict[str, Any],
    start_at_monotonic: float | None = None,
    start_group: str = "",
) -> None:
    logs: list[str] = []

    def log(message: str) -> None:
        event = STEP_EVENT_RE.match(str(message or ""))
        if event:
            key, state, detail = event.groups()
            _update_step(task_id, key, state, detail)
            return
        text = _safe_error(message)
        if text:
            logs.append(text)
            del logs[:-40]
            _update_live_log(task_id, logs, text)

    mode = str(payload.get("flow_mode") or "full").strip().lower()
    if start_at_monotonic is not None:
        _set_task(
            task_id,
            status="queued",
            progress=1,
            stage="等待同批账号同步起跑",
            start_group=start_group,
        )
        delay = start_at_monotonic - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    started_at_ns = time.time_ns()
    _set_task(
        task_id,
        status="running",
        progress=5,
        stage="准备执行账号流程",
        current_step="fingerprint",
        steps=_initial_steps(mode),
        logs=[],
        start_group=start_group,
        started_at=started_at_ns / 1_000_000_000,
        started_at_ns=started_at_ns,
    )
    try:
        result = run_flow(payload, logger=log)
        if result.get("action_required") == "checkout_session_confirmation":
            checkout_link = _checkout_link(result)
            pending_result = {
                **result,
                "checkout_link": checkout_link,
                "logs": logs[-40:],
            }
            _update_step(
                task_id,
                "payment",
                "pending",
                (
                    "等待对应 Checkout 会话确认；链接已保留在当前行"
                    if checkout_link
                    else "等待对应 Checkout 会话确认；Checkout 标识缺失"
                ),
            )
            _set_task(
                task_id,
                status="action_required",
                progress=92,
                stage=(
                    "订阅待确认：使用当前行的 Checkout 链接继续"
                    if checkout_link
                    else "订阅待确认：Checkout 链接生成失败"
                ),
                result=pending_result,
                finished_at=time.time(),
            )
            return
        _set_task(
            task_id,
            status="done",
            progress=100,
            stage="独立 HTTP 流程完成",
            result={**result, "logs": logs[-40:]},
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        safe_error = _safe_error(exc)
        _fail_running_step(task_id, safe_error)
        _set_task(
            task_id,
            status="error",
            progress=100,
            error=safe_error,
            logs=logs[-40:],
            finished_at=time.time(),
        )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/") and path != "/health":
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"ok": True, "service": "reg-factory-chatgpt-plus", "max_concurrency": ALIGNED_BATCH_LIMIT})
            return
        if path == "/api/registration-queue":
            self._send_json(200, _registration_queue())
            return
        if path == "/api/runtime-defaults":
            self._send_json(200, _runtime_defaults())
            return
        if path == "/api/standalone/info":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "direct-bind-standalone",
                    "upstream_app": False,
                    "project_database": False,
                    "fingerprint_provider": "registration-profile",
                    "fingerprint_sticky_per_account": True,
                    "fingerprint_store_version": 3,
                    "fingerprint_cross_layer_audit": True,
                    "fingerprint_batch_limit": 500,
                    "aligned_batch_limit": ALIGNED_BATCH_LIMIT,
                    "task_count": len(TASKS),
                },
            )
            return
        if path.startswith("/api/standalone-flow/task/"):
            task_id = path.rsplit("/", 1)[-1]
            with TASKS_LOCK:
                task = dict(TASKS.get(task_id) or {})
            if not task:
                self._send_json(404, {"ok": False, "error": "task not found"})
                return
            self._send_json(200, {"ok": True, "task": task})
            return
        if path in {"", "/"}:
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/registration-queue/ack":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                self._send_json(200, {"ok": _ack_registration(str(payload.get("id") or ""))})
            except (ValueError, TypeError):
                self._send_json(400, {"ok": False, "error": "invalid queue acknowledgement"})
            return
        if path == "/api/standalone-flow/tasks/clear":
            with TASKS_LOCK:
                TASKS.clear()
            self._send_json(200, {"ok": True})
            return
        if path not in {
            "/api/fingerprint/allocate",
            "/api/fingerprint/allocate-batch",
            "/api/account/resolve",
            "/api/card-bind/session",
            "/api/standalone-flow/quick-checkout",
            "/api/standalone-flow/quick-checkout-batch",
        }:
            self._send_json(404, {"ok": False, "error": "route not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid request body length")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            payload = _with_runtime_proxy(payload)
            if path == "/api/card-bind/session":
                result = preflight(payload)
                if not str(result.get("publishable_key") or "").startswith("pk_"):
                    raise RuntimeError("preflight did not return a publishable key")
                self._send_json(200, result)
                return
            if path == "/api/account/resolve":
                self._send_json(200, resolve_account(payload))
                return
            if path == "/api/fingerprint/allocate":
                self._send_json(200, allocate_fingerprint(payload))
                return
            if path == "/api/fingerprint/allocate-batch":
                self._send_json(200, allocate_fingerprints(payload))
                return
            if path == "/api/standalone-flow/quick-checkout-batch":
                source = payload.get("tasks")
                if not isinstance(source, list) or not source:
                    raise ValueError("tasks must be a non-empty list")
                if len(source) > ALIGNED_BATCH_LIMIT:
                    raise ValueError(
                        f"aligned batch cannot exceed {ALIGNED_BATCH_LIMIT} accounts"
                    )
                prepared: list[tuple[str, dict[str, Any], str]] = []
                for item in source:
                    if not isinstance(item, dict):
                        raise ValueError("task item must be an object")
                    client_id = str(item.get("client_id") or "")[:100]
                    task_payload = item.get("payload")
                    if not isinstance(task_payload, dict):
                        raise ValueError("task payload must be an object")
                    task_payload = _with_runtime_proxy(task_payload)
                    validate_payload(task_payload, require_payment_method=True)
                    prepared.append((client_id, task_payload, uuid.uuid4().hex))
                start_group = uuid.uuid4().hex
                start_delay_ms = max(
                    100, min(1000, int(payload.get("start_delay_ms") or 250))
                )
                start_at_monotonic = time.perf_counter() + (start_delay_ms / 1000)
                start_at_epoch = time.time() + (start_delay_ms / 1000)
                for client_id, task_payload, task_id in prepared:
                    _set_task(
                        task_id,
                        status="queued",
                        progress=0,
                        stage="等待同批账号同步起跑",
                        start_group=start_group,
                    )
                    threading.Thread(
                        target=_run_task,
                        args=(task_id, task_payload, start_at_monotonic, start_group),
                        daemon=True,
                    ).start()
                self._send_json(
                    202,
                    {
                        "ok": True,
                        "start_group": start_group,
                        "start_at": start_at_epoch,
                        "items": [
                            {"client_id": client_id, "task_id": task_id}
                            for client_id, _task_payload, task_id in prepared
                        ],
                    },
                )
                return
            validate_payload(payload, require_payment_method=True)
            task_id = uuid.uuid4().hex
            _set_task(task_id, status="queued", progress=0, stage="独立 HTTP 任务排队")
            threading.Thread(target=_run_task, args=(task_id, payload), daemon=True).start()
            self._send_json(202, {"ok": True, "task_id": task_id})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": _safe_error(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"ok": False, "error": _safe_error(exc)})


if __name__ == "__main__":
    fingerprint_status = optimize_fingerprint_store("US")
    server = ThreadingHTTPServer(("127.0.0.1", 5601), Handler)
    print(
        "direct-bind standalone -> http://127.0.0.1:5601/ "
        f"fingerprints={fingerprint_status}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
