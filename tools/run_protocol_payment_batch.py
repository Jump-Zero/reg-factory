#!/usr/bin/env python3
"""Run a token-backed protocol link extraction or explicit PayPal payment task."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.protocol_payment import (
    payment_method,
    paypal_payment_config_ready,
    paypal_payment_ready,
    resolve_protocol_engine_root,
)


_TOKEN_RE = re.compile(r"(?i)(access[_-]?token|authorization|refresh[_-]?token)([=:])[^\s,}]+")
_WORKER_CAP = {"kakao": 2, "momo": 2}


def _safe_text(value: object) -> str:
    text = _TOKEN_RE.sub(r"\1\2<redacted>", str(value or ""))
    text = re.sub(r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)", "<redacted-card>", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text)
    return text.replace("\r", " ").replace("\n", " ")[:360]


def _load_task(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"无法读取协议任务账号文件: {type(exc).__name__}") from exc
    if isinstance(raw, list):
        raw_accounts = raw
        payment_config: object = {}
    elif isinstance(raw, dict):
        raw_accounts = raw.get("accounts")
        payment_config = raw.get("payment_config") or {}
    else:
        raise ValueError("协议任务文件必须是对象或兼容的账号数组")
    if not isinstance(raw_accounts, list):
        raise ValueError("协议任务 accounts 必须是数组")
    if not isinstance(payment_config, dict):
        raise ValueError("协议任务 payment_config 必须是对象")
    accounts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_accounts:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        token = str(item.get("access_token") or "").strip()
        if not email or not token or email in seen:
            continue
        seen.add(email)
        accounts.append({"email": email, "access_token": token, "account_id": str(item.get("account_id") or "").strip()})
    if not accounts:
        raise ValueError("没有可执行协议提链的有效 AT")
    return accounts, payment_config


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _load_engine_config(root: Path) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for name in ("config.example.json", "config.json"):
        try:
            loaded = json.loads((root / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(loaded, dict):
            config = _deep_merge(config, loaded)
    return config


def _proxy_pool(value: str) -> list[str]:
    return list(dict.fromkeys(
        item.strip()
        for item in re.split(r"[\r\n,;]+", str(value or ""))
        if item.strip()
    ))


def _stage_countries(spec: dict[str, Any]) -> dict[str, str]:
    country = str(spec.get("country") or "").strip().upper()
    promotion = "TH" if spec["id"] == "gopay" else "VN" if spec["id"] in {"ideal", "kakao", "twint"} else country
    approve = "JP" if spec["id"] == "gopay" else country
    return {
        "auth_gate": country,
        "checkout": country,
        "promotion": promotion,
        "stripe_init": country,
        "payment_method": country,
        "confirm": country,
        "approve": approve,
        "redirect": country,
        "poll": country,
    }


def _route_options(
    spec: dict[str, Any], checkout_proxy: str, approve_proxy: str, timeout: int
) -> dict[str, Any]:
    country = str(spec.get("country") or "").strip().upper()
    checkout_pool = _proxy_pool(checkout_proxy)
    approve_pool = _proxy_pool(approve_proxy) or list(checkout_pool)
    options: dict[str, Any] = {
        "target_country": country,
        "checkout_country": country,
        "approve_country": "JP" if spec["id"] == "gopay" else country,
        "stage_proxy_countries": _stage_countries(spec),
        "timeout_seconds": timeout,
    }
    if checkout_pool:
        options["checkout_proxy_pool"] = checkout_pool
    if approve_pool:
        options["approve_proxy_pool"] = approve_pool
    return options


def _runtime_config(root: Path, method_id: str, checkout_proxy: str, approve_proxy: str, timeout: int) -> dict[str, Any]:
    spec = payment_method(method_id, root) or {"id": method_id, "country": "US"}
    config = _load_engine_config(root)
    chatgpt = config.get("chatgpt") if isinstance(config.get("chatgpt"), dict) else {}
    chatgpt.setdefault("auth_base_url", "https://auth.openai.com")
    chatgpt.setdefault("chat_base_url", "https://chatgpt.com")
    config["chatgpt"] = chatgpt
    protocol = config.get("protocol_payments") if isinstance(config.get("protocol_payments"), dict) else {}
    protocol["enabled_methods"] = [method_id]
    protocol["reference_root"] = str(root / "services" / "protocol-payment")
    protocol["timeout_seconds"] = timeout
    methods = protocol.get("methods") if isinstance(protocol.get("methods"), dict) else {}
    method_config = methods.get(method_id) if isinstance(methods.get(method_id), dict) else {}
    route = _route_options(spec, checkout_proxy, approve_proxy, timeout)
    method_config = _deep_merge(method_config, {
        "checkout_proxy_pool": route.get("checkout_proxy_pool", []),
        "approve_proxy_pool": route.get("approve_proxy_pool", []),
        "stage_proxy_countries": route["stage_proxy_countries"],
        "timeout_seconds": timeout,
    })
    methods[method_id] = method_config
    protocol["methods"] = methods
    protocol["proxy_pools"] = {
        **(protocol.get("proxy_pools") if isinstance(protocol.get("proxy_pools"), dict) else {}),
        "checkout": route.get("checkout_proxy_pool", []),
        "approve": route.get("approve_proxy_pool", []),
    }
    config["protocol_payments"] = protocol
    return config


def _public_result(email: str, result: object) -> dict[str, Any]:
    data = dict(result) if isinstance(result, dict) else {"ok": False, "error": str(result)}
    for key in tuple(data):
        if any(part in key.lower() for part in ("token", "authorization", "secret", "cookie")):
            data.pop(key, None)
    return {
        "email": email,
        "ok": bool(data.get("ok")),
        "status": str(data.get("status") or ""),
        "operation": str(data.get("operation") or "extract_link"),
        "url": str(data.get("url") or ""),
        "qr_data": str(data.get("qr_data") or ""),
        "error": _safe_text(data.get("error") or data.get("message") or ""),
        "error_code": str(data.get("error_code") or ""),
        "payment_status": str(data.get("paypal_status") or data.get("payment_status") or ""),
    }


def _result_detail(row: dict[str, Any]) -> str:
    artifact = str(row.get("url") or "") or ("QR 已生成" if row.get("qr_data") else "")
    return str(
        row.get("payment_status")
        or artifact
        or row.get("error")
        or row.get("error_code")
        or "无可用协议结果"
    )


def _execute_paypal_payment(
    item: dict[str, str],
    *,
    runtime_config: dict[str, Any],
    paypal_link: dict[str, Any],
    proxy: str,
    timeout: int,
) -> dict[str, Any]:
    from sms_tool.config import runtime_config_scope
    from sms_tool.paypal_auto import auto_pay

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".session.json", delete=False)
    session_path = Path(handle.name)
    try:
        with handle:
            json.dump(
                {
                    "email": item["email"],
                    "accessToken": item["access_token"],
                    "account_id": item["account_id"],
                    "account": {"id": item["account_id"], "planType": "free"},
                    "user": {"email": item["email"]},
                    "paypal": {
                        "url": str(paypal_link.get("url") or ""),
                        "link_type": str(paypal_link.get("link_type") or ""),
                    },
                },
                handle,
                ensure_ascii=False,
            )
        with runtime_config_scope(runtime_config, workflow="payment"):
            result = auto_pay(
                session_file=str(session_path),
                proxy=proxy or None,
                headless=True,
                timeout=timeout,
            )
        public = _public_result(item["email"], result)
        public["operation"] = "execute_payment"
        return public
    finally:
        try:
            session_path.unlink(missing_ok=True)
        except OSError:
            pass


def _paypal_runtime_config(
    engine_root: Path,
    payment_config: dict[str, Any],
    input_path: Path,
) -> tuple[dict[str, Any], tuple[Path, Path]]:
    try:
        base = json.loads((engine_root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        base = {}
    if not isinstance(base, dict):
        base = {}
    chatgpt = base.get("chatgpt")
    if not isinstance(chatgpt, dict):
        chatgpt = {}
    chatgpt.setdefault("auth_base_url", "https://auth.openai.com")
    chatgpt.setdefault("chat_base_url", "https://chatgpt.com")
    base["chatgpt"] = chatgpt

    configured = base.get("paypal_auto")
    merged = dict(configured) if isinstance(configured, dict) else {}
    if payment_config:
        merged.update(payment_config)
    card_index = input_path.with_suffix(".card-index")
    phone_index = input_path.with_suffix(".phone-index")
    merged["card_index_file"] = str(card_index)
    merged["phone_index_file"] = str(phone_index)
    base["paypal_auto"] = merged
    return base, (card_index, phone_index)


def _paypal_config_for_route(
    base: dict[str, Any], spec: dict[str, Any], routed: dict[str, Any]
) -> dict[str, Any]:
    config = deepcopy(base)
    paypal = config.get("paypal") if isinstance(config.get("paypal"), dict) else {}
    paypal["target_country"] = spec["country"]
    paypal["checkout_country"] = spec["country"]
    paypal["stage_proxy_countries"] = dict(routed.get("stage_proxy_countries") or {})
    stage_proxies = paypal.get("stage_proxies") if isinstance(paypal.get("stage_proxies"), dict) else {}
    for stage in (
        "checkout", "promotion", "provider", "stripe_init", "payment_method",
        "confirm", "approve", "redirect", "poll",
    ):
        value = str(routed.get(f"{stage}_proxy") or "").strip()
        if value:
            stage_proxies[stage] = value
    paypal["stage_proxies"] = stage_proxies
    config["paypal"] = paypal
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="reg-factory batch protocol link extractor")
    parser.add_argument("--accounts-file", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--operation", choices=("extract", "pay"), default="extract")
    parser.add_argument("--payment-confirmed", action="store_true")
    parser.add_argument("--engine-root", default="")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--report", default="")
    parser.add_argument("--delete-input", action="store_true")
    args = parser.parse_args()

    engine_root = resolve_protocol_engine_root(args.engine_root)
    if not engine_root:
        raise SystemExit("[protocol][FAIL] 未找到协议引擎；设置 REG_FACTORY_PROTOCOL_PAYMENT_ROOT 后重试")
    spec = payment_method(args.method, engine_root)
    if not spec:
        raise SystemExit("[protocol][FAIL] 不支持的协议渠道")
    if not spec["batch_enabled"]:
        raise SystemExit(f"[protocol][FAIL] {spec['label']} 上游协议不支持批量")
    if args.operation == "pay":
        if spec["id"] != "paypal":
            raise SystemExit("[protocol][FAIL] 当前只有 PayPal 支持批量协议直接支付")
        if not args.payment_confirmed:
            raise SystemExit("[protocol][FAIL] 缺少真实支付确认，任务未执行")

    input_path = Path(args.accounts_file).resolve()
    index_paths: tuple[Path, ...] = ()
    try:
        accounts, payment_config = _load_task(input_path)
        if args.operation == "pay" and not (
            paypal_payment_config_ready(payment_config) or paypal_payment_ready(engine_root)
        ):
            raise SystemExit("[protocol][FAIL] 未提供可用的 PayPal 卡片、地址和手机号接码资料")
        checkout_proxy = os.environ.get("REG_FACTORY_PROTOCOL_CHECKOUT_PROXY", "").strip()
        approve_proxy = os.environ.get("REG_FACTORY_PROTOCOL_APPROVE_PROXY", "").strip()
        timeout = max(30, min(900, int(args.timeout or 300)))
        workers = 1 if args.operation == "pay" else max(1, min(int(args.workers or 1), _WORKER_CAP.get(spec["id"], 4), len(accounts)))
        os.chdir(engine_root)
        if str(engine_root) not in sys.path:
            sys.path.insert(0, str(engine_root))
        from sms_tool.payment_link_manager import generate_payment_link
        from sms_tool.payment_routing import PaymentRoutePlanner

        config = _runtime_config(engine_root, spec["id"], checkout_proxy, approve_proxy, timeout)
        route_options = _route_options(spec, checkout_proxy, approve_proxy, timeout)
        paypal_config: dict[str, Any] = {}
        if args.operation == "pay":
            paypal_config, index_paths = _paypal_runtime_config(engine_root, payment_config, input_path)
        operation_label = "协议支付" if args.operation == "pay" else "协议提链"
        print(f"[protocol] 操作={operation_label} 渠道={spec['label']} {spec['country']}/{spec['currency']} 账号={len(accounts)} 并发={workers}", flush=True)
        if not checkout_proxy:
            print("[protocol][WARN] 未配置提链出口，协议引擎将自行判定可用路由", flush=True)

        def extract(index: int, item: dict[str, str]) -> dict[str, Any]:
            plan = PaymentRoutePlanner(config).plan(
                spec["id"], options=route_options, pool_offset=index
            )
            routed = {
                **route_options,
                **plan.to_adapter_options(),
                "payment_route_plan": plan,
            }
            routed.pop("checkout_proxy_pool", None)
            routed.pop("approve_proxy_pool", None)
            if args.operation == "pay":
                link = generate_payment_link(
                    access_token=item["access_token"],
                    payment_method=spec["id"],
                    proxy=plan.checkout_proxy or None,
                    runtime_config=config,
                    **routed,
                )
                if not isinstance(link, dict) or not link.get("ok") or not link.get("url"):
                    failed = _public_result(item["email"], link)
                    failed["operation"] = "execute_payment"
                    return failed
                return _execute_paypal_payment(
                    item,
                    runtime_config=_paypal_config_for_route(paypal_config, spec, routed),
                    paypal_link=link,
                    proxy=plan.proxy_for("approve", plan.checkout_proxy),
                    timeout=timeout,
                )
            result = generate_payment_link(
                access_token=item["access_token"],
                payment_method=spec["id"],
                proxy=plan.checkout_proxy or None,
                runtime_config=config,
                **routed,
            )
            return _public_result(item["email"], result)

        rows: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(extract, index, item): item["email"]
                for index, item in enumerate(accounts)
            }
            for future in concurrent.futures.as_completed(futures):
                email = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001
                    row = _public_result(email, {
                        "ok": False,
                        "operation": "execute_payment" if args.operation == "pay" else "extract_link",
                        "error": exc,
                        "error_code": "worker_exception",
                    })
                rows.append(row)
                state = "OK" if row.get("ok") else "FAIL"
                detail = _result_detail(row)
                print(f"[protocol][{state}] {row['email']} {detail}", flush=True)

        report = {
            "schema": "reg_factory.protocol_payment_batch.v1",
            "payment_method": spec["id"],
            "operation": "execute_payment" if args.operation == "pay" else "extract_link",
            "count": len(rows),
            "success": sum(bool(row["ok"]) for row in rows),
            "failed": sum(not bool(row["ok"]) for row in rows),
            "results": rows,
        }
        if args.report:
            report_path = Path(args.report).resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[protocol] 完成 成功={report['success']} 失败={report['failed']}", flush=True)
        return 0 if report["failed"] == 0 else 2
    finally:
        for index_path in index_paths:
            try:
                index_path.unlink(missing_ok=True)
            except OSError:
                pass
        if args.delete_input:
            try:
                input_path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
